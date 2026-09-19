"""下载机回报管理员任务实际运行状态的内部接口。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import AdminTaskRun

router = APIRouter(prefix="/internal/admin", tags=["internal-admin"])

_SCHEDULED_TASK_NAMES = frozenset(
    {
        "app.tasks.weather.schedule_daily_weather_fetch",
        "app.tasks.overview_preagg.refresh_daily_satellite",
        "app.tasks.overview_preagg.refresh_overview_stats",
    }
)


class EnsureAdminTaskRunRequest(BaseModel):
    """Beat 使用的幂等运行记录创建请求。"""

    task_key: str = Field(..., min_length=1, max_length=64)
    task_name: str = Field(..., min_length=1, max_length=255)
    execution_key: str = Field(..., min_length=1, max_length=255)
    params: dict[str, Any] = Field(default_factory=dict)


class AdminTaskStatusRequest(BaseModel):
    worker_name: str = Field(default="scheduled-task", min_length=1, max_length=256)
    celery_task_id: str | None = Field(default=None, max_length=255)
    status: Literal["running", "success", "failed", "cancelled"]
    result: Any | None = None
    error: str | None = Field(default=None, max_length=4000)


def _scheduled_run_id(task_key: str, execution_key: str) -> uuid.UUID:
    """同一调度任务和执行窗口复用固定 ID，防止 Beat 重投递生成重复记录。"""
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"agric-satellite/admin-schedule/{task_key}/{execution_key}",
    )


@router.post("/task-runs/ensure")
async def ensure_task_run(
    body: EnsureAdminTaskRunRequest,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """为 Beat 自动执行创建幂等的管理员运行记录。"""
    if body.task_name not in _SCHEDULED_TASK_NAMES:
        raise HTTPException(status_code=400, detail="不支持的自动任务")

    run_id = _scheduled_run_id(body.task_key, body.execution_key)
    run = await db.get(AdminTaskRun, run_id)
    if run is None:
        params = dict(body.params)
        params.update(source="schedule", execution_key=body.execution_key)
        run = AdminTaskRun(
            id=run_id,
            task_key=body.task_key,
            task_name=body.task_name,
            status="queued",
            params_json=params,
            triggered_by="beat",
        )
        db.add(run)
        try:
            await db.commit()
        except IntegrityError:
            # Beat 重新投递可能与上一轮重试并发到达；唯一 ID 冲突时复用已存在记录，
            # 不能因为监控记录重复而阻断真正的下载/聚合任务。
            await db.rollback()
            run = await db.get(AdminTaskRun, run_id)
            if run is None:
                raise
        else:
            await db.refresh(run)
    return {
        "run_id": str(run.id),
        "task_key": run.task_key,
        "task_name": run.task_name,
        "status": run.status,
    }


@router.post("/task-runs/{run_id}/status")
async def update_task_status(
    run_id: uuid.UUID,
    body: AdminTaskStatusRequest,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    run = await db.get(AdminTaskRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="admin task run not found")
    if (
        body.celery_task_id
        and run.celery_task_id
        and run.celery_task_id != body.celery_task_id
    ):
        raise HTTPException(status_code=409, detail="celery task id mismatch")
    if body.celery_task_id:
        run.celery_task_id = body.celery_task_id
    run.status = body.status
    run.updated_at = datetime.now(timezone.utc)
    if body.status == "running":
        run.started_at = run.started_at or run.updated_at
    else:
        run.finished_at = run.finished_at or run.updated_at
        if body.status == "success":
            run.result_json = body.result
            run.error = None
        else:
            run.error = body.error or "Celery task failed"
    await db.commit()
    return {"ok": True, "run_id": str(run.id), "status": run.status}


__all__ = ["router"]
