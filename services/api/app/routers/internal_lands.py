"""Internal data endpoints for workers.

Workers receive and return the canonical land_id.  The endpoints deliberately
have no field UUID, tag lookup, or compatibility mapping logic.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.geo import geojson_centroid
from app.middleware.internal_auth import InternalAuth
from app.models.tables import LandParcel

router = APIRouter(prefix="/internal/lands", tags=["internal-lands"])


class LandResolveOut(BaseModel):
    land_id: str
    source_parcel_id: str | None = None
    tile_id: str
    virtual_tile_id: str | None = None
    project_key: str | None = None
    land_name: str | None = None
    farm_id: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    province_name: str | None = None
    city_name: str | None = None
    county_name: str | None = None
    town_name: str | None = None
    village_name: str | None = None
    boundary_geojson: dict[str, Any] | None = None
    crop_type: str | None = None
    season: str | None = None
    tags_json: list[Any] | None = None


class LandGeomOut(BaseModel):
    land_id: str
    land_name: str | None = None
    area_ha: float | None = None
    centroid_lon: float | None = None
    centroid_lat: float | None = None
    geojson: dict[str, Any] | None = None


class LandTagsPatch(BaseModel):
    tags_json: list[str] | None = None


@router.get("/resolve", response_model=LandResolveOut)
async def resolve_land(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    land_id: str = Query(..., min_length=1),
):
    """Read one canonical parcel by land_id; no lookup or translation occurs."""
    land = await db.get(LandParcel, land_id.strip())
    if not land or land.deleted_at is not None:
        raise HTTPException(status_code=404, detail="land parcel not found")
    return LandResolveOut(
        land_id=land.land_id,
        source_parcel_id=land.source_parcel_id,
        tile_id=land.tile_id,
        virtual_tile_id=land.virtual_tile_id,
        project_key=land.project_key,
        land_name=land.land_name,
        farm_id=str(land.farm_id) if land.farm_id else None,
        group_id=land.group_id,
        group_name=land.group_name,
        province_name=land.province_name,
        city_name=land.city_name,
        county_name=land.county_name,
        town_name=land.town_name,
        village_name=land.village_name,
        boundary_geojson=land.boundary_geojson,
        crop_type=land.crop_type,
        season=land.season,
        tags_json=land.tags_json,
    )


@router.get("/{land_id}/geom", response_model=LandGeomOut)
async def land_geom(
    land_id: str,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    include_geojson: int = Query(0, ge=0, le=1),
):
    """Return centroid and optional JSONB boundary from land_parcels."""
    land = await db.get(LandParcel, land_id)
    if not land or land.deleted_at is not None:
        raise HTTPException(status_code=404, detail="land parcel not found")
    centroid = geojson_centroid(land.boundary_geojson)
    return LandGeomOut(
        land_id=land.land_id,
        land_name=land.land_name,
        area_ha=float(land.area_ha) if land.area_ha is not None else None,
        centroid_lon=centroid[1] if centroid else None,
        centroid_lat=centroid[0] if centroid else None,
        geojson=land.boundary_geojson if include_geojson else None,
    )


@router.patch("/{land_id}/tags", response_model=LandResolveOut)
async def patch_land_tags(
    land_id: str,
    body: LandTagsPatch,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update tags on the canonical row; tags never determine parcel identity."""
    land = await db.get(LandParcel, land_id)
    if not land or land.deleted_at is not None:
        raise HTTPException(status_code=404, detail="land parcel not found")
    if body.tags_json is not None:
        land.tags_json = body.tags_json
    await db.commit()
    await db.refresh(land)
    return LandResolveOut(
        land_id=land.land_id,
        tile_id=land.tile_id,
        land_name=land.land_name,
        farm_id=str(land.farm_id) if land.farm_id else None,
        boundary_geojson=land.boundary_geojson,
        crop_type=land.crop_type,
        season=land.season,
        tags_json=land.tags_json,
    )


class DataReadinessOut(BaseModel):
    land_id: str
    weather_rows: int = 0
    soil_ok: bool = False
    s2_dates: int = 0
    s1_dates: int = 0
    span_days: int | None = None


