"""全部地块收获进度报表（JSON 分页 + Excel 导出共用的数据层）。

只读已落库的收获进度结果（``parcel_harvest_progress``，当前算法版本、S2），不做现场计算、
不入队：报表面向全部地块，逐块现场计算代价过高，且 GET 不应产生写入。

落库门槛（``HARVEST_PROGRESS_MIN_SAVE_PCT``，默认 3%）使低于门槛的观测日不入库，所以：

* 区间内有结果行 → 已计算，最新一行即地块最新状态；
* 区间内无结果行，但已计算区间标记（``parcel_harvest_progress_coverage``）覆盖查询区间
  → 已计算、合计占比均未超过门槛，按“未收获（<门槛）”计 0%；
* 两者都没有 → “未计算”（需 harvest-progress/backfill 重算后才会出现在统计中）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

import structlog
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.harvest_progress import HARVEST_PROGRESS_METHOD_VERSION
from app.services.harvest_progress import SENSOR, min_save_pct

logger = structlog.get_logger(__name__)

# 进度分档（按最新合计占比 = 已收获 + 疑似收获）
BUCKET_NOT_COMPUTED = "not_computed"
BUCKET_NONE = "none"  # 已计算，无观测日超过落库门槛
BUCKET_LT30 = "lt30"
BUCKET_30_90 = "30_90"
BUCKET_GE90 = "ge90"
BUCKETS = (BUCKET_NONE, BUCKET_LT30, BUCKET_30_90, BUCKET_GE90, BUCKET_NOT_COMPUTED)

BUCKET_LABELS_ZH = {
    BUCKET_NONE: "未收获（≤门槛）",
    BUCKET_LT30: "收获 <30%",
    BUCKET_30_90: "收获 30–90%",
    BUCKET_GE90: "收获 ≥90%",
    BUCKET_NOT_COMPUTED: "未计算",
}
STATUS_LABELS_ZH = {
    "growing": "生长中",
    "harvesting": "收获中",
    "harvested": "已收获",
}
CONFIDENCE_LABELS_ZH = {"high": "高", "medium": "中", "low": "低"}

SORT_FIELDS = (
    "land_id",
    "land_name",
    "group_name",
    "crop_type",
    "area_mu",
    "first_harvest_date",
    "latest_date",
    "harvested_pct",
    "suspected_pct",
    "combined_pct",
    "harvested_area_mu",
    "combined_area_mu",
    "newly_combined_pct",
    "confidence",
    "days_since_last_image",
)


@dataclass
class ReportFilters:
    date_from: date
    date_to: date
    group_id: str | None = None
    crop: str | None = None
    buckets: list[str] = field(default_factory=list)
    min_pct: float | None = None
    keyword: str | None = None


_LANDS = text(
    """
    SELECT land_id, land_name, group_id, group_name, farm_id, crop_type,
           province_name, city_name, county_name,
           COALESCE(land_area_mu, area_ha * 15) AS area_mu
    FROM agric_satellite.land_parcels
    WHERE deleted_at IS NULL
      AND (CAST(:group_id AS text) IS NULL OR group_id = CAST(:group_id AS text))
      AND (CAST(:crop AS text) IS NULL OR crop_type = CAST(:crop AS text))
      AND (CAST(:kw AS text) IS NULL
           OR land_id = CAST(:kw AS text)
           OR land_name ILIKE '%' || CAST(:kw AS text) || '%')
    ORDER BY land_id
    """
)

_ROW_COLS = """
    land_id, obs_date, status, harvested_pct, suspected_harvest_pct,
    harvested_or_suspected_pct, newly_harvested_pct, harvested_area_mu,
    parcel_area_mu, valid_pct, season_start, confidence, confidence_level,
    confirmed, confirmed_by, method_version
