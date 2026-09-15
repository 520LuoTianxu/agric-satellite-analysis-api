"""Internal agri land/scene reads for download-host skip-existing (D2)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth

router = APIRouter(prefix="/internal/agri", tags=["internal-agri"])


class AgriLandOut(BaseModel):
    land_id: str
    tile_id: str | None = None
    land_name: str | None = None
    group_id: str | None = None


class AgriSceneDatesOut(BaseModel):
    land_id: str
    sensor: str
    dates: list[date] = Field(default_factory=list)
    count: int = 0


class AgriSensorSummary(BaseModel):
    sensor: str
    count: int = 0
    date_min: date | None = None
    date_max: date | None = None


class AgriScenesSummaryOut(BaseModel):
    land_id: str
    total: int = 0
    sensors: list[AgriSensorSummary] = Field(default_factory=list)


@router.get("/lands/{land_id}", response_model=AgriLandOut)
async def get_land(
    land_id: str,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = (
        (
            await db.execute(
                text(
                    """
                SELECT land_id, tile_id, land_name, group_id::text AS group_id
                FROM agric_satellite.land_parcels
                WHERE land_id = :lid
                LIMIT 1
                """
                ),
                {"lid": land_id},
            )
        )
        .mappings()
        .first()
    )
    if not row:
        # Allow RS ingest for agri-tagged fields before land_parcels upsert.
        return AgriLandOut(land_id=land_id)
    return AgriLandOut(
        land_id=row["land_id"],
        tile_id=row.get("tile_id"),
        land_name=row.get("land_name"),
        group_id=row.get("group_id"),
    )


@router.get(
    "/lands/{land_id}/scenes/dates",
    response_model=AgriSceneDatesOut,
)
async def land_scene_dates(
    land_id: str,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    sensor: str = Query(..., min_length=1, max_length=16),
):
    """Distinct scene dates for skip-existing (excludes decloud-derived rows).

    Matches ingest ``existing_agri_scene_dates`` filter semantics.
    """
    # land_parcels row is optional — tagged fields may ingest before parcel upsert.
    rows = (
        await db.execute(
            text(
                """
                SELECT DISTINCT date
                FROM agric_satellite.parcel_scene_products
                WHERE land_id = :land_id
                  AND sensor = :sensor
                  AND COALESCE(scene_id, '') NOT LIKE '%_decloud'
                  AND COALESCE(pixel_data->>'source', '') <> 'uncrtaints_decloud'
                ORDER BY date
                """
            ),
            {"land_id": land_id, "sensor": sensor},
        )
    ).fetchall()
    dates = [r[0] for r in rows if r[0] is not None]
    return AgriSceneDatesOut(
        land_id=land_id,
        sensor=sensor,
        dates=dates,
        count=len(dates),
    )


@router.get(
    "/lands/{land_id}/scenes/summary",
    response_model=AgriScenesSummaryOut,
)
async def land_scenes_summary(
    land_id: str,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    exists = (
        await db.execute(
            text("SELECT 1 FROM agric_satellite.land_parcels WHERE land_id = :lid"),
            {"lid": land_id},
        )
    ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="land parcel not found")

    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT sensor,
                       count(*)::int AS count,
                       min(date) AS date_min,
                       max(date) AS date_max
                FROM agric_satellite.parcel_scene_products
                WHERE land_id = :land_id
                GROUP BY sensor
                ORDER BY sensor
                """
                ),
                {"land_id": land_id},
            )
        )
        .mappings()
        .all()
    )

    sensors: list[AgriSensorSummary] = []
    total = 0
    for r in rows:
        c = int(r["count"] or 0)
        total += c
        sensors.append(
            AgriSensorSummary(
                sensor=r["sensor"],
                count=c,
                date_min=r.get("date_min"),
                date_max=r.get("date_max"),
            )
        )
    return AgriScenesSummaryOut(land_id=land_id, total=total, sensors=sensors)
