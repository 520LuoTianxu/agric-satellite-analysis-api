"""Internal agri land/scene reads for download-host skip-existing (D2)."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import LandParcel

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


class SatelliteBatchInputsRequest(BaseModel):
    land_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1, max_length=50
    )
    sensor: Literal["S1", "S2"]
    include_existing_dates: bool = True

    @field_validator("land_ids", mode="before")
    @classmethod
    def normalize_land_ids(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        if len(value) > 50:
            raise ValueError("land_ids can contain at most 50 items")
        if any(not isinstance(item, str) for item in value):
            raise ValueError("land_ids must contain strings")
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("land_ids must not contain empty values")
        # 同一请求内去重并保留调用方顺序，避免重复地块导致重复下载。
        return list(dict.fromkeys(normalized))


class SatelliteBatchLandInputOut(BaseModel):
    land_id: str
    base_id: str | None = None
    land_area_mu: float | None = None
    tile_id: str
    land_name: str | None = None
    boundary_geojson: dict[str, Any]
    crop_type: str | None = None
    season: str | None = None
    existing_dates: list[date] = Field(default_factory=list)


class SatelliteBatchInputsOut(BaseModel):
    items: list[SatelliteBatchLandInputOut]


class AgriSensorSummary(BaseModel):
    sensor: str
    count: int = 0
    date_min: date | None = None
    date_max: date | None = None


class AgriScenesSummaryOut(BaseModel):
    land_id: str
    total: int = 0
    sensors: list[AgriSensorSummary] = Field(default_factory=list)


@router.post("/satellite-batch/inputs", response_model=SatelliteBatchInputsOut)
async def satellite_batch_inputs(
    body: SatelliteBatchInputsRequest,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """批量返回遥感任务所需地块元数据和已入库日期，避免下载机逐地块请求。"""
    land_ids = body.land_ids
    # 仅读取批处理所需字段，避免批量接口加载地块其他业务列增加数据库返回量。
    land_rows = (
        await db.execute(
            select(
                LandParcel.land_id,
                LandParcel.base_id,
                LandParcel.land_area_mu,
                LandParcel.tile_id,
                LandParcel.land_name,
                LandParcel.boundary_geojson,
                LandParcel.crop_type,
                LandParcel.season,
            ).where(
                LandParcel.land_id.in_(land_ids),
                LandParcel.deleted_at.is_(None),
            )
        )
    ).mappings().all()
    land_by_id = {land["land_id"]: land for land in land_rows}
    missing = [land_id for land_id in land_ids if land_id not in land_by_id]
    if missing:
        raise HTTPException(status_code=404, detail={"missing_land_ids": missing})

    dates_by_land: dict[str, list[date]] = {land_id: [] for land_id in land_ids}
    if body.include_existing_dates:
        date_query = text(
            """
            SELECT DISTINCT land_id, date
            FROM agric_satellite.parcel_scene_products
            WHERE land_id IN :land_ids
              AND sensor = :sensor
              AND COALESCE(scene_id, '') NOT LIKE '%_decloud'
              AND COALESCE(pixel_data->>'source', '') <> 'uncrtaints_decloud'
            ORDER BY land_id, date
            """
        ).bindparams(bindparam("land_ids", expanding=True))
        date_rows = await db.execute(
            date_query, {"land_ids": land_ids, "sensor": body.sensor}
        )
        for land_id, scene_date in date_rows:
            if scene_date is not None:
                dates_by_land[land_id].append(scene_date)

    items = []
    for land_id in land_ids:
        land = land_by_id[land_id]
        items.append(
            SatelliteBatchLandInputOut(
                land_id=land["land_id"],
                base_id=str(land["base_id"]) if land["base_id"] is not None else None,
                land_area_mu=(
                    float(land["land_area_mu"])
                    if land["land_area_mu"] is not None
                    else None
                ),
                tile_id=land["tile_id"],
                land_name=land["land_name"],
                boundary_geojson=land["boundary_geojson"],
                crop_type=land["crop_type"],
                season=land["season"],
                existing_dates=dates_by_land[land_id],
            )
        )
    return SatelliteBatchInputsOut(items=items)


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
        raise HTTPException(status_code=404, detail="land parcel not found")
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
    """读取跳过重复处理所需的原始场景日期，并排除派生去云产品。

    过滤口径需与 ingest ``existing_agri_scene_dates`` 一致，避免把去云产品
    误认为原始传感器观测已完成，导致后续原始场景被跳过。
    """
    exists = (
        await db.execute(
            text(
                "SELECT 1 FROM agric_satellite.land_parcels "
                "WHERE land_id = :land_id AND deleted_at IS NULL"
            ),
            {"land_id": land_id},
        )
    ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="land parcel not found")
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
