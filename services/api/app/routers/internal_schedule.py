"""下载机 Beat 的内部发现接口。Postgres 只在 API 机访问。"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.logging import logger
from app.middleware.internal_auth import InternalAuth
from app.models.tables import Job, LandParcel
from app.services.beat_schedule import (
    STAGGER_SECONDS,
    WEEKLY_INDEX_KEYS,
    index_task_name,
    weekly_date_window,
)
from app.services.overview_daily import business_today, finalize_daily, prepare_daily

router = APIRouter(prefix="/internal/schedule", tags=["internal-schedule"])


class WeeklyIndexJobOut(BaseModel):
    land_id: str
    job_id: str
    task_name: str
    countdown: int = 0
    date_from: str
    date_to: str
    index: str


class WeeklyIndexPrepareOut(BaseModel):
    items: list[WeeklyIndexJobOut] = Field(default_factory=list)
    lands_checked: int = 0
    lands_dispatched: int = 0
    jobs_created: int = 0


class WeatherLandsOut(BaseModel):
    land_ids: list[str] = Field(default_factory=list)
    batch_size: int = 50


@router.post("/daily-satellite")
async def prepare_daily_satellite(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    as_of: date | None = Query(None),
) -> dict[str, Any]:
    """每日发现全部有效地块，按5×5公里窗口派发增量S1/S2下载。"""
    from fastapi import HTTPException

    day = as_of or business_today()
    if day > business_today():
        raise HTTPException(400, "统计日期不能晚于北京时间当天")
    return await prepare_daily(db, day)


@router.post("/daily-satellite/{run_id}/finalize")
async def finalize_daily_satellite(
    run_id: uuid.UUID,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """核对下载和MQ入库状态，统一保存每日干旱、洪涝和弱长势快照。"""
    return await finalize_daily(db, run_id)


@router.post("/weekly-index", response_model=WeeklyIndexPrepareOut)
async def prepare_weekly_index(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """列出过期的统一地块，在 API 建 Job，返回给下载机执行。"""
    today = date.today()
    # 规范光学结果存于 parcel_scene_products；不再以历史 raster_layers
    # 作为另一套地块/遥感主链路。
    rows = (
        await db.execute(
            text(
                """
                SELECT l.land_id, max(p.date)::date AS latest_date
                FROM agric_satellite.land_parcels AS l
                LEFT JOIN agric_satellite.parcel_scene_products AS p
                  ON p.land_id = l.land_id
                WHERE l.deleted_at IS NULL
                GROUP BY l.land_id
                ORDER BY l.land_id
                """
            )
        )
    ).all()

    items: list[WeeklyIndexJobOut] = []
    lands_dispatched = 0
    for land_id, latest in rows:
        window = weekly_date_window(latest, today=today)
        if window is None:
            continue
        date_from, date_to = window
        countdown = lands_dispatched * STAGGER_SECONDS
        for idx_key in WEEKLY_INDEX_KEYS:
            job = Job(
                land_id=land_id,
                type=idx_key,
                status="pending",
                params_json={
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                },
            )
            db.add(job)
            await db.flush()
            items.append(
                WeeklyIndexJobOut(
                    land_id=land_id,
                    job_id=str(job.id),
                    task_name=index_task_name(idx_key),
                    countdown=countdown,
                    date_from=date_from.isoformat(),
                    date_to=date_to.isoformat(),
                    index=idx_key,
                )
            )
        lands_dispatched += 1

    await db.commit()
    return WeeklyIndexPrepareOut(
        items=items,
        lands_checked=len(rows),
        lands_dispatched=lands_dispatched,
        jobs_created=len(items),
    )


@router.get("/weather-lands", response_model=WeatherLandsOut)
async def list_weather_lands(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """每日 Open-Meteo 拉取用的有效 land_id 列表。"""
    land_ids = (
        (
            await db.execute(
                select(LandParcel.land_id).where(LandParcel.deleted_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    return WeatherLandsOut(
        land_ids=[str(lid) for lid in land_ids],
        batch_size=settings.weather_batch_size,
    )


@router.post("/overview-refresh")
async def refresh_overview(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    window_days: int = Query(60, ge=1, le=365),
    crop: str | None = Query(default=None),
) -> dict[str, Any]:
    """在 API 上跑全国和分省总览预聚合，写入 overview_stats_daily。"""
    return await _refresh_overview_preagg(db, window_days=window_days, crop=crop)


# 总览缓存表 upsert：同一窗口同一作物只保留最新一行
_UPSERT_OVERVIEW_SQL = """
INSERT INTO agric_satellite.overview_stats_daily (
    as_of_date, level, region_code, region_name, parent_code,
    metric_json, window_from, window_to, crop, updated_at
) VALUES (
    :as_of, :level, :region_code, :region_name, :parent_code,
    CAST(:metric AS jsonb), :window_from, :window_to, :crop, now()
)
ON CONFLICT (as_of_date, level, region_code, window_from, window_to, crop)
DO UPDATE SET
    region_name = EXCLUDED.region_name,
    parent_code = EXCLUDED.parent_code,
    metric_json = EXCLUDED.metric_json,
    updated_at = now()
