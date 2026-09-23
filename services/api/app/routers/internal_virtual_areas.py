"""项目区像素资产的内部读写接口；仅供下载机控制面调用。"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth

router = APIRouter(
    prefix="/internal/virtual-project-areas", tags=["internal-virtual-areas"]
)


class VirtualAreaAssetIn(BaseModel):
    sensor: str = Field(pattern="^(S1|S2)$")
    scene_date: date
    scene_id: str = Field(min_length=1, max_length=512)
    asset_kind: str = Field(min_length=1, max_length=32)
    oss_key: str = Field(min_length=1, max_length=1024)
    format: str = Field(default="json", max_length=32)
    compression: str = Field(default="gzip", max_length=32)
    grid_json: dict[str, Any] = Field(default_factory=dict)
    checksum: str = Field(min_length=1, max_length=128)
    byte_size: int = Field(ge=0)
    status: str = Field(
        default="ready", pattern="^(pending|running|ready|partial|failed|stale)$"
    )
    error: str | None = Field(default=None, max_length=4000)


@router.get("/{tile_id}/assets")
async def list_assets(
    tile_id: str,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    sensor: str = Query(..., pattern="^(S1|S2)$"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
) -> list[dict[str, Any]]:
    """只返回可直接读取的 pixel_json 资产，preview 由详情接口另行查询。"""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=400, detail="date_from must be no later than date_to"
        )
    params: dict[str, Any] = {"tile_id": tile_id, "sensor": sensor}
    clauses = [
        "tile_id = :tile_id",
        "sensor = :sensor",
        "asset_kind = 'pixel_json'",
        "status = 'ready'",
    ]
    if date_from:
        clauses.append("scene_date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        clauses.append("scene_date <= :date_to")
        params["date_to"] = date_to
    rows = (
        (
            await db.execute(
                text(
                    f"""
                SELECT tile_id, sensor, scene_date, scene_id, asset_kind, oss_key,
                       format, compression, grid_json, checksum, byte_size, status
                FROM agric_satellite.virtual_project_area_assets
                WHERE {" AND ".join(clauses)}
                ORDER BY scene_date, scene_id
                """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


@router.put("/{tile_id}/assets")
async def upsert_asset(
    tile_id: str,
    body: VirtualAreaAssetIn,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """OSS 上传成功后登记资产，并更新项目区最近回填时间。"""
    exists = await db.execute(
        text(
            """
            SELECT 1 FROM agric_satellite.virtual_project_areas
            WHERE tile_id = :tile_id AND algorithm_version = 'vpa10-greedy-v1'
            """
        ),
        {"tile_id": tile_id},
    )
    if exists.scalar() is None:
        raise HTTPException(status_code=404, detail="vpa10 project area not found")
    row = (
        (
            await db.execute(
                text(
                    """
                INSERT INTO agric_satellite.virtual_project_area_assets (
                    tile_id, sensor, scene_date, scene_id, asset_kind, oss_key,
                    format, compression, grid_json, checksum, byte_size, status, error,
                    updated_at
                ) VALUES (
                    :tile_id, :sensor, :scene_date, :scene_id, :asset_kind, :oss_key,
                    :format, :compression, CAST(:grid_json AS jsonb), :checksum,
                    :byte_size, :status, :error, now()
                )
                ON CONFLICT (tile_id, sensor, scene_date, scene_id, asset_kind)
                DO UPDATE SET
                    oss_key = EXCLUDED.oss_key,
                    format = EXCLUDED.format,
                    compression = EXCLUDED.compression,
                    grid_json = EXCLUDED.grid_json,
                    checksum = EXCLUDED.checksum,
                    byte_size = EXCLUDED.byte_size,
                    status = EXCLUDED.status,
                    error = EXCLUDED.error,
                    updated_at = now()
                RETURNING tile_id, sensor, scene_date, scene_id, asset_kind, oss_key,
                          format, compression, grid_json, checksum, byte_size, status, error
                """
                ),
                {
                    "tile_id": tile_id,
                    "sensor": body.sensor,
                    "scene_date": body.scene_date,
                    "scene_id": body.scene_id,
                    "asset_kind": body.asset_kind,
                    "oss_key": body.oss_key,
                    "format": body.format,
                    "compression": body.compression,
                    "grid_json": json.dumps(body.grid_json, ensure_ascii=False),
                    "checksum": body.checksum,
                    "byte_size": body.byte_size,
                    "status": body.status,
                    "error": body.error,
                },
            )
        )
        .mappings()
        .one()
    )
    await db.execute(
        text(
            """
            UPDATE agric_satellite.virtual_project_areas
            SET last_backfill_at = now(),
                manifest_oss_key = CASE
                    WHEN :asset_kind = 'pixel_json' THEN :oss_key
                    ELSE manifest_oss_key
                END,
                manifest_sha256 = CASE
                    WHEN :asset_kind = 'pixel_json' THEN :checksum
                    ELSE manifest_sha256
                END,
                status = CASE WHEN :status = 'ready' THEN 'active' ELSE status END,
                updated_at = now()
            WHERE tile_id = :tile_id
            """
        ),
        {
            "tile_id": tile_id,
            "status": body.status,
            "asset_kind": body.asset_kind,
            "oss_key": body.oss_key,
            "checksum": body.checksum,
        },
    )
    await db.commit()
    return dict(row)


__all__ = ["router"]
