"""China overview (全国态势): country→province→city→county stats."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections import defaultdict
from urllib.parse import quote
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.scheduled_land_filter import (
    MAX_SCHEDULE_LAND_AREA_MU,
    scheduled_land_sql,
)
from app.core.agri_classify import (
    CLOUD_MAX_PCT,
    NDMI_DRY_ABS,
    PHENOLOGY_MONTHS,
    WEAK_NDVI_LT,
    classify_drought,
    classify_drought_from_pixels,
    classify_flood_series,
    is_drought_season,
    is_flood_alert,
    is_open_water_flood,
    overview_flood_bucket,
    official_s2_sql,
)
from app.core.crops import get_crop_season, normalize_crop_key
from app.core.database import get_db
from app.middleware.auth import OrgContext, require_roles
from app.models.tables import Job
from app.schemas.agri import (
    OverviewChildOut,
    OverviewDroughtCounts,
    OverviewFloodCounts,
    OverviewRegionOut,
    OverviewRegionsOut,
    OverviewStatsOut,
    OverviewTotals,
    OverviewWeakGrowth,
    OverviewWeakParcelOut,
    OverviewWeakParcelsOut,
)
from app.services.overview_daily import (
    business_today,
    read_daily_snapshot,
    read_daily_history,
    run_id_for,
    run_summary,
)

router = APIRouter(tags=["agri-overview"])

_reader = require_roles("owner", "admin", "member", "viewer")

OverviewLevel = Literal["country", "province", "city", "county"]

_CHILD_LEVEL: dict[OverviewLevel, OverviewLevel | None] = {
    "country": "province",
    "province": "city",
    "city": "county",
    "county": None,
}

_LEVEL_CODE_COL = {
    "province": "province_code",
    "city": "city_code",
    "county": "county_code",
}
_LEVEL_NAME_COL = {
    "province": "province_name",
    "city": "city_name",
    "county": "county_name",
}

_CACHE_FRESH_HOURS = 36
_overview_cache_ready = False
_overview_cache_init_lock = asyncio.Lock()

_ENSURE_CACHE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agric_satellite.overview_stats_daily (
    as_of_date date NOT NULL,
    level text NOT NULL,
    region_code text NOT NULL DEFAULT '',
    region_name text,
    parent_code text,
    metric_json jsonb NOT NULL,
    window_from date NOT NULL,
    window_to date NOT NULL,
    crop text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, level, region_code, window_from, window_to, crop)
)
"""
_ENSURE_CACHE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS overview_stats_daily_lookup_idx
    ON agric_satellite.overview_stats_daily (level, region_code, window_from, window_to, crop, updated_at DESC)