"""

_ROWS = text(
    f"""
    SELECT {_ROW_COLS}
    FROM agric_satellite.parcel_harvest_progress
    WHERE sensor = :sensor AND method_version = :method_version
      AND land_id IN :ids
      AND obs_date >= :date_from AND obs_date <= :date_to
    ORDER BY land_id, obs_date
    """
).bindparams(bindparam("ids", expanding=True))

# 区间前最后一条结果行：用于区间首个观测日的“较上期新增”。
_PREV_ROWS = text(
    f"""
    SELECT DISTINCT ON (land_id) {_ROW_COLS}
    FROM agric_satellite.parcel_harvest_progress
    WHERE sensor = :sensor AND method_version = :method_version
      AND land_id IN :ids
      AND obs_date < :date_from
    ORDER BY land_id, obs_date DESC
    """
).bindparams(bindparam("ids", expanding=True))

_COVERAGE = text(
    """
    SELECT land_id, computed_from, computed_to, min_save_pct
    FROM agric_satellite.parcel_harvest_progress_coverage
    WHERE sensor = :sensor AND method_version = :method_version
      AND land_id IN :ids
    """
).bindparams(bindparam("ids", expanding=True))

# 不按 pixel_data->>'format' 过滤：那会把每行整份像元 JSON 解压出来，全量报表代价过高。
_LATEST_OBS = text(
    """
    SELECT land_id, max(date) AS obs_date
    FROM agric_satellite.parcel_scene_products
    WHERE sensor = 'S2' AND land_id IN :ids AND date <= :date_to
    GROUP BY land_id
    """
).bindparams(bindparam("ids", expanding=True))

_CHUNK = 1000


def _f(value: Any, nd: int = 1) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return round(v, nd)


def _d(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def normalize_row(r: dict[str, Any]) -> dict[str, Any]:
    harvested = _f(r.get("harvested_pct")) or 0.0
    combined = _f(r.get("harvested_or_suspected_pct"))
    if combined is None:
        combined = harvested
    suspected = _f(r.get("suspected_harvest_pct"))
    if suspected is None:
        suspected = round(max(combined - harvested, 0.0), 1)
    return {
        "land_id": str(r["land_id"]),
        "date": _d(r.get("obs_date") or r.get("date")),
        "status": r.get("status"),
        "harvested_pct": harvested,
        "suspected_pct": suspected,
        "combined_pct": combined,
        "newly_harvested_pct": _f(r.get("newly_harvested_pct")) or 0.0,
        "harvested_area_mu": _f(r.get("harvested_area_mu"), 2),
        "parcel_area_mu": _f(r.get("parcel_area_mu"), 2),
        "valid_pct": _f(r.get("valid_pct")),
        "season_start": _d(r.get("season_start")),
        "confidence": _f(r.get("confidence"), 2),
        "confidence_level": r.get("confidence_level"),
        "confirmed": r.get("confirmed"),
        "confirmed_by": r.get("confirmed_by"),
        "method_version": r.get("method_version") or HARVEST_PROGRESS_METHOD_VERSION,
    }


def bucket_for(computed: bool, combined: float | None) -> str:
    if not computed:
        return BUCKET_NOT_COMPUTED
    p = combined or 0.0
    if p <= 0:
        return BUCKET_NONE
    if p < 30:
        return BUCKET_LT30
    if p < 90:
        return BUCKET_30_90
    return BUCKET_GE90


def coverage_state(
    coverage: dict[str, Any] | None,
    date_from: date,
    date_to: date,
    latest_obs: date | None,
    today: date,
) -> str:
    """``full`` 覆盖查询区间 / ``partial`` 有交集 / ``none``。

    与接口 range_computed 一致：标记止于上次计算日，之后若没有新的 S2 观测也算覆盖。
    """
    if not coverage:
        return "none"
    c_from, c_to = _d(coverage.get("computed_from")), _d(coverage.get("computed_to"))
    if c_from is None or c_to is None:
        return "none"
    hi = min(date_to, today)
    if c_from <= date_from and (c_to >= hi or latest_obs is None or latest_obs <= c_to):
        return "full"
    if c_from <= hi and c_to >= date_from:
        return "partial"
    return "none"


def _area_mu(land: dict[str, Any], rows: list[dict[str, Any]]) -> float | None:
    a = _f(land.get("area_mu"), 2)
    if a is None:
        for r in reversed(rows):
            if r.get("parcel_area_mu") is not None:
                return r["parcel_area_mu"]
    return a


def build_report(
    filters: ReportFilters,
    lands: Iterable[dict[str, Any]],
    rows: Iterable[dict[str, Any]],
    prev_rows: Iterable[dict[str, Any]] = (),
    coverage: dict[str, dict[str, Any]] | None = None,
    latest_obs: dict[str, date | None] | None = None,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """纯函数：组装每地块最新状态（未过滤分档/门槛前的全集）与逐日明细。

    返回 ``(parcels, daily)``；daily 只含区间内落库的观测日，附 ``newly_combined_pct``
    （合计占比较同一季上一条结果行的增量；上一条不存在时即本期合计，因为之前均 ≤ 门槛）。
    """
    today = today or date.today()
    coverage = coverage or {}
    latest_obs = latest_obs or {}
    by_land: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        n = normalize_row(r)
        by_land.setdefault(n["land_id"], []).append(n)
    prev = {str(r["land_id"]): normalize_row(r) for r in prev_rows}

    parcels: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    for land in lands:
        lid = str(land["land_id"])
        lrows = sorted(by_land.get(lid, []), key=lambda r: r["date"] or date.min)
        area = _area_mu(land, lrows)
        last_prev = prev.get(lid)
        for r in lrows:
            same_season = (
                last_prev is not None and last_prev["season_start"] == r["season_start"]
            )
            base = last_prev["combined_pct"] if same_season else 0.0
            r["newly_combined_pct"] = round(max(r["combined_pct"] - base, 0.0), 1)
            r["combined_area_mu"] = (
                round(area * r["combined_pct"] / 100.0, 2) if area else None
            )
            last_prev = r
            daily.append({**_land_cols(land, area), **r})

        obs = _d(latest_obs.get(lid))
        cov = coverage_state(
            coverage.get(lid), filters.date_from, filters.date_to, obs, today
        )
        latest = lrows[-1] if lrows else None
        computed = latest is not None or cov == "full"
        p: dict[str, Any] = {
            **_land_cols(land, area),
            "computed": computed,
            "coverage": "stored" if latest is not None else cov,
            "latest_obs_date": obs,
            "days_since_last_image": (min(filters.date_to, today) - obs).days
            if obs
            else None,
            "obs_count": len(lrows),
            "first_harvest_date": None,
            "latest_date": None,
            "status": None,
            "harvested_pct": 0.0 if computed else None,
            "suspected_pct": 0.0 if computed else None,
            "combined_pct": 0.0 if computed else None,
            "harvested_area_mu": 0.0 if computed else None,
            "combined_area_mu": 0.0 if computed else None,
            "newly_combined_pct": None,
            "newly_harvested_pct": None,
            "season_start": None,
            "confidence": None,
            "confidence_level": None,
            "confirmed": None,
            "method_version": HARVEST_PROGRESS_METHOD_VERSION if computed else None,
        }
        if latest is not None:
            season = [r for r in lrows if r["season_start"] == latest["season_start"]]
            p.update(
                first_harvest_date=season[0]["date"],
                latest_date=latest["date"],
                status=latest["status"],
                harvested_pct=latest["harvested_pct"],
                suspected_pct=latest["suspected_pct"],
                combined_pct=latest["combined_pct"],
                harvested_area_mu=latest["harvested_area_mu"]
                if latest["harvested_area_mu"] is not None
                else (round(area * latest["harvested_pct"] / 100, 2) if area else None),
                combined_area_mu=latest["combined_area_mu"],
                newly_combined_pct=latest["newly_combined_pct"],
                newly_harvested_pct=latest["newly_harvested_pct"],
                season_start=latest["season_start"],
                confidence=latest["confidence"],
                confidence_level=latest["confidence_level"],
                confirmed=latest["confirmed"],
                method_version=latest["method_version"],
            )
        elif computed:
            p["status"] = "growing"
        p["bucket"] = bucket_for(computed, p["combined_pct"])
        parcels.append(p)
    return parcels, daily


def _land_cols(land: dict[str, Any], area: float | None) -> dict[str, Any]:
    return {
        "land_id": str(land["land_id"]),
        "land_name": land.get("land_name"),
        "group_id": land.get("group_id"),
        "group_name": land.get("group_name"),
        "farm_id": land.get("farm_id"),
        "crop_type": land.get("crop_type"),
        "province_name": land.get("province_name"),
        "city_name": land.get("city_name"),
        "county_name": land.get("county_name"),
        "area_mu": area,
    }


def apply_filters(
    parcels: list[dict[str, Any]], filters: ReportFilters
) -> list[dict[str, Any]]:
    out = parcels
    if filters.buckets:
        wanted = set(filters.buckets)
        out = [p for p in out if p["bucket"] in wanted]
    if filters.min_pct is not None and filters.min_pct > 0:
        out = [p for p in out if (p["combined_pct"] or 0.0) >= filters.min_pct]
    return out


def sort_parcels(
    parcels: list[dict[str, Any]], sort: str = "combined_pct", order: str = "desc"
) -> list[dict[str, Any]]:
    key = sort if sort in SORT_FIELDS else "combined_pct"
    present = [p for p in parcels if p.get(key) is not None]
    missing = [p for p in parcels if p.get(key) is None]
    present.sort(
        key=lambda p: (
            (p[key], p["land_id"])
            if not isinstance(p[key], str)
            else (p[key].lower(), p["land_id"])
        ),
        reverse=order == "desc",
    )
    return present + sorted(missing, key=lambda p: p["land_id"])  # 空值始终排最后


def facets(parcels: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """筛选下拉选项：分组与作物（按地块数降序）。"""
    groups: dict[str, dict[str, Any]] = {}
    crops: dict[str, int] = {}
    for p in parcels:
        gid = p.get("group_id")
        if gid:
            g = groups.setdefault(
                str(gid), {"id": str(gid), "name": p.get("group_name"), "count": 0}
            )
            g["count"] += 1
        if p.get("crop_type"):
            crops[p["crop_type"]] = crops.get(p["crop_type"], 0) + 1
    return {
        "groups": sorted(groups.values(), key=lambda g: (-g["count"], g["id"])),
        "crops": [
            {"id": k, "count": v}
            for k, v in sorted(crops.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }


def summarize(parcels: list[dict[str, Any]]) -> dict[str, Any]:
    computed = [p for p in parcels if p["computed"]]
    total_area = sum(p["area_mu"] or 0.0 for p in parcels)
    computed_area = sum(p["area_mu"] or 0.0 for p in computed)
    harvested_area = sum(p["harvested_area_mu"] or 0.0 for p in computed)
    combined_area = sum(p["combined_area_mu"] or 0.0 for p in computed)
    counts = {b: 0 for b in BUCKETS}
    for p in parcels:
        counts[p["bucket"]] += 1
    return {
        "parcel_count": len(parcels),
        "computed_count": len(computed),
        "total_area_mu": round(total_area, 2),
        "computed_area_mu": round(computed_area, 2),
        "harvested_area_mu": round(harvested_area, 2),
        "combined_area_mu": round(combined_area, 2),
        "avg_combined_pct": round(
            sum(p["combined_pct"] or 0.0 for p in computed) / len(computed), 1
        )
        if computed
        else None,
        "area_weighted_combined_pct": round(combined_area / computed_area * 100, 1)
        if computed_area
        else None,
        "bucket_counts": counts,
        "min_save_pct": min_save_pct(),
    }


async def load_report(
    db: AsyncSession, filters: ReportFilters
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读库并组装（未分页）。只读；覆盖表缺失（迁移未执行）时按无标记处理。"""
    lands = [
        dict(r._mapping)
        for r in (
            await db.execute(
                _LANDS,
                {
                    "group_id": filters.group_id or None,
                    "crop": filters.crop or None,
                    "kw": (filters.keyword or "").strip() or None,
                },
            )
        ).fetchall()
    ]
    ids = [str(land["land_id"]) for land in lands]
    rows: list[dict[str, Any]] = []
    prev: list[dict[str, Any]] = []
    coverage: dict[str, dict[str, Any]] = {}
    latest: dict[str, date | None] = {}
    common = {"sensor": SENSOR, "method_version": HARVEST_PROGRESS_METHOD_VERSION}
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        rng = {"ids": chunk, "date_from": filters.date_from, "date_to": filters.date_to}
        rows += [
            dict(r._mapping)
            for r in (await db.execute(_ROWS, {**common, **rng})).fetchall()
        ]
        prev += [
            dict(r._mapping)
            for r in (
                await db.execute(
                    _PREV_ROWS, {**common, "ids": chunk, "date_from": filters.date_from}
                )
            ).fetchall()
        ]
        for r in (
            await db.execute(_LATEST_OBS, {"ids": chunk, "date_to": filters.date_to})
        ).fetchall():
            latest[str(r.land_id)] = r.obs_date
        try:
            async with db.begin_nested():
                for r in (
                    await db.execute(_COVERAGE, {**common, "ids": chunk})
                ).fetchall():
                    coverage[str(r.land_id)] = dict(r._mapping)
        except Exception as exc:  # 覆盖表未建：全部按“无标记”处理
            logger.warning("harvest_report_coverage_unavailable", error=str(exc)[:200])
    return build_report(filters, lands, rows, prev, coverage, latest)