WHERE COALESCE(overview_stats_daily.metric_json->'filters'->>'snapshot', 'false') <> 'true'
"""


async def _refresh_overview_preagg(
    db: AsyncSession,
    *,
    window_days: int,
    crop: str | None,
) -> dict[str, Any]:
    """先算全国，再按省逐个计算并写入缓存。"""
    from app.core.crops import normalize_crop_key
    from app.routers.agri_overview import (
        _compute_live_stats,
        _resolve_region_label,
        ensure_overview_cache_table,
    )

    to_d = date.today()
    from_d = to_d - timedelta(days=int(window_days))
    crop_key = normalize_crop_key(crop) if crop else None
    await ensure_overview_cache_table(db)

    results: list[dict[str, Any]] = []

    async def _one(
        *,
        level: str,
        code: str | None,
        name: str | None,
        parent_code: str | None = None,
    ) -> None:
        # 全国只用景均值；省一级才允许像素干旱
        allow_pixels = level != "country"
        out = await _compute_live_stats(
            db,
            level=level,  # type: ignore[arg-type]
            code=code,
            name=name,
            from_d=from_d,
            to_d=to_d,
            crop=crop,
            allow_pixels=allow_pixels,
        )
        payload = out.model_dump(mode="json")
        resolved_code, resolved_name = await _resolve_region_label(
            db,
            level,
            code,
            name,  # type: ignore[arg-type]
        )
        await db.execute(
            text(_UPSERT_OVERVIEW_SQL),
            {
                "as_of": to_d,
                "level": level,
                "region_code": resolved_code or "",
                "region_name": resolved_name,
                "parent_code": parent_code,
                "metric": json.dumps(payload, ensure_ascii=False, default=str),
                "window_from": from_d,
                "window_to": to_d,
                "crop": crop_key or "",
            },
        )
        results.append(
            {
                "level": level,
                "code": resolved_code,
                "name": resolved_name,
                "parcel_count": (payload.get("totals") or {}).get("parcel_count"),
            }
        )

    await _one(level="country", code=None, name=None)
    provinces = (
        await db.execute(
            text(
                """
                SELECT province_code AS code, province_name AS name, count(*) AS n
                FROM agric_satellite.land_parcels
                WHERE province_name IS NOT NULL AND deleted_at IS NULL
                GROUP BY province_code, province_name
                ORDER BY n DESC, name
                """
            )
        )
    ).fetchall()
    for row in provinces:
        try:
            await _one(
                level="province",
                code=str(row.code) if row.code else None,
                name=str(row.name),
                parent_code=None,
            )
        except Exception as exc:
            logger.warning(
                "overview_preagg_province_failed",
                code=str(row.code) if row.code else None,
                name=str(row.name),
                error=str(exc),
            )
            continue

    await db.commit()
    return {
        "ok": True,
        "as_of": to_d.isoformat(),
        "window_from": from_d.isoformat(),
        "window_to": to_d.isoformat(),
        "regions": len(results),
        "results": results,
    }