"""


def _default_window() -> tuple[date, date]:
    to_d = date.today()
    from_d = to_d - timedelta(days=60)
    return from_d, to_d


def _phenology_months(crop: str | None) -> list[int]:
    """Resolve phenology months for weak-growth; crop switches season window only."""
    if crop:
        months = sorted(get_crop_season(crop).season_months)
        if months:
            return months
    return list(PHENOLOGY_MONTHS)


def _month_in_clause(
    months: list[int], params: dict[str, Any], prefix: str = "pm"
) -> str:
    """Bind EXTRACT(MONTH ...) IN (:pm0, :pm1, ...) without array drivers."""
    if not months:
        months = list(PHENOLOGY_MONTHS)
    keys: list[str] = []
    for i, m in enumerate(months):
        key = f"{prefix}{i}"
        params[key] = int(m)
        keys.append(f":{key}")
    return f"EXTRACT(MONTH FROM s.date)::int IN ({', '.join(keys)})"


def _pad_adcode(level: OverviewLevel, code: str | None) -> str | None:
    """Pad DB codes to 6-digit Aliyun DataV adcodes when possible."""
    if not code:
        return None
    c = str(code).strip()
    if not c.isdigit():
        return c
    if level == "province" and len(c) <= 2:
        return c.zfill(2) + "0000"
    if level == "city" and len(c) <= 4:
        return c.zfill(4) + "00"
    if level == "county":
        return c.zfill(6)
    return c.zfill(6) if len(c) < 6 else c


def _affected_ratio(count: int, total: int) -> float:
    """计算稳定的 0..1 受影响比例，兼容空区划并限制异常计数。"""
    if total <= 0 or count <= 0:
        return 0.0
    return round(min(count / total, 1.0), 6)


async def _agri_ready(db: AsyncSession) -> None:
    q = await db.execute(
        text(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'agric_satellite' LIMIT 1"
        )
    )
    if q.scalar() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agric_satellite schema not installed",
        )


async def ensure_overview_cache_table(db: AsyncSession) -> None:
    """兼容旧部署初始化总览缓存表；同一API进程只执行一次DDL。"""
    global _overview_cache_ready
    if _overview_cache_ready:
        return
    async with _overview_cache_init_lock:
        if _overview_cache_ready:
            return
        # 使用独立事务提交DDL，避免调用方后续业务回滚时把初始化结果一起回滚。
        from app.core.database import engine

        async with engine.begin() as connection:
            await connection.execute(text(_ENSURE_CACHE_TABLE_SQL))
            await connection.execute(text(_ENSURE_CACHE_INDEX_SQL))
        _overview_cache_ready = True


def _region_where(
    level: OverviewLevel, code: str | None, name: str | None, params: dict[str, Any]
) -> str:
    """Build WHERE for parcels in the selected region (code preferred, else exact name)."""
    # 总览既有定时预聚合也有实时查询，统一在这里过滤不参与自动任务的地块，
    # 避免被排除地块重新进入统计链路或拖慢大地块的实时查询。
    schedule_filter = scheduled_land_sql("p")
    params["max_schedule_area_mu"] = MAX_SCHEDULE_LAND_AREA_MU
    if level == "country":
        return f"p.deleted_at IS NULL AND {schedule_filter}"
    code_col = _LEVEL_CODE_COL[level]
    name_col = _LEVEL_NAME_COL[level]
    clauses: list[str] = []
    if code:
        params["region_code"] = code.strip()
        padded = _pad_adcode(level, code)
        if padded and padded != code.strip():
            params["region_code_padded"] = padded
            clauses.append(
                f"(p.{code_col} = :region_code OR p.{code_col} = :region_code_padded)"
            )
        else:
            clauses.append(f"p.{code_col} = :region_code")
    if name:
        params["region_name"] = name.strip()
        clauses.append(f"p.{name_col} ILIKE :region_name")
    if not clauses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"level={level} requires code or name",
        )
    if len(clauses) == 1:
        return f"({clauses[0]}) AND p.deleted_at IS NULL AND {schedule_filter}"
    return " AND ".join(clauses + ["p.deleted_at IS NULL", schedule_filter])


async def _resolve_region_label(
    db: AsyncSession, level: OverviewLevel, code: str | None, name: str | None
) -> tuple[str | None, str]:
    """Return (code, name) for the current region."""
    if level == "country":
        return None, "全国"
    if name and code:
        return code, name
    code_col = _LEVEL_CODE_COL[level]
    name_col = _LEVEL_NAME_COL[level]
    params: dict[str, Any] = {}
    wh = _region_where(level, code, name, params)
    row = (
        await db.execute(
            text(
                f"""
                SELECT p.{code_col} AS code, p.{name_col} AS name
                FROM agric_satellite.land_parcels p
                WHERE {wh} AND p.{name_col} IS NOT NULL
                LIMIT 1
                """
            ),
            params,
        )
    ).fetchone()
    if row and row.name:
        return (str(row.code) if row.code else code), str(row.name)
    if name:
        return code, name
    if code:
        return code, code
    return code, "未知"


async def _build_path(
    db: AsyncSession, level: OverviewLevel, code: str | None, name: str | None
) -> list[dict[str, Any]]:
    path: list[dict[str, Any]] = [{"level": "country", "code": None, "name": "全国"}]
    if level == "country":
        return path

    params: dict[str, Any] = {}
    wh = _region_where(level, code, name, params)
    row = (
        await db.execute(
            text(
                f"""
                SELECT province_code, province_name, city_code, city_name,
                       county_code, county_name
                FROM agric_satellite.land_parcels p
                WHERE {wh}
                LIMIT 1
                """
            ),
            params,
        )
    ).fetchone()
    if not row:
        resolved_code, resolved_name = await _resolve_region_label(
            db, level, code, name
        )
        path.append({"level": level, "code": resolved_code, "name": resolved_name})
        return path

    path.append(
        {
            "level": "province",
            "code": str(row.province_code) if row.province_code else None,
            "name": row.province_name or "未知省",
        }
    )
    if level in ("city", "county"):
        path.append(
            {
                "level": "city",
                "code": str(row.city_code) if row.city_code else None,
                "name": row.city_name or "未知市",
            }
        )
    if level == "county":
        path.append(
            {
                "level": "county",
                "code": str(row.county_code) if row.county_code else None,
                "name": row.county_name or "未知县",
            }
        )
    return path


def _stats_to_dict(out: OverviewStatsOut) -> dict[str, Any]:
    return out.model_dump(mode="json", by_alias=True)


def _stats_from_cache_json(payload: dict[str, Any]) -> OverviewStatsOut:
    return OverviewStatsOut.model_validate(payload)


async def _read_cache(
    db: AsyncSession,
    *,
    level: OverviewLevel,
    region_code: str | None,
    from_d: date,
    to_d: date,
    crop_key: str | None,
) -> OverviewStatsOut | None:
    """Return cached stats if a fresh row exists (updated_at within 36h)."""
    await ensure_overview_cache_table(db)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_CACHE_FRESH_HOURS)
    row = (
        await db.execute(
            text(
                """
                SELECT metric_json, updated_at
                FROM agric_satellite.overview_stats_daily
                WHERE level = :level
                  AND region_code = :region_code
                  AND window_from = :from_d
                  AND window_to = :to_d
                  AND crop = :crop
                  AND updated_at >= :cutoff
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ),
            {
                "level": level,
                "region_code": region_code or "",
                "from_d": from_d,
                "to_d": to_d,
                "crop": crop_key or "",
                "cutoff": cutoff,
            },
        )
    ).fetchone()
    if not row:
        return None
    payload = row.metric_json
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    try:
        out = _stats_from_cache_json(payload)
    except Exception:
        return None
    # Annotate source
    filters = dict(out.filters or {})
    filters["drought_source"] = "cache"
    filters["cache_hit"] = True
    filters["cache_updated_at"] = row.updated_at.isoformat() if row.updated_at else None
    return out.model_copy(update={"filters": filters})


