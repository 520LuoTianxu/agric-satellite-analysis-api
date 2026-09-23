"""Smart 缺失地块补齐与参数化遥感回填编排。

Smart/MySQL 只在 API 机可访问；本模块负责先补齐 ``land_parcels``，再把
S1/S2 任务交给现有下载队列，避免下载机直接连接任一业务数据库。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import date
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.task_priority import MANUAL_TASK_PRIORITY
from app.core.config import settings
from app.models.tables import Job, LandParcel
from app.services.mysql_land_sync import sync_selected_lands
from app.services.satellite_batch import build_satellite_batch_jobs
from app.services.virtual_area_service import (
    create_vpa10_download_jobs,
    prepare_vpa10_areas,
)


SMART_BACKFILL_MAX_LANDS = 1000


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


async def _load_land_parcels(
    db: AsyncSession, land_ids: Sequence[str]
) -> list[LandParcel]:
    """按指定编号读取有效地块；空列表不发出无意义的 IN 查询。"""
    if not land_ids:
        return []
    result = await db.execute(
        select(LandParcel).where(
            LandParcel.land_id.in_(land_ids),
            LandParcel.deleted_at.is_(None),
        )
    )
    return list(result.scalars().all())


def _compact_sync_summary(
    summaries: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """压缩多批 Smart 同步结果，避免把未处理的几万条编号写进任务结果。"""
    if not summaries:
        return None

    def unique_values(key: str) -> list[str]:
        return list(
            dict.fromkeys(
                str(value)
                for summary in summaries
                for value in summary.get(key, [])
            )
        )

    synced = unique_values("synced_land_ids")
    missing = unique_values("missing_land_ids")
    filtered = unique_values("filtered_land_ids")
    invalid = unique_values("invalid_land_ids")
    has_skipped = bool(missing or filtered or invalid)
    return {
        "status": "partial" if has_skipped else "completed",
        "mode": "selected",
        "batch_count": len(summaries),
        "source_rows": sum(int(item.get("source_rows", 0) or 0) for item in summaries),
        "synced_land_ids": synced,
        "synced_land_count": len(synced),
        "missing_land_count": len(missing),
        "filtered_land_count": len(filtered),
        "invalid_land_count": len(invalid),
    }


def _selection_summary(
    requested: Sequence[str],
    lands: Sequence[LandParcel],
    *,
    selection_limit: int | None,
    sync_summaries: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """生成面向管理员的选地结果，明确本次真正进入遥感任务的编号。"""
    selected_land_ids = [str(land.land_id) for land in lands]
    return {
        "status": "partial"
        if len(selected_land_ids) < len(requested)
        else "completed",
        "requested_land_count": len(requested),
        "selected_land_ids": selected_land_ids,
        "selected_land_count": len(selected_land_ids),
        "skipped_land_count": max(0, len(requested) - len(selected_land_ids)),
        "selection_limit": selection_limit,
        "source_sync": _compact_sync_summary(sync_summaries),
    }


def _raise_sync_error(
    sync_summary: dict[str, Any],
    *,
    fallback_missing: Sequence[str],
) -> None:
    """把 Smart 源同步状态转换成稳定的 API 业务错误。"""
    status = sync_summary.get("status")
    if status == "skipped_locked":
        raise LandSelectionError("Smart 地块同步正在进行，请稍后重试", status_code=409)
    if status == "not_found":
        raise LandSelectionError(
            "Smart中不存在请求地块",
            missing_land_ids=sync_summary.get("missing_land_ids", fallback_missing),
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


async def ensure_land_parcels(
    db: AsyncSession,
    land_ids: Sequence[str],
    *,
    max_lands: int | None = None,
    allow_partial: bool = False,
) -> tuple[list[LandParcel], dict[str, Any] | None]:
    """读取并按请求顺序选择地块，缺失时精确从 Smart 补齐。

    ``max_lands`` 是执行上限而不是输入校验上限：先查询已有地块，再按请求顺序
    取最多指定数量。Smart 回填开启 ``allow_partial`` 后会分批查询源库，遇到
    不存在或无效编号会继续向后寻找可用地块，直到选满或请求耗尽。
    """
    requested = list(dict.fromkeys(str(value).strip() for value in land_ids))
    if not requested or any(not value for value in requested):
        raise ValueError("land_ids must contain at least one non-empty ID")
    if max_lands is not None and max_lands < 1:
        raise ValueError("max_lands must be positive")

    selection_limit = max_lands or len(requested)
    lands = await _load_land_parcels(db, requested)
    lands_by_id = {str(land.land_id): land for land in lands}
    ordered = _order_lands(lands_by_id.values(), requested)
    selected = ordered[:selection_limit]
    missing = [land_id for land_id in requested if land_id not in lands_by_id]
    sync_summaries: list[dict[str, Any]] = []

    # 没有缺失或已经达到执行上限时，不再访问 Smart，直接返回真实可处理清单。
    if len(selected) >= selection_limit or not missing:
        return selected, _selection_summary(
            requested,
            selected,
            selection_limit=max_lands,
        )

    if not settings.mysql_source_enabled:
        if not allow_partial or not selected:
            raise LandSelectionError(
                "land_parcels中缺少请求地块，且Smart源未启用",
                missing_land_ids=missing[:SMART_BACKFILL_MAX_LANDS],
            )
        return selected, _selection_summary(
            requested,
            selected,
            selection_limit=max_lands,
        )

    if not allow_partial:
        # 通用批量接口保留原有全量校验语义；只在执行上限内补齐所需编号。
        required_missing = missing[: max(0, selection_limit - len(selected))]
        sync_summary = await sync_selected_lands(
            required_missing,
            include_excluded_schedule_lands=True,
        )
        _raise_sync_error(sync_summary, fallback_missing=required_missing)
        refreshed = await _load_land_parcels(db, required_missing)
        lands_by_id.update({str(land.land_id): land for land in refreshed})
        selected = _order_lands(lands_by_id.values(), requested)[:selection_limit]
        remaining_required = [
            land_id for land_id in required_missing if land_id not in lands_by_id
        ]
        if remaining_required:
            raise LandSelectionError(
                "Smart同步后仍缺少请求地块",
                missing_land_ids=remaining_required,
            )
        return selected, _selection_summary(
            requested,
            selected,
            selection_limit=max_lands,
            sync_summaries=[sync_summary],
        )

    remaining = list(missing)
    while remaining and len(selected) < selection_limit:
        # 每批最多补齐当前还缺的数量，避免为凑够1000个任务额外拉取无用地块。
        batch_size = selection_limit - len(selected)
        batch = remaining[:batch_size]
        remaining = remaining[batch_size:]
        sync_summary = await sync_selected_lands(
            batch,
            include_excluded_schedule_lands=True,
            allow_partial=True,
        )
        if sync_summary.get("status") == "skipped_locked":
            raise LandSelectionError("Smart 地块同步正在进行，请稍后重试", status_code=409)
        if sync_summary.get("status") in {"failed", "disabled"}:
            raise LandSelectionError("Smart 地块同步未完成", status_code=503)
        sync_summaries.append(sync_summary)

        refreshed = await _load_land_parcels(db, batch)
        lands_by_id.update({str(land.land_id): land for land in refreshed})
        selected = _order_lands(lands_by_id.values(), requested)[:selection_limit]

    if not selected:
        raise LandSelectionError(
            "Smart中不存在可处理的请求地块",
            missing_land_ids=missing[:SMART_BACKFILL_MAX_LANDS],
        )
    return selected, _selection_summary(
        requested,
        selected,
        selection_limit=max_lands,
        sync_summaries=sync_summaries,
    )


def _order_lands(lands: Sequence[LandParcel], requested: Sequence[str]) -> list[LandParcel]:
    """保持调用方顺序，后续空间分组再按 land_id 选择稳定锚点。"""
    by_id = {str(land.land_id): land for land in lands}
    return [by_id[land_id] for land_id in requested if land_id in by_id]


async def _build_smart_virtual_area_jobs(
    db: AsyncSession,
    lands: Sequence[LandParcel],
    *,
    date_from: date,
    date_to: date,
    sensors: Sequence[str],
    force: bool,
    parent_job_id: uuid.UUID,
) -> tuple[list[Any], list[Job], bool]:
    """把 Smart 清单转换成 vpa10 项目区 Job；保留旧测试/旧库的安全回退。"""
    # 线上 API 使用真实 AsyncSession；测试中的轻量 fake DB 仍走既有构造器，
    # 避免为了验证任务树而要求连接真实 PostgreSQL。数据库脚本执行后线上必走 vpa10。
    if not isinstance(db, AsyncSession):
        groups, jobs = await asyncio.to_thread(
            build_satellite_batch_jobs,
            lands,
            date_from=date_from,
            date_to=date_to,
            sensors=sensors,
            force=force,
            parent_job_id=parent_job_id,
            id_namespace=parent_job_id,
            chunk_days=settings.index_backfill_chunk_days,
        )
        return list(groups), list(jobs), False

    snapshots = [
        {
            "land_id": str(land.land_id),
            "boundary_geojson": land.boundary_geojson,
            "boundary_srid": land.boundary_srid,
        }
        for land in lands
    ]
    prepared = await prepare_vpa10_areas(
        db,
        snapshots,
        date_from=date_from,
        date_to=date_to,
        assigned_by="smart-sync",
    )
    jobs = await create_vpa10_download_jobs(
        db,
        prepared["areas"],
        date_from=date_from,
        date_to=date_to,
        sensors=sensors,
        force=force,
        parent_job_id=parent_job_id,
        chunk_days=settings.index_backfill_chunk_days,
    )
    groups = [
        SimpleNamespace(
            anchor_land_id=area.get("anchor_land_id"),
            land_ids=list(area.get("land_ids", [])),
            job_ids=[],
            virtual_area_tile_id=area.get("tile_id"),
        )
        for area in prepared["areas"]
    ]
    jobs_by_area: dict[str, list[str]] = {}
    for job in jobs:
        tile_id = str((job.params_json or {}).get("virtual_area_tile_id") or "")
        jobs_by_area.setdefault(tile_id, []).append(str(job.id))
    for group in groups:
        group.job_ids = jobs_by_area.get(str(group.virtual_area_tile_id), [])
    return groups, jobs, True


async def run_smart_land_backfill(
    *,
    land_ids: Sequence[str],
    date_from: date,
    date_to: date,
    sensors: Sequence[str] = ("S1", "S2"),
    force: bool = False,
    parent_job_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """在 API 机执行一次参数化 Smart 回填，并派发已有遥感下载任务。"""
    from app.core.database import async_session
    from app.mq_publish import publish_api_task

    async with async_session() as db:
        lands, selection = await ensure_land_parcels(
            db,
            land_ids,
            max_lands=SMART_BACKFILL_MAX_LANDS,
            allow_partial=True,
        )
        selected_land_ids = [str(land.land_id) for land in lands]
        execution_parent_id = parent_job_id or uuid.uuid4()
        groups, jobs, using_virtual_areas = await _build_smart_virtual_area_jobs(
            db,
            lands,
            date_from=date_from,
            date_to=date_to,
            sensors=sensors,
            force=force,
            parent_job_id=execution_parent_id,
        )

        # 父 Job 是管理页的唯一一级节点；每个卫星日期/传感器 Job 都挂到它下面。
        parent_job = Job(
            id=execution_parent_id,
            type="smart_land_backfill",
            status="pending",
            progress_json={
                "stage": "queued",
                "land_count": len(lands),
                "group_count": len(groups),
                "job_count": len(jobs),
                "requested_land_count": selection["requested_land_count"]
                if selection
                else len(land_ids),
                "selected_land_count": len(selected_land_ids),
                "algorithm_version": (
                    "vpa10-greedy-v1" if using_virtual_areas else "legacy-5km-v1"
                ),
            },
            params_json={
                "land_ids": selected_land_ids,
                "selected_land_ids": selected_land_ids,
                "requested_land_count": selection["requested_land_count"]
                if selection
                else len(land_ids),
                "selected_land_count": len(selected_land_ids),
                "skipped_land_count": selection["skipped_land_count"]
                if selection
                else 0,
                "selection_limit": SMART_BACKFILL_MAX_LANDS,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "sensors": list(sensors),
                "force": force,
                "job_ids": [str(job.id) for job in jobs],
                "satellite_job_ids": [str(job.id) for job in jobs],
                "admin_task_run_id": str(execution_parent_id),
                "algorithm_version": (
                    "vpa10-greedy-v1" if using_virtual_areas else "legacy-5km-v1"
                ),
            },
        )
        db.add(parent_job)
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

        parent_job.status = "partial" if failed_job_ids else "running"
        parent_job.progress_json = {
            **(parent_job.progress_json or {}),
            "stage": "dispatched",
            "queued_count": len(jobs) - len(failed_job_ids),
            "failed_count": len(failed_job_ids),
        }
        await db.commit()

    return {
        "status": "partial" if failed_job_ids else "queued",
        "parent_job_id": str(execution_parent_id),
        "requested_land_count": selection["requested_land_count"] if selection else len(land_ids),
        "selected_land_ids": selected_land_ids,
        "selected_land_count": len(selected_land_ids),
        "skipped_land_count": selection["skipped_land_count"] if selection else 0,
        "selection_limit": SMART_BACKFILL_MAX_LANDS,
        "land_count": len(lands),
        "group_count": len(groups),
        "job_count": len(jobs),
        "queued_job_ids": [str(job.id) for job in jobs if str(job.id) not in failed_job_ids],
        "failed_job_ids": failed_job_ids,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "source_sync": selection.get("source_sync") if selection else None,
    }
