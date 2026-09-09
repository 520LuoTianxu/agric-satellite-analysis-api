"""China overview (全国态势): country→province→city→county stats."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agri_classify import (
    CLOUD_MAX_PCT,
    PHENOLOGY_MONTHS,
    WEAK_NDVI_LT,
    classify_drought,
    classify_flood,
)
from app.core.crops import get_crop_season, normalize_crop_key
from app.core.database import get_db
from app.middleware.auth import OrgContext, require_roles
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


async def _agri_ready(db: AsyncSession) -> None:
    q = await db.execute(
        text(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'agri' LIMIT 1"
        )
    )
    if q.scalar() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agri schema not installed",
        )


def _region_where(
    level: OverviewLevel,
    code: str | None,
    name: str | None,
    params: dict[str, Any],
) -> str:
    """Build WHERE for parcels in the selected region (code preferred, else exact name)."""
    if level == "country":
        return "TRUE"
    code_col = _LEVEL_CODE_COL[level]
    name_col = _LEVEL_NAME_COL[level]
    clauses: list[str] = []
    if code:
        params["region_code"] = code.strip()
        # Also accept padded 6-digit forms for province/city
        padded = _pad_adcode(level, code)
        if padded and padded != code.strip():
            params["region_code_padded"] = padded
            # Match short or padded: e.g. province 37 OR 370000 stored oddly
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
    # Prefer code when both given: AND them for precision; if code sparse, name alone works
    if len(clauses) == 1:
        return clauses[0]
    # Both code and name: require both (precise); callers may pass only one.
    return " AND ".join(clauses)


async def _resolve_region_label(
    db: AsyncSession,
    level: OverviewLevel,
    code: str | None,
    name: str | None,
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
                FROM agri.land_parcels p
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
    db: AsyncSession,
    level: OverviewLevel,
    code: str | None,
    name: str | None,
) -> list[dict[str, Any]]:
    path: list[dict[str, Any]] = [{"level": "country", "code": None, "name": "全国"}]
    if level == "country":
        return path

    # Load one parcel matching region to fill ancestors
    params: dict[str, Any] = {}
    wh = _region_where(level, code, name, params)
    row = (
        await db.execute(
            text(
                f"""
                SELECT province_code, province_name, city_code, city_name,
                       county_code, county_name
                FROM agri.land_parcels p
                WHERE {wh}
                LIMIT 1
                """
            ),
            params,
        )
    ).fetchone()
    if not row:
        # Still show requested node
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
):
    """Aggregate drought / flood / weak-growth stats for a China admin region."""
    await _agri_ready(db)
    default_from, default_to = _default_window()
    from_d = from_ or default_from
    to_d = to or default_to
    if from_d > to_d:
        raise HTTPException(status_code=400, detail="from must be <= to")

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

    # Parcels in region
    parcels = (
        await db.execute(
            text(
                f"""
                SELECT p.land_id,
                       coalesce(p.land_area_mu, 0)::float AS area_mu,
                       p.province_code, p.province_name,
                       p.city_code, p.city_name,
                       p.county_code, p.county_name
                FROM agri.land_parcels p
                WHERE {region_wh}
                """
            ),
            params,
        )
    ).fetchall()

    parcel_ids = [r.land_id for r in parcels]
    area_by_land = {r.land_id: float(r.area_mu or 0) for r in parcels}
    total_area = sum(area_by_land.values())
    total_count = len(parcels)

    drought_counts = {"severe": 0, "moderate": 0, "mild": 0, "normal": 0, "unknown": 0}
    drought_area = {k: 0.0 for k in drought_counts}
    flood_counts = {"flood": 0, "wet": 0, "dry": 0, "unknown": 0}
    flood_area = {k: 0.0 for k in flood_counts}
    weak_lands: set[str] = set()
    drought_by_land: dict[str, str] = {}
    flood_by_land: dict[str, str] = {}

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

    for row in parcels:
        ck = _child_key(row)
        if ck is None:
            continue
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
                "weak_growth": 0,
            }
        child_agg[ck]["parcel_count"] += 1
        child_agg[ck]["area_mu"] += float(row.area_mu or 0)

    land_to_child: dict[str, tuple[str | None, str]] = {}
    for row in parcels:
        ck = _child_key(row)
        if ck is not None:
            land_to_child[row.land_id] = ck

    if parcel_ids:
        # Latest clear S2 per parcel in window (join region parcels — no ANY array bind)
        s2_rows = (
            await db.execute(
                text(
                    f"""
                    SELECT DISTINCT ON (s.land_id)
                           s.land_id, s.ndvi_avg, s.ndmi_avg
                    FROM agri.parcel_scene_products s
                    JOIN agri.land_parcels p ON p.land_id = s.land_id
                    WHERE {region_wh}
                      AND s.sensor = 'S2'
                      AND s.date >= :from_d AND s.date <= :to_d
                      AND NOT (
                          coalesce(s.parcel_cloud_cover_pct, s.cloud_cover) > :cloud_max
                          OR s.cloud_cover_over_30 IS TRUE
                      )
                    ORDER BY s.land_id, s.date DESC
                    """
                ),
                params,
            )
        ).fetchall()

        for r in s2_rows:
            cls = classify_drought(r.ndvi_avg, r.ndmi_avg)
            if cls is None:
                continue
            drought_by_land[r.land_id] = cls
            drought_counts[cls] += 1
            drought_area[cls] += area_by_land.get(r.land_id, 0.0)
            ck = land_to_child.get(r.land_id)
            if ck and ck in child_agg:
                if cls == "severe":
                    child_agg[ck]["drought_severe"] += 1
                if cls in ("severe", "moderate", "mild"):
                    child_agg[ck]["drought_alert"] += 1

        # Latest S1 per parcel (no cloud filter)
        s1_rows = (
            await db.execute(
                text(
                    f"""
                    SELECT DISTINCT ON (s.land_id)
                           s.land_id, s.vv_avg, s.vh_avg
                    FROM agri.parcel_scene_products s
                    JOIN agri.land_parcels p ON p.land_id = s.land_id
                    WHERE {region_wh}
                      AND s.sensor = 'S1'
                      AND s.date >= :from_d AND s.date <= :to_d
                    ORDER BY s.land_id, s.date DESC
                    """
                ),
                params,
            )
        ).fetchall()

        for r in s1_rows:
            cls = classify_flood(r.vv_avg, r.vh_avg)
            if cls is None:
                continue
            flood_by_land[r.land_id] = cls
            flood_counts[cls] += 1
            flood_area[cls] += area_by_land.get(r.land_id, 0.0)
            ck = land_to_child.get(r.land_id)
            if ck and cls == "flood" and ck in child_agg:
                child_agg[ck]["flood"] += 1

        # Weak growth: mean ndvi of clear in-season S2 < threshold
        weak_rows = (
            await db.execute(
                text(
                    f"""
                    SELECT s.land_id,
                           avg(s.ndvi_avg)::float AS mean_ndvi
                    FROM agri.parcel_scene_products s
                    JOIN agri.land_parcels p ON p.land_id = s.land_id
                    WHERE {region_wh}
                      AND s.sensor = 'S2'
                      AND s.date >= :from_d AND s.date <= :to_d
                      AND {month_wh}
                      AND s.ndvi_avg IS NOT NULL
                      AND NOT (
                          coalesce(s.parcel_cloud_cover_pct, s.cloud_cover) > :cloud_max
                          OR s.cloud_cover_over_30 IS TRUE
                      )
                    GROUP BY s.land_id
                    HAVING avg(s.ndvi_avg) < :weak_ndvi
                    """
                ),
                params,
            )
        ).fetchall()

        for r in weak_rows:
            weak_lands.add(r.land_id)
            ck = land_to_child.get(r.land_id)
            if ck and ck in child_agg:
                child_agg[ck]["weak_growth"] += 1

    # Unknown = parcels with no classifiable scene
    drought_counts["unknown"] = total_count - sum(
        drought_counts[k] for k in ("severe", "moderate", "mild", "normal")
    )
    drought_area["unknown"] = total_area - sum(
        drought_area[k] for k in ("severe", "moderate", "mild", "normal")
    )
    flood_counts["unknown"] = total_count - sum(
        flood_counts[k] for k in ("flood", "wet", "dry")
    )
    flood_area["unknown"] = total_area - sum(
        flood_area[k] for k in ("flood", "wet", "dry")
    )

    weak_area = sum(area_by_land[lid] for lid in weak_lands)

    resolved_code, resolved_name = await _resolve_region_label(db, level, code, name)
    path = await _build_path(db, level, code, name)

    children = [
        OverviewChildOut(
            level=v["level"],
            code=v["code"],
            name=v["name"],
            parcel_count=v["parcel_count"],
            drought_severe=v["drought_severe"],
            drought_alert=v["drought_alert"],
            flood=v["flood"],
            weak_growth=v["weak_growth"],
            area_mu=round(v["area_mu"], 2),
        )
        for v in sorted(
            child_agg.values(), key=lambda x: (-x["parcel_count"], x["name"])
        )
    ]

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
            flood=flood_counts["flood"],
            wet=flood_counts["wet"],
            dry=flood_counts["dry"],
            unknown=flood_counts["unknown"],
            area_mu={k: round(v, 2) for k, v in flood_area.items()},
        ),
        weak_growth=OverviewWeakGrowth(
            parcel_count=len(weak_lands), area_mu=round(weak_area, 2)
        ),
        children=children,
    )


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
    # Default: country children = provinces
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
        wh = "TRUE"
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
                FROM agri.land_parcels p
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
    limit: int = Query(50, ge=1, le=200),
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
        AND NOT (
            coalesce(s.parcel_cloud_cover_pct, s.cloud_cover) > :cloud_max
            OR s.cloud_cover_over_30 IS TRUE
        )
    """

    total_row = (
        await db.execute(
            text(
                f"""
                SELECT count(*)::int AS total
                FROM (
                    SELECT s.land_id
                    FROM agri.parcel_scene_products s
                    JOIN agri.land_parcels p ON p.land_id = s.land_id
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
                    FROM agri.parcel_scene_products s
                    JOIN agri.land_parcels p ON p.land_id = s.land_id
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
                    FROM agri.parcel_scene_products s
                    JOIN weak w ON w.land_id = s.land_id
                    JOIN agri.land_parcels p ON p.land_id = s.land_id
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
                JOIN agri.land_parcels p ON p.land_id = w.land_id
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
