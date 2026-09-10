"""Agri-first APIs: 项目区 (virtual_project_areas), 地块 (land_parcels), S1/S2 scenes.

Primary product surface for this fork. OpenFarm /v1/farms and /v1/fields remain
available but are treated as legacy for agri-satellite-analysis.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.storage import get_parcel_product_storage
from app.middleware.auth import OrgContext, require_roles, org_matches, org_scope
from app.schemas.agri import (
    AgriStatsOut,
    AgriTableCount,
    LandParcelOut,
    LandScenesSummaryOut,
    ProjectAreaLandOut,
    ProjectAreaOut,
    SceneProductOut,
    SensorSceneSummary)
from app.schemas.common import PaginatedResponse

router = APIRouter(prefix="/agri", tags=["agri"])

logger = logging.getLogger(__name__)

_reader = require_roles("owner", "admin", "member", "viewer")

# Averages + meta; pixel_data excluded unless include_pixels=1
_SCENE_COLS = """
    land_id, tile_id, date, sensor, scene_id, land_name,
    cloud_cover, cloud_cover_over_30, parcel_cloud_cover_pct,
    json_oss_key, pixel_data_url, pixel_count,
    ndvi_avg, ndvi_min, ndvi_max,
    evi_avg, evi_min, evi_max,
    ndmi_avg, ndmi_min, ndmi_max,
    ndre_avg, ndre_min, ndre_max,
    mndwi_avg, mndwi_min, mndwi_max,
    cire_avg, cire_min, cire_max,
    vv_avg, vv_min, vv_max,
    vh_avg, vh_min, vh_max,
    generated_at_shanghai, ingested_at
