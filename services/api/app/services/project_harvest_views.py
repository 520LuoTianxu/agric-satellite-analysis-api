"""项目级收获分析接口的响应组装（纯函数，输入为数据库行字典；本地模拟服务也复用）。"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from agric_satellite_analysis_common import project_harvest as ph

LEGEND = [{"code": t["code"], "key": t["key"], "name": t["name"], "color": t["color"]} for t in ph.TIERS]
_MU = ["area_mu", "crop_mu"] + [f"{k}_mu" for k in ph.TIER_KEYS]


def _f(v: Any) -> float | None:
    return None if v is None else round(float(v), 2)


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def run_view(run: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not run:
        return None
    return {
        "run_id": str(run["run_id"]),
        "group_id": run["group_id"],
        "status": run["status"],
        "method_version": run.get("method_version") or ph.METHOD_VERSION,
        "is_current": bool(run.get("is_current")),
        "season_from": _iso(run.get("season_from")),
        "season_to": _iso(run.get("season_to")),
        "dedup_hash": run.get("dedup_hash"),
        "parcel_count": run.get("parcel_count"),
        "merged_count": run.get("merged_count"),
        "dedup_area_mu": _f(run.get("dedup_area_mu")),
        "trigger": run.get("trigger"),
        "error": run.get("error"),
        "created_at": _iso(run.get("created_at")),
        "started_at": _iso(run.get("started_at")),
        "finished_at": _iso(run.get("finished_at")),
    }


def overview_view(run: Mapping[str, Any] | None, active: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """当前运行元数据；没有运行时 available=false。"""
    if not run:
        return {"available": False, "active_run": run_view(active), "legend": LEGEND,
                "method_version": ph.METHOD_VERSION}
    extras = run.get("extras") or {}
    blocks = []
    for b in run.get("blocks") or []:
        blocks.append({"key": b.get("key"), "bbox": b.get("bbox4326"), "dates": b.get("dates") or [],
                       "parcel_count": b.get("parcel_count"), "note": b.get("note")})
    return {
        "available": True,
        **(run_view(run) or {}),
        "valid_dates": [_iso(d) for d in (run.get("valid_dates") or [])],
        "blocks": blocks,
        "thresholds": run.get("thresholds") or {},
        "params": run.get("params") or {},
        "pick_dates": extras.get("pick_dates") or {},
        "dedup": {"kept": run.get("parcel_count"), "merged": run.get("merged_count"),
                  "area_mu": _f(run.get("dedup_area_mu")), "rule": ph.PRIORITY_RULE},
        "active_run": run_view(active),
        "legend": LEGEND,
    }


def _totals(s: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: _f(s.get(k)) for k in _MU}
    out["parcel_count"] = s.get("parcel_count")
    out["harvested_mu"] = _f((s.get("high_mu") or 0) + (s.get("medium_mu") or 0))
    out["harvested_pct"] = None if s.get("harvested_pct") is None else float(s["harvested_pct"])
    crop = float(s.get("crop_mu") or 0)
    out["cloud_pct"] = round(100 * float(s.get("cloud_mu") or 0) / crop, 1) if crop > 0 else None
    return out


def summary_view(
    summaries: Sequence[Mapping[str, Any]], date: str | None, season_to: str | None, pick_dates: Mapping[str, int] | None = None
) -> dict[str, Any]:
    """date=None/'pick' → 每块采用日期口径；否则该日期口径。"""
    pick = date in (None, "", "pick")
    key_date = str(season_to) if pick else str(date)
    rows = [s for s in summaries if _iso(s["obs_date"]) == key_date]
    out: dict[str, Any] = {"date": "pick" if pick else key_date, "totals": None,
                           "by_block": [], "by_town_village": [], "by_status": []}
    for s in rows:
        st, sk = s["scope_type"], s["scope_key"]
        if pick:
            if st != "pick":
                continue
            st, _, sk = sk.partition(":")
        elif st == "pick":
            continue
        item = {"key": sk, **_totals(s)}
        if st == "all":
            out["totals"] = _totals(s)
        elif st == "block":
            out["by_block"].append(item)
        elif st == "town_village":
            out["by_town_village"].append(item)
        elif st == "status":
            out["by_status"].append(item)
    for k in ("by_block", "by_town_village", "by_status"):
        out[k].sort(key=lambda x: str(x["key"]))
    if pick:
        out["pick_dates"] = dict(pick_dates or {})
    out["series"] = ph.series_from_summaries([s for s in summaries if s["scope_type"] in ("all", "block")])
    return out


_SORTABLE = {"land_id", "harvested_pct", "area_mu", "crop_mu", "cloud_pct", "high_mu", "medium_mu", "noncrop_mu"}


def parcel_rows(
    stats: Sequence[Mapping[str, Any]], dedup: Mapping[str, Mapping[str, Any]], date: str | None
) -> list[dict[str, Any]]:
    """某日（或 pick）逐地块行。"""
    pick = date in (None, "", "pick")
    latest: dict[str, str] = {}
    for r in stats:
        lid = str(r["land_id"])
        d = _iso(r["obs_date"])
        latest[lid] = max(latest.get(lid, ""), d)
    out = []
    for r in stats:
        lid = str(r["land_id"])
        if pick and not r.get("is_pick"):
            continue
        if not pick and _iso(r["obs_date"]) != str(date):
            continue
        m = dedup.get(lid, {})
        out.append({
            "land_id": lid,
            "land_name": m.get("land_name"),
            "land_status": m.get("land_status"),
            "town_name": m.get("town_name"),
            "village_name": m.get("village_name"),
            "block_key": r.get("block_key"),
            "obs_date": _iso(r["obs_date"]),
            "is_pick": bool(r.get("is_pick")),
            "fallback": bool(r.get("is_pick")) and _iso(r["obs_date"]) != latest.get(lid),
            "registered_area_mu": _f(m.get("registered_area_mu")),
            "covered_by": list(m.get("covered_by") or []),
            "peak_ndvi_median": None if r.get("peak_ndvi_median") is None else round(float(r["peak_ndvi_median"]), 3),
            **{k: _f(r.get(k)) for k in _MU},
            "harvested_mu": _f((r.get("high_mu") or 0) + (r.get("medium_mu") or 0)),
            "harvested_pct": None if r.get("harvested_pct") is None else float(r["harvested_pct"]),
            "cloud_pct": None if r.get("cloud_pct") is None else float(r["cloud_pct"]),
        })
    return out


def filter_sort_page(
    rows: list[dict[str, Any]], *, q: str | None = None, status: str | None = None, sort: str = "harvested_pct",
    order: str = "desc", offset: int = 0, limit: int = 50,
) -> dict[str, Any]:
    if q:
        ql = q.strip().lower()
        rows = [r for r in rows if ql in " ".join(str(r.get(k) or "") for k in ("land_id", "land_name", "town_name", "village_name")).lower()]
    if status not in (None, ""):
        rows = [r for r in rows if str(r.get("land_status")) == str(status)]
    key = sort if sort in _SORTABLE else "harvested_pct"

    def k(r: Mapping[str, Any]):
        v = r.get(key)
        if key == "land_id":
            s = str(v)
            return (1, int(s)) if s.isdigit() else (0, s)
        return (v is not None, v if v is not None else 0)

    rows = sorted(rows, key=k, reverse=(order != "asc"))
    return {"total": len(rows), "offset": offset, "limit": limit, "items": rows[offset : offset + limit]}


def tier_dominant(r: Mapping[str, Any]) -> str:
    vals = {k: float(r.get(f"{k}_mu") or 0) for k in ph.TIER_KEYS}
    return max(vals, key=vals.get) if any(vals.values()) else "cloud"


def geojson_view(dedup_rows: Iterable[Mapping[str, Any]], rows_by_land: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    feats = []
    for d in dedup_rows:
        if not d.get("kept") or not d.get("clipped_geojson"):
            continue
        lid = str(d["land_id"])
        r = rows_by_land.get(lid, {})
        feats.append({
            "type": "Feature",
            "id": int(lid) if lid.isdigit() else lid,
            "geometry": d["clipped_geojson"],
            "properties": {
                "land_id": lid, "land_name": d.get("land_name"), "land_status": d.get("land_status"),
                "town_name": d.get("town_name"), "village_name": d.get("village_name"),
                "area_mu": _f(d.get("clipped_area_mu")), "harvested_pct": r.get("harvested_pct"),
                "harvested_mu": r.get("harvested_mu"), "cloud_pct": r.get("cloud_pct"),
                "noncrop_mu": r.get("noncrop_mu"), "obs_date": r.get("obs_date"),
                "block_key": r.get("block_key"), "dominant": tier_dominant(r) if r else None,
            },
        })
    return {"type": "FeatureCollection", "features": feats}


def layers_view(
    rasters: Sequence[Mapping[str, Any]], blocks: Sequence[Mapping[str, Any]], date: str, kind: str,
    url_for: Callable[[str], str | None],
) -> dict[str, Any]:
    items = []
    have = {r["block_key"]: r for r in rasters if _iso(r["obs_date"]) == date and r["kind"] == kind and r["format"] == "png"}
    for b in blocks:
        key = b.get("key")
        r = have.get(key)
        if r is None:
            items.append({"block": key, "available": False, "bbox": b.get("bbox4326")})
            continue
        w, s, e, n = (float(r[k]) for k in ("west", "south", "east", "north"))
        items.append({"block": key, "available": True, "url": url_for(r["oss_key"]),
                      "coordinates": [[w, n], [e, n], [e, s], [w, s]], "width": r.get("width"), "height": r.get("height")})
    return {"date": date, "kind": kind, "items": items, "legend": LEGEND}


def dedup_view(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    items = []
    for d in rows:
        items.append({
            "land_id": d["land_id"], "land_name": d.get("land_name"), "land_status": d.get("land_status"),
            "kept": bool(d.get("kept")), "merged_reason": d.get("merged_reason"), "priority_rank": d.get("priority_rank"),
            "clipped_area_mu": _f(d.get("clipped_area_mu")), "original_area_mu": _f(d.get("original_area_mu")),
            "registered_area_mu": _f(d.get("registered_area_mu")), "covered_by": list(d.get("covered_by") or []),
            "boundary_repaired": bool(d.get("boundary_repaired")),
            "town_name": d.get("town_name"), "village_name": d.get("village_name"),
        })
    items.sort(key=lambda x: x["priority_rank"] or 0)
    kept = [x for x in items if x["kept"]]
    return {"rule": ph.PRIORITY_RULE, "kept": len(kept), "merged": len(items) - len(kept),
            "kept_area_mu": round(sum(x["clipped_area_mu"] or 0 for x in kept), 2), "items": items}