async def _compute_live_stats(
    db: AsyncSession,
    *,
    level: OverviewLevel,
    code: str | None,
    name: str | None,
    from_d: date,
    to_d: date,
    crop: str | None,
    allow_pixels: bool,
    parcel_facts: dict[str, dict[str, Any]] | None = None,
    parcel_batch_size: int | None = None,
) -> OverviewStatsOut:
    """Compute live overview stats; pixel drought when allow_pixels and data exists.

    ``parcel_batch_size`` is used by the internal pre-aggregation job to avoid
    loading every parcel and its scene rows into one database result set.  The
    public live endpoint keeps the historical one-batch behavior when it is
    omitted.
    """
    crop_key = normalize_crop_key(crop) if crop else None
    pheno_months = _phenology_months(crop)

    params: dict[str, Any] = {
        "from_d": from_d,
        "to_d": to_d,
        "cloud_max": CLOUD_MAX_PCT,
        "weak_ndvi": WEAK_NDVI_LT,
    }
    region_wh = _region_where(level, code, name, params)
    month_wh = _month_in_clause(pheno_months, params)

    area_by_land: dict[str, float] = {}
    total_area = 0.0
    total_count = 0
    drought_counts = {"severe": 0, "moderate": 0, "mild": 0, "normal": 0, "unknown": 0}
    drought_area = {k: 0.0 for k in drought_counts}
    flood_keys = ("flood_severe", "flood_moderate", "flood_mild", "dry", "unknown")
    flood_counts = {k: 0 for k in flood_keys}
    flood_area = {k: 0.0 for k in flood_keys}
    weak_lands: set[str] = set()

    child_level = _CHILD_LEVEL[level]
    child_agg: dict[tuple[str | None, str], dict[str, Any]] = {}

    def _child_key(row: Any) -> tuple[str | None, str] | None:
        if child_level is None:
            return None
        if child_level == "province":
            c, n = row.province_code, row.province_name
        elif child_level == "city":
            c, n = row.city_code, row.city_name
        else:
            c, n = row.county_code, row.county_name
        if not n:
            return None
        return (str(c) if c else None, str(n))

    land_to_child: dict[str, tuple[str | None, str]] = {}

    drought_source: Literal["pixels", "scene_avg", "cache"] = "scene_avg"
    pixels_used = 0
    pixels_parcels = 0
    ranks = {None: -1, "dry": 0, "watch": 1, "flood_moderate": 2, "flood_severe": 3}

    # 内部预聚合按 land_id 游标分页，避免 OFFSET 越翻越慢，也避免一次把所有地块和像素 JSONB 拉进内存。
    land_cursor: str | None = None
    while True:
        page_params = dict(params)
        page_region_wh = region_wh
        if land_cursor is not None:
            page_region_wh += " AND p.land_id > :land_cursor"
            page_params["land_cursor"] = land_cursor
        page_limit = ""
        if parcel_batch_size is not None:
            page_limit = " LIMIT :parcel_limit"
            page_params["parcel_limit"] = parcel_batch_size

        parcels = (
            await db.execute(
                text(
                    f"""
                    SELECT p.land_id,
                           coalesce(p.land_area_mu, 0)::float AS area_mu,
                           p.province_code, p.province_name,
                           p.city_code, p.city_name,
                           p.county_code, p.county_name
                    FROM agric_satellite.land_parcels p
                    WHERE {page_region_wh}
                    ORDER BY p.land_id{page_limit}
                    """
                ),
                page_params,
            )
        ).fetchall()
        if not parcels:
            break

        total_count += len(parcels)
        total_area += sum(float(row.area_mu or 0) for row in parcels)
        for row in parcels:
            area_by_land[row.land_id] = float(row.area_mu or 0)

        # 每日批次复用同一次分类构建各级快照，保留当时区划与面积，避免历史受地块修改影响。
        if parcel_facts is not None:
            for row in parcels:
                parcel_facts[row.land_id] = {
                    **dict(row._mapping),
                    "drought": "unknown",
                    "flood": "unknown",
                    "weak": False,
                    "s1_date": None,
                    "s2_date": None,
                }

        for row in parcels:
            ck = _child_key(row)
            if ck is None:
                continue
            land_to_child[row.land_id] = ck
            if ck not in child_agg:
                child_agg[ck] = {
                    "level": child_level,
                    "code": ck[0],
                    "name": ck[1],
                    "parcel_count": 0,
                    "area_mu": 0.0,
                    "drought_severe": 0,
                    "drought_alert": 0,
                    "flood": 0,
                    "flood_alert": 0,
                    "weak_growth": 0,
                }
            child_agg[ck]["parcel_count"] += 1
            child_agg[ck]["area_mu"] += float(row.area_mu or 0)

        # 每个场景查询只绑定当前页的地块，避免 IN 查询重新扫描整个区域的数据。
        land_params = {f"land_{idx}": row.land_id for idx, row in enumerate(parcels)}
        land_sql = ", ".join(f":{key}" for key in land_params)
        batch_params = dict(params)
        batch_params.update(land_params)
        batch_region = f"{region_wh} AND s.land_id IN ({land_sql})"

        # Latest clear S2 — optionally include pixel_data for sub-country levels.
        pixel_col = ", s.pixel_data" if allow_pixels else ""
        s2_rows = (
            await db.execute(
                text(
                    f"""
                    SELECT DISTINCT ON (s.land_id)
                           s.land_id, s.date, s.ndvi_avg, s.ndmi_avg
                           {pixel_col}
                    FROM agric_satellite.parcel_scene_products s
                    JOIN agric_satellite.land_parcels p ON p.land_id = s.land_id
                    WHERE {batch_region}
                      AND s.sensor = 'S2'
                      AND s.date >= :from_d AND s.date <= :to_d
                      AND {official_s2_sql("s")}
                    ORDER BY s.land_id, s.date DESC, s.scene_id
                    """
                ),
                batch_params,
            )
        ).fetchall()

        for r in s2_rows:
            date_str = (
                r.date.isoformat() if hasattr(r.date, "isoformat") else str(r.date)
            )
            if not is_drought_season(date_str):
                continue
            cls = None
            if allow_pixels:
                pdata = getattr(r, "pixel_data", None)
                if pdata is not None:
                    maj, _severe_share, n = classify_drought_from_pixels(pdata)
                    if maj is not None and n > 0:
                        cls = maj
                        pixels_used += n
                        pixels_parcels += 1
            if cls is None:
                cls = classify_drought(r.ndvi_avg, r.ndmi_avg)
            if cls is None:
                continue
            # 快照确认：NDMI 干旱阈值不满足时，不保留轻/中/重旱分类。
            if cls in ("mild", "moderate", "severe"):
                ndmi = r.ndmi_avg
                if ndmi is None or float(ndmi) >= NDMI_DRY_ABS:
                    cls = "normal"
            drought_counts[cls] += 1
            if parcel_facts is not None:
                parcel_facts[r.land_id].update(drought=cls, s2_date=date_str)
            drought_area[cls] += area_by_land.get(r.land_id, 0.0)
            ck = land_to_child.get(r.land_id)
            if ck and ck in child_agg:
                if cls == "severe":
                    child_agg[ck]["drought_severe"] += 1
                if cls in ("severe", "moderate", "mild"):
                    child_agg[ck]["drought_alert"] += 1

        if allow_pixels and pixels_parcels > 0:
            drought_source = "pixels"

        # 洪涝必须比较近期历史基线；每页内保留该页地块的完整 S1 序列。
        s1_rows = (
            await db.execute(
                text(
                    f"""
                    SELECT s.land_id, s.date, s.vv_avg, s.vh_avg, s.scene_id,
                           s.pixel_data->>'relative_orbit' AS relative_orbit
                    FROM agric_satellite.parcel_scene_products s
                    JOIN agric_satellite.land_parcels p ON p.land_id = s.land_id
                    WHERE {batch_region}
                      AND s.sensor = 'S1'
                      AND s.date >= :from_d AND s.date <= :to_d
                    ORDER BY s.land_id, s.date, s.scene_id
                    """
                ),
                batch_params,
            )
        ).fetchall()

        s1_by_land = defaultdict(list)
        for row in s1_rows:
            s1_by_land[row.land_id].append(
                {
                    "date": str(row.date)[:10],
                    "vv": row.vv_avg,
                    "vh": row.vh_avg,
                    "scene_id": getattr(row, "scene_id", None),
                    "relative_orbit": getattr(row, "relative_orbit", None),
                }
            )
        for land_id, observations in s1_by_land.items():
            classified = [
                (day, cls)
                for day, cls in classify_flood_series(observations)
                if cls is not None
            ]
            if not classified:
                continue
            # 同日多轨道景使用最高关注等级；基线和待判景都限制在统计日之前，避免未来观测泄漏。
            scene_date, cls = max(
                classified, key=lambda item: (item[0], ranks[item[1]])
            )
            bucket = overview_flood_bucket(cls)
            if bucket is None:
                continue
            flood_counts[bucket] += 1
            if parcel_facts is not None:
                parcel_facts[land_id].update(flood=bucket, s1_date=scene_date)
            flood_area[bucket] += area_by_land.get(land_id, 0.0)
            ck = land_to_child.get(land_id)
            if ck and ck in child_agg:
                if is_open_water_flood(cls):
                    child_agg[ck]["flood"] += 1
                if is_flood_alert(cls):
                    child_agg[ck]["flood_alert"] += 1

        weak_rows = (
            await db.execute(
                text(
                    f"""
                    SELECT s.land_id,
                           avg(s.ndvi_avg)::float AS mean_ndvi
                    FROM agric_satellite.parcel_scene_products s
                    JOIN agric_satellite.land_parcels p ON p.land_id = s.land_id
                    WHERE {batch_region}
                      AND s.sensor = 'S2'
                      AND s.date >= :from_d AND s.date <= :to_d
                      AND {month_wh}
                      AND s.ndvi_avg IS NOT NULL
                      AND {official_s2_sql("s")}
                    GROUP BY s.land_id
                    HAVING avg(s.ndvi_avg) < :weak_ndvi
                    """
                ),
                batch_params,
            )
        ).fetchall()

        for r in weak_rows:
            weak_lands.add(r.land_id)
            if parcel_facts is not None:
                parcel_facts[r.land_id]["weak"] = True
            ck = land_to_child.get(r.land_id)
            if ck and ck in child_agg:
                child_agg[ck]["weak_growth"] += 1

        if parcel_batch_size is None:
            break
        land_cursor = str(parcels[-1].land_id)

    drought_counts["unknown"] = total_count - sum(
        drought_counts[k] for k in ("severe", "moderate", "mild", "normal")
    )
    drought_area["unknown"] = total_area - sum(
        drought_area[k] for k in ("severe", "moderate", "mild", "normal")
    )
    flood_known = ("flood_severe", "flood_moderate", "flood_mild", "dry")
    flood_counts["unknown"] = total_count - sum(flood_counts[k] for k in flood_known)
    flood_area["unknown"] = total_area - sum(flood_area[k] for k in flood_known)

    weak_area = sum(area_by_land[lid] for lid in weak_lands)

    resolved_code, resolved_name = await _resolve_region_label(db, level, code, name)
    path = await _build_path(db, level, code, name)

    open_water = flood_counts["flood_severe"] + flood_counts["flood_moderate"]
    mild = flood_counts["flood_mild"]

    children = [
        OverviewChildOut(
            level=v["level"],
            code=_pad_adcode(v["level"], v["code"]),
            name=v["name"],
            parcel_count=v["parcel_count"],
            drought_severe=v["drought_severe"],
            drought_alert=v["drought_alert"],
            flood=v["flood"],
            flood_alert=v["flood_alert"],
            weak_growth=v["weak_growth"],
            area_mu=round(v["area_mu"], 2),
            drought_ratio=_affected_ratio(v["drought_alert"], v["parcel_count"]),
            flood_ratio=_affected_ratio(v["flood_alert"], v["parcel_count"]),
            weak_growth_ratio=_affected_ratio(v["weak_growth"], v["parcel_count"]),
        )
        for v in sorted(
            child_agg.values(), key=lambda x: (-x["parcel_count"], x["name"])
        )
    ]

    flood_area_out = {
        "flood_severe": round(flood_area["flood_severe"], 2),
        "flood_moderate": round(flood_area["flood_moderate"], 2),
        "flood_mild": round(flood_area["flood_mild"], 2),
        "flood": round(flood_area["flood_severe"] + flood_area["flood_moderate"], 2),
        "wet": round(flood_area["flood_mild"], 2),
        "dry": round(flood_area["dry"], 2),
        "unknown": round(flood_area["unknown"], 2),
    }

    return OverviewStatsOut(
        region={
            "level": level,
            "code": resolved_code,
            "name": resolved_name,
            "path": path,
            "adcode": _pad_adcode(level, resolved_code)
            if level != "country"
            else "100000",
        },
        filters={
            "from": from_d.isoformat(),
            "to": to_d.isoformat(),
            "crop": crop_key,
            "cloud_max_pct": CLOUD_MAX_PCT,
            "phenology_months": pheno_months,
            "weak_ndvi_lt": WEAK_NDVI_LT,
            "drought_source": drought_source,
            "cache_hit": False,
            "pixels_parcels": pixels_parcels if allow_pixels else 0,
            "pixels_classified": pixels_used if allow_pixels else 0,
        },
        totals=OverviewTotals(parcel_count=total_count, area_mu=round(total_area, 2)),
        drought=OverviewDroughtCounts(
            severe=drought_counts["severe"],
            moderate=drought_counts["moderate"],
            mild=drought_counts["mild"],
            normal=drought_counts["normal"],
            unknown=drought_counts["unknown"],
            area_mu={k: round(v, 2) for k, v in drought_area.items()},
        ),
        flood=OverviewFloodCounts(
            flood_severe=flood_counts["flood_severe"],
            flood_moderate=flood_counts["flood_moderate"],
            flood_mild=mild,
            flood=open_water,
            wet=mild,
            dry=flood_counts["dry"],
            unknown=flood_counts["unknown"],
            area_mu=flood_area_out,
        ),
        weak_growth=OverviewWeakGrowth(
            parcel_count=len(weak_lands), area_mu=round(weak_area, 2)
        ),
        children=children,
    )


