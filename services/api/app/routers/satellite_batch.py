"""遥感地块清单聚合回填接口。"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.auth import OrgContext, require_roles
from app.models.tables import LandParcel
from app.mq_publish import publish_api_task
from app.schemas.satellite_batch import SatelliteBatchRequest, SatelliteBatchResponse
from app.services.satellite_batch import build_satellite_batch_jobs

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
    """传入landIdList，按5×5公里分组并派发共享窗口的S1/S2回填。"""
    lands = (
        (
            await db.execute(
                select(LandParcel).where(
                    LandParcel.land_id.in_(body.land_ids),
                    LandParcel.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    missing = sorted(set(body.land_ids) - {land.land_id for land in lands})
    if missing:
        raise HTTPException(status_code=404, detail={"missing_land_ids": missing})
    try:
        groups, jobs = await asyncio.to_thread(
            build_satellite_batch_jobs,
            lands,
            date_from=body.date_from,
            date_to=body.date_to,
            sensors=body.sensors,
            force=body.force,
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
        date_from=body.date_from,
        date_to=body.date_to,
        groups=groups,
    )
