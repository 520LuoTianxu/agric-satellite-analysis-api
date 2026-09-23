"""遥感地块清单聚合回填接口。"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.task_priority import MANUAL_TASK_PRIORITY
from app.core.config import settings
from app.core.database import get_db
from app.middleware.auth import OrgContext, require_roles
from app.mq_publish import publish_api_task
from app.schemas.satellite_batch import SatelliteBatchRequest, SatelliteBatchResponse
from app.services.smart_land_backfill import (
    SMART_BACKFILL_MAX_LANDS,
    LandSelectionError,
    ensure_land_parcels,
)
from app.services.virtual_area_service import build_vpa10_satellite_jobs

router = APIRouter()
_writer = require_roles("owner", "admin", "member")


@router.post(
    "/lands/backfill-indices/batch",
    response_model=SatelliteBatchResponse,
    status_code=202,
)
async def backfill_satellite_batch(
    body: SatelliteBatchRequest,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """支持清单/闭区间选地，缺失主数据时先从 Smart 补齐再派发 S1/S2。"""
    requested_land_ids = body.resolved_land_ids()
    try:
        # 输入可以覆盖较大编号范围，但实际查询后最多只为1000个有效地块创建任务。
        lands, selection = await ensure_land_parcels(
            db,
            requested_land_ids,
            max_lands=SMART_BACKFILL_MAX_LANDS,
        )
    except LandSelectionError as exc:
        detail: object = (
            {"missing_land_ids": exc.missing_land_ids}
            if exc.missing_land_ids
            else str(exc)
        )
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    try:
        groups, jobs, _ = await build_vpa10_satellite_jobs(
            db,
            lands,
            date_from=body.date_from,
            date_to=body.date_to,
            sensors=body.sensors,
            force=body.force,
            assigned_by="manual-satellite-batch",
            chunk_days=settings.index_backfill_chunk_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    for job in jobs:
        db.add(job)
    # 先提交所有任务记录，下载机只需通过内部HTTP读job即可拿到地块列表与窗口。
    await db.commit()
    for index, job in enumerate(jobs):
        try:
            await asyncio.to_thread(
                publish_api_task,
                type="satellite_batch",
                land_id=job.land_id,
                task_id=str(job.id),
                extras={"job_id": str(job.id)},
                priority=MANUAL_TASK_PRIORITY,
            )
        except HTTPException as exc:
            # 部分派发失败时明确标出未入队任务，响应保留已派发编号，方便调用方核对。
            for pending in jobs[index:]:
                pending.status = "failed"
                pending.error = "遥感聚合任务派发失败"
            await db.commit()
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "message": "遥感聚合任务派发失败",
                    "queued_job_ids": [str(item.id) for item in jobs[:index]],
                    "failed_job_ids": [str(item.id) for item in jobs[index:]],
                },
            ) from exc
    return SatelliteBatchResponse(
        land_count=len(lands),
        group_count=len(groups),
        job_count=len(jobs),
        requested_land_count=(
            selection.get("requested_land_count") if selection else len(requested_land_ids)
        ),
        selected_land_ids=[str(land.land_id) for land in lands],
        skipped_land_count=(selection.get("skipped_land_count", 0) if selection else 0),
        date_from=body.date_from,
        date_to=body.date_to,
        groups=groups,
    )