@router.get("/overview/stats", response_model=OverviewStatsOut)
async def overview_stats(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    level: OverviewLevel = Query("country"),
    code: str | None = Query(None),
    name: str | None = Query(None),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    crop: str | None = Query(
        None,
        description="Crop key (e.g. corn) — sets phenology months for weak-growth only; does not filter parcels by planted crop.",
    ),
    use_cache: bool | None = Query(
        None,
        description="Prefer pre-agg cache when fresh. Default true for level=country.",
    ),
    live: int = Query(
        0,
        ge=0,
        le=1,
        description="Force live compute (skip cache). live=1 disables cache.",
    ),
):
    """Aggregate drought / flood / weak-growth stats for a China admin region."""
    await _agri_ready(db)
    default_from, default_to = _default_window()
    from_d = from_ or default_from
    to_d = to or default_to
    if from_d > to_d:
        raise HTTPException(status_code=400, detail="from must be <= to")

    crop_key = normalize_crop_key(crop) if crop else None
    prefer_cache = (use_cache if use_cache is not None else (level == "country")) and (
        live != 1
    )

    if prefer_cache:
        cached = await _read_cache(
            db,
            level=level,
            region_code=code,
            from_d=from_d,
            to_d=to_d,
            crop_key=crop_key,
        )
        if cached is not None:
            return cached

    # Country live: scene averages only (avoid loading all pixel_data blobs → OOM).
    # Province/city/county: pixel-level drought when pixel_data present.
    allow_pixels = level != "country"
    return await _compute_live_stats(
        db,
        level=level,
        code=code,
        name=name,
        from_d=from_d,
        to_d=to_d,
        crop=crop,
        allow_pixels=allow_pixels,
    )