def _sync_load_assessment_bundle(land_id: str) -> dict[str, Any]:
    """Run the report loader against the same canonical land row."""
    from app.core.database_sync import SyncSession
    from app.reports.land_assessment.data_loader import load_land_bundle

    session = SyncSession()
    try:
        return load_land_bundle(session, land_id, allow_http=False)
    finally:
        session.close()


@router.get("/{land_id}/assessment-bundle")
async def assessment_bundle(
    land_id: str,
    _: InternalAuth,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    del date_from, date_to
    try:
        return await asyncio.to_thread(_sync_load_assessment_bundle, land_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"assessment-bundle failed: {exc}") from exc


@router.get("/{land_id}/data-readiness", response_model=DataReadinessOut)
async def data_readiness(
    land_id: str,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
):
    """Check readiness by querying direct land_id foreign keys."""
    if not await db.get(LandParcel, land_id):
        raise HTTPException(status_code=404, detail="land parcel not found")
    params: dict[str, Any] = {"land_id": land_id}
    weather_sql = "SELECT count(*)::int FROM agric_satellite.weather_daily WHERE land_id = :land_id"
    if date_from:
        weather_sql += " AND date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        weather_sql += " AND date <= :date_to"
        params["date_to"] = date_to
    weather_rows = int((await db.execute(text(weather_sql), params)).scalar() or 0)
    soil_ok = bool(
        (
            await db.execute(
                text(
                    "SELECT 1 FROM agric_satellite.soil_profiles "
                    "WHERE land_id = :land_id LIMIT 1"
                ),
                {"land_id": land_id},
            )
        ).scalar()
    )
    s2_dates = s1_dates = 0
    span_days = None
    if date_from and date_to:
        if date_to < date_from:
            raise HTTPException(status_code=400, detail="date_to must be >= date_from")
        span_days = (date_to - date_from).days + 1
        row = (
            await db.execute(
                text(
                    """
                    SELECT
                      count(DISTINCT date) FILTER (WHERE sensor = 'S2') AS s2_dates,
                      count(DISTINCT date) FILTER (WHERE sensor = 'S1') AS s1_dates
                    FROM agric_satellite.parcel_scene_products
                    WHERE land_id = :land_id AND date >= :date_from AND date <= :date_to
                      AND coalesce(scene_id, '') NOT LIKE '%_decloud'
                      AND coalesce(pixel_data->>'source', '') <> 'uncrtaints_decloud'
                    """
                ),
                {"land_id": land_id, "date_from": date_from, "date_to": date_to},
            )
        ).mappings().first()
        s2_dates, s1_dates = int(row["s2_dates"] or 0), int(row["s1_dates"] or 0)
    return DataReadinessOut(
        land_id=land_id,
        weather_rows=weather_rows,
        soil_ok=soil_ok,
        s2_dates=s2_dates,
        s1_dates=s1_dates,
        span_days=span_days,
    )


def _sync_load_season_growth_inputs(
    land_id: str, date_from: str, date_to: str
) -> dict[str, Any]:
    """Return direct land metadata and S1/S2 facts for season-growth."""
    from datetime import date as date_type

    from sqlalchemy import text as sa_text

    from app.core.agri_classify import (
        CLOUD_MAX_PCT,
        cloud_pct,
        is_official_optical_product,
        parse_s1_relative_orbit,
    )
    from app.core.database_sync import SyncSession

    start = date_type.fromisoformat(date_from[:10])
    end = date_type.fromisoformat(date_to[:10])
    if end < start:
        raise ValueError("date_to must be >= date_from")
    session = SyncSession()
    try:
        row = session.execute(
            sa_text(
                """
                SELECT land_id, land_name, area_ha, crop_type, tags_json
                FROM agric_satellite.land_parcels
                WHERE land_id = :land_id AND deleted_at IS NULL
                """
            ),
            {"land_id": land_id},
        ).mappings().first()
        if not row:
            raise ValueError(f"Land parcel not found: {land_id}")
        land_meta = {
            "land_id": row["land_id"],
            "land_name": row["land_name"] or "地块",
            "crop_type": row["crop_type"],
            "area_ha": float(row["area_ha"]) if row["area_ha"] is not None else None,
            "tags_json": row["tags_json"] if isinstance(row["tags_json"], list) else [],
        }

        def number(value: Any) -> float | None:
            try:
                return None if value is None else float(value)
            except (TypeError, ValueError):
                return None

        def iso(value: Any) -> str:
            return value.isoformat()[:10] if hasattr(value, "isoformat") else str(value)[:10]

        scene_rows = session.execute(
            sa_text(
                """
                SELECT date, sensor, scene_id, ndvi_avg, evi_avg, ndmi_avg, mndwi_avg,
                       vv_avg, vh_avg, parcel_cloud_cover_pct, cloud_cover,
                       pixel_data->>'source' AS source,
                       pixel_data->>'decloud_quality' AS decloud_quality,
                       pixel_data->>'relative_orbit' AS relative_orbit,
                       rgb_url, large_rgb_url, rgb_oss_key
                FROM agric_satellite.parcel_scene_products
                WHERE land_id = :land_id AND date >= :date_from AND date <= :date_to
                ORDER BY date, sensor, scene_id
                """
            ),
            {
                "land_id": land_id,
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
            },
        ).mappings().all()
        s2_rows: list[dict[str, Any]] = []
        s1_rows: list[dict[str, Any]] = []
        for item in scene_rows:
            if item["sensor"] == "S2":
                cloud = cloud_pct(item["parcel_cloud_cover_pct"], item["cloud_cover"])
                official = is_official_optical_product(
                    source=item.get("source"),
                    scene_id=item.get("scene_id"),
                    parcel_cloud_cover_pct=item["parcel_cloud_cover_pct"],
                    cloud_cover=item["cloud_cover"],
                    decloud_quality=item.get("decloud_quality"),
                    cloud_max_pct=CLOUD_MAX_PCT,
                )
                s2_rows.append(
                    {
                        "date": iso(item["date"]),
                        "scene_id": item.get("scene_id"),
                        "ndvi_avg": number(item["ndvi_avg"]),
                        "evi_avg": number(item["evi_avg"]),
                        "ndmi_avg": number(item["ndmi_avg"]),
                        "mndwi_avg": number(item["mndwi_avg"]),
                        "cloud_pct": number(cloud),
                        "decloud_quality": item.get("decloud_quality"),
                        "source": item.get("source"),
                        "official": bool(official),
                        "clear": cloud is not None and cloud <= CLOUD_MAX_PCT,
                        "rgb_url": item.get("rgb_url"),
                        "large_rgb_url": item.get("large_rgb_url"),
                        "rgb_oss_key": item.get("rgb_oss_key"),
                    }
                )
            elif item["sensor"] == "S1":
                orbit = item.get("relative_orbit")
                if orbit is None:
                    orbit = parse_s1_relative_orbit(item.get("scene_id"))
                s1_rows.append(
                    {
                        "date": iso(item["date"]),
                        "scene_id": item.get("scene_id"),
                        "vv_avg": number(item["vv_avg"]),
                        "vh_avg": number(item["vh_avg"]),
                        "relative_orbit": orbit,
                        "rgb_url": item.get("rgb_url"),
                        "large_rgb_url": item.get("large_rgb_url"),
                        "rgb_oss_key": item.get("rgb_oss_key"),
                    }
                )
        return {
            "land": land_meta,
            "land_id": land_id,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "s2_rows": s2_rows,
            "s1_rows": s1_rows,
            "prior_s2_rows": [],
            "prior_window": {},
        }
    finally:
        session.close()


@router.get("/{land_id}/season-growth-inputs")
async def season_growth_inputs(
    land_id: str,
    _: InternalAuth,
    date_from: str = Query(..., min_length=8, max_length=32),
    date_to: str = Query(..., min_length=8, max_length=32),
):
    try:
        return await asyncio.to_thread(
            _sync_load_season_growth_inputs, land_id, date_from, date_to
        )
    except ValueError as exc:
        code = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"season-growth-inputs failed: {exc}") from exc
