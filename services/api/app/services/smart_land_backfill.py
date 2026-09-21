"""Smart 缺失地块补齐与参数化遥感回填编排。

Smart/MySQL 只在 API 机可访问；本模块负责先补齐 ``land_parcels``，再把
S1/S2 任务交给现有下载队列，避免下载机直接连接任一业务数据库。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.task_priority import MANUAL_TASK_PRIORITY
from app.core.config import settings
from app.models.tables import LandParcel
from app.services.mysql_land_sync import sync_selected_lands
from app.services.satellite_batch import build_satellite_batch_jobs


class LandSelectionError(ValueError):
    """请求的地块无法形成完整、可处理的地块集合。"""

    def __init__(
        self,
        message: str,
        *,
        missing_land_ids: Sequence[str] = (),
        status_code: int = 404,
    ) -> None:
        super().__init__(message)
        self.missing_land_ids = list(missing_land_ids)
        self.status_code = status_code


async def ensure_land_parcels(
    db: AsyncSession, land_ids: Sequence[str]
) -> tuple[list[LandParcel], dict[str, Any] | None]:
    """读取地块；仅对 ``land_parcels`` 缺失的编号执行 Smart 精确同步。"""
    requested = list(dict.fromkeys(str(value).strip() for value in land_ids))
    result = await db.execute(
        select(LandParcel).where(
            LandParcel.land_id.in_(requested),
            LandParcel.deleted_at.is_(None),
        )
    )
    lands = list(result.scalars().all())
    existing_ids = {str(land.land_id) for land in lands}
    missing = [land_id for land_id in requested if land_id not in existing_ids]
    if missing:
        if not settings.mysql_source_enabled:
            raise LandSelectionError(
                "land_parcels中缺少请求地块，且Smart源未启用",
                missing_land_ids=missing,
            )
        # Smart 同步使用独立 API 数据库会话；提交后当前请求重新查询，保证拿到新记录。
        sync_summary = await sync_selected_lands(
            missing, include_excluded_schedule_lands=True
        )
        status = sync_summary.get("status")
        if status == "skipped_locked":
            raise LandSelectionError(
                "Smart 地块同步正在进行，请稍后重试", status_code=409
            )
        if status == "not_found":
            raise LandSelectionError(
                "Smart中不存在请求地块",
                missing_land_ids=sync_summary.get("missing_land_ids", missing),
            )
        if status in {"filtered", "invalid", "disabled"}:
            raise LandSelectionError(
                "Smart地块未通过同步校验",
                missing_land_ids=(
                    sync_summary.get("filtered_land_ids", [])
                    + sync_summary.get("invalid_land_ids", [])
                ),
            )
        if status != "completed":
            raise LandSelectionError("Smart 地块同步未完成", status_code=503)

        result = await db.execute(
            select(LandParcel).where(
                LandParcel.land_id.in_(requested),
                LandParcel.deleted_at.is_(None),
            )
        )
        lands = list(result.scalars().all())
        existing_ids = {str(land.land_id) for land in lands}
        missing = [land_id for land_id in requested if land_id not in existing_ids]
        if missing:
            raise LandSelectionError(
                "Smart同步后仍缺少请求地块", missing_land_ids=missing
            )
        return _order_lands(lands, requested), sync_summary

    return _order_lands(lands, requested), None


def _order_lands(lands: Sequence[LandParcel], requested: Sequence[str]) -> list[LandParcel]:
    """保持调用方顺序，后续空间分组再按 land_id 选择稳定锚点。"""
    by_id = {str(land.land_id): land for land in lands}
    return [by_id[land_id] for land_id in requested if land_id in by_id]


async def run_smart_land_backfill(
    *,
    land_ids: Sequence[str],
    date_from: date,
    date_to: date,
    sensors: Sequence[str] = ("S1", "S2"),
    force: bool = False,
) -> dict[str, Any]:
    """在 API 机执行一次参数化 Smart 回填，并派发已有遥感下载任务。"""
    from app.core.database import async_session
    from app.mq_publish import publish_api_task

    async with async_session() as db:
        lands, sync_summary = await ensure_land_parcels(db, land_ids)
        groups, jobs = await asyncio.to_thread(
            build_satellite_batch_jobs,
            lands,
            date_from=date_from,
            date_to=date_to,
            sensors=sensors,
            force=force,
            chunk_days=settings.index_backfill_chunk_days,
        )
        for job in jobs:
            db.add(job)
        await db.commit()

        failed_job_ids: list[str] = []
        for job in jobs:
            try:
                # Smart 凭据不进入 MQ payload；下载机只拿到任务 ID 并通过 Internal HTTP 取数据。
                await asyncio.to_thread(
                    publish_api_task,
                    type="satellite_batch",
                    land_id=job.land_id,
                    task_id=str(job.id),
                    extras={"job_id": str(job.id)},
                    priority=MANUAL_TASK_PRIORITY,
                )
            except Exception as exc:
                job.status = "failed"
                job.error = f"遥感聚合任务派发失败：{str(exc)[:3900]}"
                failed_job_ids.append(str(job.id))
        if failed_job_ids:
            await db.commit()

    return {
        "status": "partial" if failed_job_ids else "queued",
        "land_count": len(lands),
        "group_count": len(groups),
        "job_count": len(jobs),
        "queued_job_ids": [str(job.id) for job in jobs if str(job.id) not in failed_job_ids],
        "failed_job_ids": failed_job_ids,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "source_sync": sync_summary,
    }