@router.get("/overview/daily")
async def overview_daily(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    level: OverviewLevel = Query("country"),
    code: str | None = Query(None),
    name: str | None = Query(None),
    as_of: date | None = Query(None),
) -> dict[str, Any]:
    """今日/历史快照及该统计日的下载进度，不将旧观测标为当天数据。"""
    if as_of and as_of > business_today():
        raise HTTPException(400, "统计日期不能晚于当天")
    stats = await read_daily_snapshot(
        db, level=level, code=code, name=name, as_of=as_of
    )
    run = await db.get(Job, run_id_for(as_of or business_today()))
    summary = run_summary(run) if run else None
    if summary:
        # 公共页面只展示批次进度，庞大的派发编号和内部地块列表留在Internal接口。
        summary = {
            key: summary[key]
            for key in (
                "run_id",
                "as_of_date",
                "status",
                "phase",
                "lands_checked",
                "group_count",
                "job_count",
                "pending_jobs",
                "failed_jobs",
                "failed_land_count",
                "results_pending",
                "error",
            )
            if key in summary
        }
    from agric_satellite_analysis_common.settings import settings as common_settings

    return {
        "stats": stats.model_dump(mode="json") if stats else None,
        "run": summary,
        "today": business_today().isoformat(),
        "schedule": {
            "enabled": common_settings.schedule_daily_satellite_enabled,
            "time": "19:15",
            "timezone": "Asia/Shanghai",
        },
    }