"""


def _row_to_dict(row: Any) -> dict[str, Any]:
    from decimal import Decimal

    d = dict(row._mapping)
    for k, v in list(d.items()):
        if isinstance(v, Decimal):
            d[k] = float(v)
        elif k in (
            "boundary_geojson",
            "source_properties",
            "pixel_data") and isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return d


def _normalize_lonlat_pixels(raw_pixels: Any) -> list[dict[str, Any]]:
    """Keep only dict lon/lat pixel objects (DB lonlat_v1 or OSS JSON)."""
    if not isinstance(raw_pixels, list):
        return []
    out: list[dict[str, Any]] = []
    for p in raw_pixels:
        if not isinstance(p, dict):
            continue
        lon = p.get("lon")
        lat = p.get("lat")
        if lon is None or lat is None:
            continue
        try:
            float(lon)
            float(lat)
        except (TypeError, ValueError):
            continue
        out.append(p)
    return out


def _normalize_oss_pixels(raw_pixels: Any) -> list[dict[str, Any]]:
    """Keep only dict lon/lat pixel objects from OSS JSON."""
    return _normalize_lonlat_pixels(raw_pixels)


def _pixels_from_db_lonlat(pixel_data: Any) -> list[dict[str, Any]] | None:
    """Extract lonlat_v1 pixels from agri.parcel_scene_products.pixel_data."""
    if not isinstance(pixel_data, dict):
        return None
    if pixel_data.get("format") != "lonlat_v1":
        return None
    pixels = _normalize_lonlat_pixels(pixel_data.get("pixels"))
    return pixels or None


def _oss_str_url(value: Any) -> str | None:
    """Accept non-empty string URLs from OSS JSON; reject other types."""
    if isinstance(value, str):
        s = value.strip()
        if s:
            return s
    return None


def _extract_oss_media_urls(obj: dict[str, Any]) -> dict[str, str | None]:
    """Pull preview image URLs from an OSS parcel product JSON object."""
    rgb_url = _oss_str_url(obj.get("rgb_url"))
    large_rgb_url = _oss_str_url(obj.get("large_rgb_url"))
    heatmap_url = _oss_str_url(obj.get("heatmap_url"))
    s2_heatmap_url = _oss_str_url(obj.get("s2_heatmap_url"))
    return {
        "rgb_url": rgb_url,
        "large_rgb_url": large_rgb_url,
        # Backward-compatible single heatmap field (prefer dedicated heatmap, else S2).
        "heatmap_url": heatmap_url or s2_heatmap_url,
        "s2_heatmap_url": s2_heatmap_url,
    }


def _load_oss_scene_json(json_oss_key: str | None) -> dict[str, Any] | None:
    """Fetch and parse original OSS parcel JSON; return dict or None on failure."""
    if not json_oss_key or not isinstance(json_oss_key, str):
        return None
    key = json_oss_key.strip()
    if not key:
        return None
    try:
        storage = get_parcel_product_storage()
        raw = storage.get_bytes(key)
        obj = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 — fallback to DB grid is intentional
        logger.warning("OSS scene JSON fetch failed for %s: %s", key, exc)
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _load_oss_scene_media(json_oss_key: str | None) -> dict[str, str | None] | None:
    """Load rgb/heatmap preview URLs from OSS JSON without requiring pixels.

    Used when pixels come from DB lonlat_v1 but json_oss_key still has preview images.
    """
    obj = _load_oss_scene_json(json_oss_key)
    if obj is None:
        return None
    return _extract_oss_media_urls(obj)


def _load_oss_scene_pixels(json_oss_key: str | None) -> dict[str, Any] | None:
    """Fetch original OSS parcel JSON; return pixels + media URLs, or media-only if no pixels."""
    obj = _load_oss_scene_json(json_oss_key)
    if obj is None:
        return None
    pixels = _normalize_oss_pixels(obj.get("pixels"))
    media = _extract_oss_media_urls(obj)
    if not pixels:
        logger.warning(
            "OSS JSON %s has no lon/lat pixels; returning media URLs only",
            json_oss_key)
        return {"pixels_lonlat": None, "pixel_count": None, **media}
    return {
        "pixels_lonlat": pixels,
        "pixel_count": obj.get("pixel_count") or len(pixels),
        **media,
    }


def _clear_scene_media_urls(d: dict[str, Any]) -> None:
    d["rgb_url"] = None
    d["large_rgb_url"] = None
    d["heatmap_url"] = None
    d["s2_heatmap_url"] = None


def _attach_scene_media_urls(d: dict[str, Any], media: dict[str, Any] | None) -> None:
    if media:
        d["rgb_url"] = media.get("rgb_url")
        d["large_rgb_url"] = media.get("large_rgb_url")
        d["heatmap_url"] = media.get("heatmap_url")
        d["s2_heatmap_url"] = media.get("s2_heatmap_url")
    else:
        _clear_scene_media_urls(d)


async def _agri_ready(db: AsyncSession) -> None:
    q = await db.execute(
        text(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'agri' LIMIT 1"
        )
    )
    if q.scalar() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agri schema not installed; run: make agri-seed (see scripts/agri_seed/README.md)")


@router.get("/stats", response_model=AgriStatsOut)
@router.get("/admin/import-status", response_model=AgriStatsOut)
async def agri_stats(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)]):
    """Read-only row counts for agri tables (import health check)."""
    await _agri_ready(db)
    tables = [
        "virtual_project_areas",
        "virtual_project_area_lands",
        "land_parcels",
        "parcel_scene_products",
        "ingest_runs",
        "ingest_batch_stats",
        "ingested_oss_objects",
    ]
    counts: list[AgriTableCount] = []
    for t in tables:
        r = await db.execute(text(f"SELECT count(*) FROM agri.{t}"))  # noqa: S608
        counts.append(AgriTableCount(table=t, count=int(r.scalar() or 0)))
    return AgriStatsOut(
        tables=counts,
        note=(
            "parcel_scene_products seed dump is a 1000-row sample; "
            "other tables are full export. Primary APIs are under /v1/agri/*; "
            "/v1/farms and /v1/fields are legacy in this fork."
        ))


@router.get("/project-areas", response_model=PaginatedResponse[ProjectAreaOut])
async def list_project_areas(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    province: str | None = Query(None, description="Filter province_name ILIKE"),
    city: str | None = Query(None),
    county: str | None = Query(None),
    q: str | None = Query(None, description="Search tile_id / project_key / names"),
    include_boundary: int = Query(0, ge=0, le=1)):
    """List 项目区 tiles (virtual_project_areas)."""
    await _agri_ready(db)
    where = ["TRUE"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if province:
        where.append("province_name ILIKE :province")
        params["province"] = f"%{province}%"
    if city:
        where.append("city_name ILIKE :city")
        params["city"] = f"%{city}%"
    if county:
        where.append("county_name ILIKE :county")
        params["county"] = f"%{county}%"
    if q:
        where.append(
            "("
            "tile_id ILIKE :q OR coalesce(project_key,'') ILIKE :q OR "
            "coalesce(group_name,'') ILIKE :q OR coalesce(org_name,'') ILIKE :q OR "
            "coalesce(province_name,'') ILIKE :q OR coalesce(city_name,'') ILIKE :q OR "
            "coalesce(county_name,'') ILIKE :q"
            ")"
        )
        params["q"] = f"%{q}%"
    wh = " AND ".join(where)

    total = (
        await db.execute(
            text(f"SELECT count(*) FROM agri.virtual_project_areas WHERE {wh}"),
            params)
    ).scalar() or 0

    boundary_expr = (
        "boundary_geojson" if include_boundary else "NULL::jsonb AS boundary_geojson"
    )
    rows = (
        await db.execute(
            text(
                f"""
                SELECT tile_id, project_key, anchor_land_id, assignment_type, parcel_count,
                       tile_width_m, tile_height_m, group_id, group_name, base_id,
                       org_code, org_name, province_name, city_name, county_name,
                       {boundary_expr}, boundary_srid,
                       min_lon, min_lat, max_lon, max_lat, created_at, updated_at,
                       parcel_count AS land_count
                FROM agri.virtual_project_areas
                WHERE {wh}
                ORDER BY province_name NULLS LAST, city_name NULLS LAST, county_name NULLS LAST, tile_id
                LIMIT :limit OFFSET :offset
                """
            ),
            params)
    ).fetchall()

    items = [ProjectAreaOut.model_validate(_row_to_dict(r)) for r in rows]
    return PaginatedResponse(items=items, total=int(total), limit=limit, offset=offset)


@router.get("/project-areas/{tile_id}", response_model=ProjectAreaOut)
async def get_project_area(
    tile_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)]):
    await _agri_ready(db)
    row = (
        await db.execute(
            text(
                """
                SELECT a.*,
                       (SELECT count(*) FROM agri.virtual_project_area_lands l
                        WHERE l.tile_id = a.tile_id) AS land_count
                FROM agri.virtual_project_areas a
                WHERE a.tile_id = :tile_id
                """
            ),
            {"tile_id": tile_id})
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project area (tile) not found")
    return ProjectAreaOut.model_validate(_row_to_dict(row))


@router.get(
    "/project-areas/{tile_id}/lands",
    response_model=PaginatedResponse[ProjectAreaLandOut])
async def list_project_area_lands(
    tile_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0)):
    await _agri_ready(db)
    exists = (
        await db.execute(
            text("SELECT 1 FROM agri.virtual_project_areas WHERE tile_id = :tile_id"),
            {"tile_id": tile_id})
    ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="Project area (tile) not found")

    params = {"tile_id": tile_id, "limit": limit, "offset": offset}
    total = (
        await db.execute(
            text(
                "SELECT count(*) FROM agri.virtual_project_area_lands WHERE tile_id = :tile_id"
            ),
            params)
    ).scalar() or 0
    rows = (
        await db.execute(
            text(
                """
                SELECT l.tile_id, l.land_id, l.assignment_type, l.is_anchor,
                       l.intersection_area_m2, l.coverage_ratio,
                       p.land_name, p.land_area_mu,
                       p.province_name, p.city_name, p.county_name,
                       p.min_lon, p.min_lat, p.max_lon, p.max_lat
                FROM agri.virtual_project_area_lands l
                JOIN agri.land_parcels p ON p.land_id = l.land_id
                WHERE l.tile_id = :tile_id
                ORDER BY l.is_anchor DESC, p.land_name NULLS LAST, l.land_id
                LIMIT :limit OFFSET :offset
                """
            ),
            params)
    ).fetchall()
    items = [ProjectAreaLandOut.model_validate(_row_to_dict(r)) for r in rows]
    return PaginatedResponse(items=items, total=int(total), limit=limit, offset=offset)


@router.get("/lands/{land_id}", response_model=LandParcelOut)
async def get_land(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)]):
    await _agri_ready(db)
    row = (
        await db.execute(
            text("SELECT * FROM agri.land_parcels WHERE land_id = :land_id"),
            {"land_id": land_id})
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Land parcel not found")
    return LandParcelOut.model_validate(_row_to_dict(row))


@router.get(
    "/lands/{land_id}/scenes",
    response_model=PaginatedResponse[SceneProductOut])
async def list_land_scenes(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    sensor: str | None = Query(None, pattern="^(S1|S2)$"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    include_pixels: int = Query(
        0,
        ge=0,
        le=1,
        description=(
            "If 1, prefer DB lonlat_v1 pixels (pixels_source=db_lonlat); "
            "else try OSS via json_oss_key; else legacy grid pixel_data (db_grid)."
        )),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0)):
    """S1/S2 time series for a 地块. Returns index averages for growth curves."""
    await _agri_ready(db)
    exists = (
        await db.execute(
            text("SELECT 1 FROM agri.land_parcels WHERE land_id = :land_id"),
            {"land_id": land_id})
    ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="Land parcel not found")

    where = ["land_id = :land_id"]
    params: dict[str, Any] = {
        "land_id": land_id,
        "limit": limit,
        "offset": offset,
    }
    if sensor:
        where.append("sensor = :sensor")
        params["sensor"] = sensor
    if date_from:
        where.append("date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where.append("date <= :date_to")
        params["date_to"] = date_to
    wh = " AND ".join(where)

    total = (
        await db.execute(
            text(f"SELECT count(*) FROM agri.parcel_scene_products WHERE {wh}"),
            params)
    ).scalar() or 0

    cols = _SCENE_COLS + (", pixel_data" if include_pixels else "")
    rows = (
        await db.execute(
            text(
                f"""
                SELECT {cols}
                FROM agri.parcel_scene_products
                WHERE {wh}
                ORDER BY date ASC, sensor ASC, scene_id ASC
                LIMIT :limit OFFSET :offset
                """
            ),
            params)
    ).fetchall()
    items: list[SceneProductOut] = []
    for r in rows:
        d = _row_to_dict(r)
        if not include_pixels:
            d.pop("pixel_data", None)
            d.pop("pixels_lonlat", None)
            d.pop("rgb_url", None)
            d.pop("large_rgb_url", None)
            d.pop("heatmap_url", None)
            d.pop("s2_heatmap_url", None)
            d.pop("pixels_source", None)
        else:
            db_lonlat = _pixels_from_db_lonlat(d.get("pixel_data"))
            if db_lonlat:
                d["pixels_lonlat"] = db_lonlat
                d["pixels_source"] = "db_lonlat"
                # Prefer DB lon/lat; drop grid payload so clients use pixels_lonlat.
                d["pixel_data"] = None
                if not d.get("pixel_count"):
                    d["pixel_count"] = len(db_lonlat)
                # Keep OSS preview images even when pixels come from DB lonlat_v1.
                _attach_scene_media_urls(
                    d, _load_oss_scene_media(d.get("json_oss_key"))
                )
            else:
                oss_payload = _load_oss_scene_pixels(d.get("json_oss_key"))
                if oss_payload and oss_payload.get("pixels_lonlat"):
                    d["pixels_lonlat"] = oss_payload["pixels_lonlat"]
                    d["pixels_source"] = "oss"
                    # Prefer OSS lon/lat; drop lossy grid to avoid frontend using it.
                    d["pixel_data"] = None
                    if oss_payload.get("pixel_count") and not d.get("pixel_count"):
                        d["pixel_count"] = oss_payload["pixel_count"]
                    _attach_scene_media_urls(d, oss_payload)
                else:
                    d["pixels_lonlat"] = None
                    # Attach media from same OSS fetch (or None if JSON missing).
                    _attach_scene_media_urls(d, oss_payload)
                    if d.get("pixel_data"):
                        d["pixels_source"] = "db_grid"
                    else:
                        d["pixels_source"] = None
        items.append(SceneProductOut.model_validate(d))
    payload = PaginatedResponse(
        items=items, total=int(total), limit=limit, offset=offset
    )
    if not include_pixels:
        return {
            "items": [
                i.model_dump(
                    exclude_none=False,
                    exclude={
                        "pixel_data",
                        "pixels_lonlat",
                        "rgb_url",
                        "large_rgb_url",
                        "heatmap_url",
                        "s2_heatmap_url",
                        "pixels_source",
                    })
                for i in items
            ],
            "total": int(total),
            "limit": limit,
            "offset": offset,
        }
    return payload


@router.get("/lands/{land_id}/scenes/summary", response_model=LandScenesSummaryOut)
async def land_scenes_summary(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)]):
    await _agri_ready(db)
    exists = (
        await db.execute(
            text("SELECT 1 FROM agri.land_parcels WHERE land_id = :land_id"),
            {"land_id": land_id})
    ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="Land parcel not found")

    rows = (
        await db.execute(
            text(
                """
                SELECT sensor,
                       count(*)::int AS count,
                       min(date) AS date_min,
                       max(date) AS date_max
                FROM agri.parcel_scene_products
                WHERE land_id = :land_id
                GROUP BY sensor
                ORDER BY sensor
                """
            ),
            {"land_id": land_id})
    ).fetchall()

    sensors: list[SensorSceneSummary] = []
    total = 0
    for r in rows:
        d = _row_to_dict(r)
        total += int(d["count"])
        latest = (
            await db.execute(
                text(
                    """
                    SELECT date, ndvi_avg, evi_avg, vv_avg, vh_avg
                    FROM agri.parcel_scene_products
                    WHERE land_id = :land_id AND sensor = :sensor
                    ORDER BY date DESC
                    LIMIT 1
                    """
                ),
                {"land_id": land_id, "sensor": d["sensor"]})
        ).fetchone()
        latest_d = _row_to_dict(latest) if latest else {}
        sensors.append(
            SensorSceneSummary(
                sensor=d["sensor"],
                count=int(d["count"]),
                date_min=d.get("date_min"),
                date_max=d.get("date_max"),
                latest_date=latest_d.get("date"),
                latest_ndvi_avg=latest_d.get("ndvi_avg"),
                latest_evi_avg=latest_d.get("evi_avg"),
                latest_vv_avg=latest_d.get("vv_avg"),
                latest_vh_avg=latest_d.get("vh_avg"))
        )

    return LandScenesSummaryOut(land_id=land_id, total=total, sensors=sensors)


# China overview (全国态势) — country/province/city/county stats
from app.routers.agri_overview import router as overview_router  # noqa: E402

router.include_router(overview_router)
