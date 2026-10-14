"""项目级（group）收获分析 proj_ts_m1_v1：去重、参数、逐地块统计汇总等纯函数。

API（去重、落库汇总、接口）与下载机 ingest（像元计算后的逐地块统计）共用。
像元级规则（需要 rasterio）在 ingest 的 ``app.core.project_harvest``。
设计：analysis/group2878/design_project_harvest.md。
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

METHOD_VERSION = "proj_ts_m1_v1"
PRIORITY_RULE = "status_desc,land_id_desc"
MU_PER_M2 = 1.0 / 666.667

# 分级码（栅格值）与名称/配色；255 = 地块外
TIERS: list[dict[str, Any]] = [
    {"code": 0, "key": "high", "name": "高置信已收获", "color": "#B31A1A"},
    {"code": 1, "key": "medium", "name": "中置信已收获", "color": "#F28C26"},
    {"code": 2, "key": "low", "name": "低/疑似收获", "color": "#FFE64D"},
    {"code": 3, "key": "unharvested", "name": "未收获", "color": "#269933"},
    {"code": 4, "key": "cloud", "name": "云/无效", "color": "#B8B8B8"},
    {"code": 5, "key": "noncrop", "name": "未种植/非耕地", "color": "#8C6B4D"},
]
TIER_KEYS = [t["key"] for t in TIERS]
CROP_KEYS = TIER_KEYS[:5]
OUTSIDE = 255

DEFAULT_PARAMS: dict[str, Any] = {
    "early_cutoff": "09-10",
    "green_thr": 0.50,
    "low_thr": 0.50,
    "drop_min": 0.25,
    "stay_thr": 0.60,
    "min_clump_px": 4,
    "edge_min_frac": 0.5,
    "valid_frac": 0.30,
    "haze_b02": 0.12,
    "pick_cloud_max": 0.20,
    "clear_scl": [4, 5, 6, 7],
    "hi_score": 0.5,
    "hi_drop": 0.35,
    # 去重
    "min_keep_mu": 0.3,
    "thin_m": 5.0,
    "max_span_deg": 0.05,
    "calib_min_parcels": 10,
    "clear_center_only": True,
}


def merge_params(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_PARAMS)
    for k, v in (overrides or {}).items():
        if k in DEFAULT_PARAMS and v is not None:
            out[k] = v
    return out


# ------------------------------------------------------------------ geometry
def _geojson(obj: Any) -> Any:
    if isinstance(obj, str):
        obj = json.loads(obj)
    if isinstance(obj, Mapping) and obj.get("type") == "Feature":
        obj = obj.get("geometry")
    return obj


def polygonal(geom: Any):
    """只保留面要素（make_valid 可能产出 GeometryCollection）；空返回 None。"""
    from shapely.geometry import MultiPolygon

    polys: list[Any] = []
    for part in getattr(geom, "geoms", [geom]):
        if part.is_empty:
            continue
        if part.geom_type == "Polygon":
            polys.append(part)
        elif part.geom_type == "MultiPolygon":
            polys.extend(p for p in part.geoms if not p.is_empty)
    if not polys:
        return None
    return MultiPolygon(polys) if len(polys) > 1 else polys[0]


def parse_boundary(boundary: Any) -> tuple[Any, bool]:
    """解析 WGS84 边界并修复；返回 (geom|None, repaired)。"""
    from shapely.geometry import shape
    from shapely.validation import make_valid

    try:
        g = shape(_geojson(boundary))
    except Exception:
        return None, False
    repaired = False
    if not g.is_valid:
        g = make_valid(g)
        repaired = True
    return polygonal(g), repaired


def utm_epsg_for(lon: float, lat: float) -> int:
    zone = int((lon + 180.0) // 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _lid_key(land_id: str) -> tuple[int, Any]:
    s = str(land_id)
    return (1, int(s)) if s.isdigit() else (0, s)


def priority_key(land: Mapping[str, Any]) -> tuple:
    """(启用状态, land_id) 越大越优先。"""
    return (int(str(land.get("land_status")) == "1"), _lid_key(str(land["land_id"])))


def dedup_hash(lands: Sequence[Mapping[str, Any]], params: Mapping[str, Any]) -> str:
    h = hashlib.sha1()
    for land in sorted(lands, key=lambda x: str(x["land_id"])):
        b = land.get("boundary_geojson")
        b = b if isinstance(b, str) else json.dumps(b, sort_keys=True, separators=(",", ":"))
        h.update(f"{land['land_id']}|{land.get('land_status')}|".encode())
        h.update(hashlib.sha1((b or "").encode()).digest())
    h.update(
        f"{PRIORITY_RULE}|{params.get('min_keep_mu')}|{params.get('thin_m')}|{params.get('max_span_deg')}".encode()
    )
    return h.hexdigest()[:24]


def dedup_parcels(
    lands: Sequence[Mapping[str, Any]], params: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """按优先级 (land_status=1 优先, land_id 大者优先) 去重。

    每块 = 原边界 − 所有更高优先级原边界的并集；剩余 < min_keep_mu 亩 / 为空 / 内缩 thin_m 后为空记为被合并。
    边界跨度 > max_span_deg（明显错误的边界）记为 invalid_boundary，不参与覆盖。
    返回 {epsg, hash, rows:[...]}，rows 按优先级序。
    """
    from pyproj import Transformer
    from shapely.geometry import MultiPolygon, mapping
    from shapely.ops import transform as stf
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    p = merge_params(params)
    parsed = []
    for land in lands:
        g, repaired = parse_boundary(land.get("boundary_geojson"))
        bad = g is None
        if g is not None:
            x0, y0, x1, y1 = g.bounds
            bad = max(x1 - x0, y1 - y0) > float(p["max_span_deg"])
        parsed.append((land, g, repaired, bad))
    good = [x for x in parsed if not x[3]]
    if good:
        cs = [x[1].centroid for x in good]
        epsg = utm_epsg_for(sum(c.x for c in cs) / len(cs), sum(c.y for c in cs) / len(cs))
    else:
        epsg = 32650
    to = Transformer.from_crs(4326, epsg, always_xy=True).transform
    back = Transformer.from_crs(epsg, 4326, always_xy=True).transform

    order = sorted(parsed, key=lambda x: priority_key(x[0]), reverse=True)
    utm = [None if bad else stf(to, g).buffer(0) for (_, g, _, bad) in order]
    idx_ok = [i for i, u in enumerate(utm) if u is not None]
    tree = STRtree([utm[i] for i in idx_ok]) if idx_ok else None
    rows: list[dict[str, Any]] = []
    min_mu = float(p["min_keep_mu"])
    thin = float(p["thin_m"])
    for rank, (land, g, repaired, bad) in enumerate(order, 1):
        i_self = rank - 1
        lid = str(land["land_id"])
        row = {
            "land_id": lid,
            "land_status": None if land.get("land_status") is None else str(land.get("land_status")),
            "land_name": land.get("land_name"),
            "priority_rank": rank,
            "priority_rule": PRIORITY_RULE,
            "registered_area_mu": land.get("land_area_mu"),
            "boundary_repaired": bool(repaired),
            "town_name": land.get("town_name"),
            "village_name": land.get("village_name"),
            "covered_by": [],
            "kept": False,
            "merged_reason": None,
            "clipped_geojson": None,
            "clipped_area_mu": 0.0,
            "original_area_mu": None,
        }
        if bad:
            row["merged_reason"] = "invalid_boundary"
            rows.append(row)
            continue
        u = utm[i_self]
        row["original_area_mu"] = round(u.area * MU_PER_M2, 4)
        cover = []
        inter = []
        for j in sorted(idx_ok[int(k)] for k in tree.query(u)):
            if j >= i_self:
                continue  # 只与更高优先级（已处理）的比较
            q = utm[j]
            if q.intersects(u):
                inter.append(q)
                if q.intersection(u).area > 1:
                    cover.append(str(order[j][0]["land_id"]))
        r = u.difference(unary_union(inter)) if inter else u
        if not r.is_empty:
            r = polygonal(r.buffer(0))
            r = MultiPolygon([r]) if r is not None and r.geom_type == "Polygon" else r
        a = 0.0 if r is None or r.is_empty else r.area * MU_PER_M2
        row["covered_by"] = sorted(cover, key=_lid_key)
        row["clipped_area_mu"] = round(a, 4)
        if r is None or r.is_empty:
            row["merged_reason"] = "empty"
        elif a < min_mu:
            row["merged_reason"] = "sliver"
        elif r.buffer(-thin).is_empty:
            row["merged_reason"] = "thin"
        else:
            row["kept"] = True
            row["clipped_geojson"] = mapping(stf(back, r))
        rows.append(row)
    return {"epsg": epsg, "hash": dedup_hash(lands, p), "rows": rows}


# ------------------------------------------------------------------ stats
def parcel_date_row(sums: Mapping[str, float], weight: float, area_mu: float) -> dict[str, float]:
    """把加权像元计数换算为亩（按去重后实测亩缩放）。"""
    w = max(float(weight), 1e-9)
    out = {f"{k}_mu": float(sums.get(k, 0.0)) / w * float(area_mu) for k in TIER_KEYS}
    out["area_mu"] = float(area_mu)
    out["crop_mu"] = float(area_mu) - out["noncrop_mu"]
    crop = out["crop_mu"]
    out["harvested_pct"] = (
        round(100.0 * (out["high_mu"] + out["medium_mu"]) / crop, 1) if crop > 1e-9 else None
    )
    out["cloud_pct"] = round(100.0 * out["cloud_mu"] / crop, 1) if crop > 1e-9 else None
    return out


def choose_pick(rows_by_date: Mapping[str, Mapping[str, float]], cloud_max: float) -> str | None:
    """采用日期：耕种部分云/无效 ≤ cloud_max 的最新日期，否则最新日期。"""
    dates = sorted(rows_by_date)
    for d in reversed(dates):
        r = rows_by_date[d]
        if r["crop_mu"] <= 1e-9 or r["cloud_mu"] / r["crop_mu"] <= cloud_max:
            return d
    return dates[-1] if dates else None


_SUM_KEYS = ["area_mu", "crop_mu"] + [f"{k}_mu" for k in TIER_KEYS]


def aggregate(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {k: 0.0 for k in _SUM_KEYS}
    n = 0
    for r in rows:
        n += 1
        for k in _SUM_KEYS:
            out[k] += float(r.get(k) or 0.0)
    out["parcel_count"] = n
    c = out["crop_mu"]
    out["harvested_pct"] = round(100.0 * (out["high_mu"] + out["medium_mu"]) / c, 1) if c > 1e-9 else None
    out["cloud_pct"] = round(100.0 * out["cloud_mu"] / c, 1) if c > 1e-9 else None
    return out


def _scopes(meta: Mapping[str, Any]) -> list[tuple[str, str]]:
    status = str(meta.get("land_status") or "")
    return [
        ("all", "all"),
        ("block", str(meta.get("block_key") or "")),
        ("town_village", f"{meta.get('town_name') or ''} {meta.get('village_name') or ''}".strip() or "-"),
        ("status", status or "-"),
    ]


def build_summaries(
    stats: Sequence[Mapping[str, Any]],
    parcels: Mapping[str, Mapping[str, Any]],
    pick_date_key: str,
) -> list[dict[str, Any]]:
    """逐地块逐日 → 汇总行（all/block/town_village/status 逐日；pick 口径 obs_date=pick_date_key）。"""
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in stats:
        meta = parcels.get(str(r["land_id"]), {})
        meta = {**meta, "block_key": r.get("block_key")}
        for st, sk in _scopes(meta):
            groups[(str(r["obs_date"]), st, sk)].append(r)
            if r.get("is_pick"):
                groups[(pick_date_key, "pick", f"{st}:{sk}")].append(r)
    out = []
    for (d, st, sk), rows in sorted(groups.items()):
        a = aggregate(rows)
        out.append({"obs_date": d, "scope_type": st, "scope_key": sk, **a})
    return out


def series_from_summaries(summaries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for s in summaries:
        if s["scope_type"] == "all":
            by_date.setdefault(str(s["obs_date"]), {}).update(
                date=str(s["obs_date"]),
                harvested_pct=s.get("harvested_pct"),
                cloud_pct=s.get("cloud_pct"),
                crop_mu=s.get("crop_mu"),
            )
    for s in summaries:
        if s["scope_type"] == "block" and str(s["obs_date"]) in by_date:
            d = by_date[str(s["obs_date"])]
            d.setdefault("blocks", {})[s["scope_key"]] = s.get("harvested_pct")
    return [by_date[k] for k in sorted(by_date)]