@router.get("/overview/history")
async def overview_history(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    level: OverviewLevel = Query("country"),
    code: str | None = Query(None),
    name: str | None = Query(None),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
) -> dict[str, Any]:
    """每日保存结果的趋势；缺失的快照日期不补零。"""
    end = to or business_today()
    start = from_ or end - timedelta(days=30)
    if start > end or (end - start).days > 366:
        raise HTTPException(400, "历史查询范围须在0～366天之间")
    items = await read_daily_history(
        db, level=level, code=code, name=name, from_d=start, to_d=end
    )
    return {"items": items, "from": start.isoformat(), "to": end.isoformat()}


def _csv_response(filename: str, rows: list[dict[str, Any]]) -> Response:
    buf = io.StringIO()
    # UTF-8 BOM for Excel
    buf.write("\ufeff")
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    else:
        buf.write("")
    data = buf.getvalue().encode("utf-8")
    # ASCII fallback + RFC 5987 for non-ASCII names (Starlette headers are latin-1)
    safe = "".join(ch if ord(ch) < 128 else "_" for ch in filename) or "export.csv"
    if not safe.endswith(".csv"):
        safe = f"{safe}.csv"
    disp = f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": disp},
    )


@router.get("/overview/export/stats.csv")
async def overview_export_stats_csv(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    level: OverviewLevel = Query("country"),
    code: str | None = Query(None),
    name: str | None = Query(None),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    crop: str | None = Query(None),
    live: int = Query(0, ge=0, le=1),
    daily: bool = Query(False),
    as_of: date | None = Query(None),
):
    """CSV of children rows from overview stats (UTF-8 BOM)."""
    if daily:
        stats = await read_daily_snapshot(
            db, level=level, code=code, name=name, as_of=as_of
        )
        if stats is None:
            raise HTTPException(404, "该日期没有保存的态势快照")
    else:
        stats = await overview_stats(
            ctx=ctx,
            db=db,
            level=level,
            code=code,
            name=name,
            from_=from_,
            to=to,
            crop=crop,
            use_cache=None,
            live=live,
        )
    rows = [
        {
            "level": c.level,
            "code": c.code or "",
            "name": c.name,
            "parcel_count": c.parcel_count,
            "drought_severe": c.drought_severe,
            "drought_alert": c.drought_alert,
            "flood": c.flood,
            "flood_alert": c.flood_alert,
            "weak_growth": c.weak_growth,
            "area_mu": c.area_mu,
        }
        for c in stats.children
    ]
    region = (stats.region or {}).get("name") or level
    return _csv_response(f"overview-stats-{region}.csv", rows)


@router.get("/overview/export/weak-parcels.csv")
async def overview_export_weak_parcels_csv(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    level: OverviewLevel = Query("country"),
    code: str | None = Query(None),
    name: str | None = Query(None),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    crop: str | None = Query(None),
    limit: int = Query(5000, ge=1, le=5000),
):
    """CSV of weak-growth parcels (same filters as weak-parcels; limit ≤5000)."""
    res = await overview_weak_parcels(
        ctx=ctx,
        db=db,
        level=level,
        code=code,
        name=name,
        from_=from_,
        to=to,
        crop=crop,
        limit=limit,
        offset=0,
    )
    rows = [
        {
            "land_id": it.land_id,
            "land_name": it.land_name or "",
            "province_name": it.province_name or "",
            "city_name": it.city_name or "",
            "county_name": it.county_name or "",
            "land_area_mu": it.land_area_mu,
            "ndvi_avg": it.ndvi_avg,
            "scene_date": it.scene_date.isoformat() if it.scene_date else "",
            "cloud_pct": it.cloud_pct if it.cloud_pct is not None else "",
        }
        for it in res.items
    ]
    return _csv_response("overview-weak-parcels.csv", rows)


@router.get("/overview/regions", response_model=OverviewRegionsOut)
async def overview_regions(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    parent_level: OverviewLevel | None = Query(None),
    parent_code: str | None = Query(None),
    parent_name: str | None = Query(None),
):
    """List child regions under a parent (for map labels)."""
    await _agri_ready(db)
    pl: OverviewLevel = parent_level or "country"
    child_level = _CHILD_LEVEL.get(pl)
    if child_level is None:
        return OverviewRegionsOut(
            parent_level=pl,
            parent_code=parent_code,
            parent_name=parent_name,
            children=[],
        )

    params: dict[str, Any] = {}
    if pl == "country":
        # 全国根节点也必须沿用自动任务过滤条件，避免被排除地块出现在下钻汇总中。
        wh = scheduled_land_sql("p")
        params["max_schedule_area_mu"] = MAX_SCHEDULE_LAND_AREA_MU
    else:
        wh = _region_where(pl, parent_code, parent_name, params)

    code_col = _LEVEL_CODE_COL[child_level]
    name_col = _LEVEL_NAME_COL[child_level]
    rows = (
        await db.execute(
            text(
                f"""
                SELECT p.{code_col} AS code,
                       p.{name_col} AS name,
                       count(*)::int AS parcel_count,
                       coalesce(sum(p.land_area_mu), 0)::float AS area_mu
                FROM agric_satellite.land_parcels p
                WHERE {wh} AND p.{name_col} IS NOT NULL
                GROUP BY p.{code_col}, p.{name_col}
                ORDER BY parcel_count DESC, name
                """
            ),
            params,
        )
    ).fetchall()

    children = [
        OverviewRegionOut(
            level=child_level,
            code=str(r.code) if r.code else None,
            name=str(r.name),
            parcel_count=int(r.parcel_count),
            area_mu=round(float(r.area_mu or 0), 2),
        )
        for r in rows
    ]
    return OverviewRegionsOut(
        parent_level=pl,
        parent_code=parent_code,
        parent_name=parent_name or ("全国" if pl == "country" else None),
        children=children,
    )


@router.get("/overview/weak-parcels", response_model=OverviewWeakParcelsOut)
async def overview_weak_parcels(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    level: OverviewLevel = Query("country"),
    code: str | None = Query(None),
    name: str | None = Query(None),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    crop: str | None = Query(
        None,
        description="Crop key — phenology months for weak-growth only (no parcel crop filter).",
    ),
    limit: int = Query(50, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    """List parcels with weak growth (clear S2 mean NDVI in phenology months < threshold)."""
    await _agri_ready(db)
    default_from, default_to = _default_window()
    from_d = from_ or default_from
    to_d = to or default_to
    if from_d > to_d:
        raise HTTPException(status_code=400, detail="from must be <= to")

    pheno_months = _phenology_months(crop)
    params: dict[str, Any] = {
        "from_d": from_d,
        "to_d": to_d,
        "cloud_max": CLOUD_MAX_PCT,
        "weak_ndvi": WEAK_NDVI_LT,
        "limit": limit,
        "offset": offset,
    }
    region_wh = _region_where(level, code, name, params)
    month_wh = _month_in_clause(pheno_months, params)

    clear_s2 = f"""
        s.sensor = 'S2'
        AND s.date >= :from_d AND s.date <= :to_d
        AND {month_wh}
        AND s.ndvi_avg IS NOT NULL
        AND {official_s2_sql("s")}
    """

    total_row = (
        await db.execute(
            text(
                f"""
                SELECT count(*)::int AS total
                FROM (
                    SELECT s.land_id
                    FROM agric_satellite.parcel_scene_products s
                    JOIN agric_satellite.land_parcels p ON p.land_id = s.land_id
                    WHERE {region_wh}
                      AND {clear_s2}
                    GROUP BY s.land_id
                    HAVING avg(s.ndvi_avg) < :weak_ndvi
                ) w
                """
            ),
            params,
        )
    ).fetchone()
    total = int(total_row.total) if total_row else 0

    rows = (
        await db.execute(
            text(
                f"""
                WITH weak AS (
                    SELECT s.land_id,
                           avg(s.ndvi_avg)::float AS ndvi_avg
                    FROM agric_satellite.parcel_scene_products s
                    JOIN agric_satellite.land_parcels p ON p.land_id = s.land_id
                    WHERE {region_wh}
                      AND {clear_s2}
                    GROUP BY s.land_id
                    HAVING avg(s.ndvi_avg) < :weak_ndvi
                ),
                latest AS (
                    SELECT DISTINCT ON (s.land_id)
                           s.land_id,
                           s.date AS scene_date,
                           coalesce(s.parcel_cloud_cover_pct, s.cloud_cover)::float AS cloud_pct
                    FROM agric_satellite.parcel_scene_products s
                    JOIN weak w ON w.land_id = s.land_id
                    JOIN agric_satellite.land_parcels p ON p.land_id = s.land_id
                    WHERE {region_wh}
                      AND {clear_s2}
                    ORDER BY s.land_id, s.date DESC
                )
                SELECT w.land_id,
                       p.land_name,
                       p.province_name,
                       p.city_name,
                       p.county_name,
                       coalesce(p.land_area_mu, 0)::float AS land_area_mu,
                       w.ndvi_avg,
                       l.scene_date,
                       l.cloud_pct
                FROM weak w
                JOIN agric_satellite.land_parcels p ON p.land_id = w.land_id
                LEFT JOIN latest l ON l.land_id = w.land_id
                ORDER BY w.ndvi_avg ASC, coalesce(p.land_area_mu, 0) DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
    ).fetchall()

    items = [
        OverviewWeakParcelOut(
            land_id=str(r.land_id),
            land_name=r.land_name,
            province_name=r.province_name,
            city_name=r.city_name,
            county_name=r.county_name,
            land_area_mu=round(float(r.land_area_mu or 0), 2),
            ndvi_avg=round(float(r.ndvi_avg), 4),
            scene_date=r.scene_date,
            cloud_pct=round(float(r.cloud_pct), 2) if r.cloud_pct is not None else None,
        )
        for r in rows
    ]
    return OverviewWeakParcelsOut(total=total, items=items)
