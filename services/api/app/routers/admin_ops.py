"""管理员运维面板：下载机心跳与受控 Beat 任务运行记录。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import settings
from app.models.tables import AdminTaskRun, DownloadWorker
from app.middleware.auth import OrgContext, require_roles
from app.services import work_items as wi

router = APIRouter(prefix="/admin/ops", tags=["admin-ops"])
_admin = require_roles("owner", "admin")

_TASK_CATALOG: dict[str, dict[str, str]] = {
    "weekly-index": {
        "label": "每周指数计算",
        "description": "发现过期地块并派发统一光学指数计算。",
        "task_name": "app.tasks.backfill.schedule_weekly_index_compute",
        "schedule": "每周一 14:00（北京时间）",
    },
    "daily-weather": {
        "label": "每日天气拉取",
        "description": "按有效地块批量拉取天气与历史补齐数据。",
        "task_name": "app.tasks.weather.schedule_daily_weather_fetch",
        "schedule": "每天 16:00（北京时间）",
    },
    "daily-satellite": {
        "label": "每日卫星刷新",
        "description": "准备每日卫星增量下载，并在结果齐备后生成态势快照。",
        "task_name": "app.tasks.overview_preagg.refresh_daily_satellite",
        "schedule": "每天 19:15（北京时间）",
    },
    "overview-refresh": {
        "label": "总览预聚合",
        "description": "刷新全国和分省总览统计缓存。",
        "task_name": "app.tasks.overview_preagg.refresh_overview_stats",
        "schedule": "每天 02:30（北京时间）",
    },
}

_TERMINAL_STATUSES = {"success", "failed"}


class ScheduledTaskOut(BaseModel):
    key: str
    label: str
    description: str
    task_name: str
    schedule: str
    enabled: bool


class DownloadWorkerOut(BaseModel):
    worker_id: str
    mode: str
    status: str
    claim_types: list[str]
    poll_interval_seconds: int
    last_claim_count: int
    total_claims: int
    queue_name: str
    queue_depths: dict[str, int]
    pending_queue_count: int | None
    last_claim_at: datetime | None
    age_seconds: int | None


class TaskRunOut(BaseModel):
    id: uuid.UUID
    task_key: str
    label: str
    task_name: str
    celery_task_id: str | None
    status: str
    params: dict[str, Any]
    result: Any | None
    error: str | None
    triggered_by: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime | None


class AdminOpsOverviewOut(BaseModel):
    generated_at: datetime
    workers: list[DownloadWorkerOut]
    tasks: list[ScheduledTaskOut]
    runs: list[TaskRunOut]


class TriggerTaskRequest(BaseModel):
    task_key: str = Field(..., min_length=1, max_length=64)
    as_of: date | None = None
    window_days: int = Field(default=60, ge=1, le=365)
    crop: str | None = Field(default=None, max_length=64)


def _celery_task_state(task_id: str) -> tuple[str, Any | None, str | None]:
    """读取 Celery 结果后转成可存入 JSONB 的基础值。"""
    from app.worker import celery_app

    result = celery_app.AsyncResult(task_id)
    state = (result.state or "PENDING").upper()
    value = result.result
    error: str | None = None
    if state in {"FAILURE", "REVOKED"}:
        error = str(value)[:4000]
        value = None
    else:
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            value = str(value)
    return state, value, error


async def _refresh_run_status(run: AdminTaskRun) -> None:
    """从共享 Celery result backend 同步一次运行状态。"""
    if not run.celery_task_id or run.status in _TERMINAL_STATUSES:
        return
    try:
        state, result, error = await asyncio.to_thread(
            _celery_task_state, run.celery_task_id
        )
    except Exception:
        # 结果后端短暂不可用时保留上次状态，避免把“未知”误报为失败。
        return

    now = datetime.now(timezone.utc)
    if state == "STARTED":
        run.status = "running"
        run.started_at = run.started_at or now
    elif state == "RETRY":
        run.status = "running"
        run.started_at = run.started_at or now
    elif state == "SUCCESS":
        run.status = "success"
        run.result_json = result
        run.finished_at = run.finished_at or now
    elif state in {"FAILURE", "REVOKED"}:
        run.status = "failed"
        run.error = error or f"Celery task state: {state}"
        run.finished_at = run.finished_at or now
    # PENDING 可能只是任务还未被 worker 取走，仍显示 queued。
    run.updated_at = now


def _worker_status(worker: DownloadWorker, now: datetime) -> tuple[str, int | None]:
    if worker.last_claim_at is None:
        return "unknown", None
    last_claim = worker.last_claim_at
    if last_claim.tzinfo is None:
        last_claim = last_claim.replace(tzinfo=timezone.utc)
    age = max(0, int((now - last_claim).total_seconds()))
    timeout = max(30, int(worker.poll_interval_seconds or 4) * 3)
    if age <= timeout:
        return "online", age
    if age <= timeout * 2:
        return "stale", age
    return "offline", age


def _to_worker_out(worker: DownloadWorker, now: datetime) -> DownloadWorkerOut:
    status, age = _worker_status(worker, now)
    return DownloadWorkerOut(
        worker_id=worker.worker_id,
        mode=worker.mode,
        status=status,
        claim_types=list(worker.claim_types_json or []),
        poll_interval_seconds=int(worker.poll_interval_seconds or 4),
        last_claim_count=int(worker.last_claim_count or 0),
        total_claims=int(worker.total_claims or 0),
        queue_name=worker.queue_name or "ingest",
        queue_depths={
            str(name): int(count)
            for name, count in (worker.queue_depths_json or {}).items()
        },
        pending_queue_count=(
            int(worker.pending_queue_count)
            if worker.pending_queue_count is not None
            else None
        ),
        last_claim_at=worker.last_claim_at,
        age_seconds=age,
    )


def _to_task_out(run: AdminTaskRun) -> TaskRunOut:
    catalog = _TASK_CATALOG.get(run.task_key, {})
    return TaskRunOut(
        id=run.id,
        task_key=run.task_key,
        label=catalog.get("label", run.task_key),
        task_name=run.task_name,
        celery_task_id=run.celery_task_id,
        status=run.status,
        params=dict(run.params_json or {}),
        result=run.result_json,
        error=run.error,
        triggered_by=run.triggered_by,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        updated_at=run.updated_at,
    )


def _enabled_task_keys() -> set[str]:
    """读取与 Beat 共用的环境开关，仅用于管理页展示。"""
    from agric_satellite_analysis_common.celery_app import enabled_beat_schedule

    return set(enabled_beat_schedule())


def _task_outputs() -> list[ScheduledTaskOut]:
    enabled_names = _enabled_task_keys()
    switch_name_by_key = {
        "weekly-index": "compute-indices-weekly",
        "daily-weather": "fetch-weather-daily",
        "daily-satellite": "refresh-satellite-overview-daily",
        "overview-refresh": "refresh-overview-stats-daily",
    }
    return [
        ScheduledTaskOut(
            key=key,
            label=item["label"],
            description=item["description"],
            task_name=item["task_name"],
            schedule=item["schedule"],
            enabled=switch_name_by_key[key] in enabled_names,
        )
        for key, item in _TASK_CATALOG.items()
    ]


@router.get("/overview", response_model=AdminOpsOverviewOut)
async def overview(
    _: Annotated[OrgContext, Depends(_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=30, ge=1, le=100),
):
    now = datetime.now(timezone.utc)
    workers = (
        await db.execute(select(DownloadWorker).order_by(DownloadWorker.worker_id))
    ).scalars().all()
    runs = (
        await db.execute(
            select(AdminTaskRun)
            .order_by(AdminTaskRun.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    for run in runs:
        await _refresh_run_status(run)
    await db.commit()
    return AdminOpsOverviewOut(
        generated_at=now,
        workers=[_to_worker_out(worker, now) for worker in workers],
        tasks=_task_outputs(),
        runs=[_to_task_out(run) for run in runs],
    )


@router.post("/task-runs", response_model=TaskRunOut, status_code=202)
async def trigger_task(
    body: TriggerTaskRequest,
    ctx: Annotated[OrgContext, Depends(_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    definition = _TASK_CATALOG.get(body.task_key)
    if definition is None:
        raise HTTPException(status_code=400, detail="不支持的定时任务")

    kwargs: dict[str, Any] = {}
    params: dict[str, Any] = {}
    if body.task_key == "daily-satellite" and body.as_of:
        kwargs["as_of"] = body.as_of.isoformat()
        params["as_of"] = body.as_of.isoformat()
    if body.task_key == "overview-refresh":
        kwargs = {"window_days": body.window_days}
        params["window_days"] = body.window_days
        if body.crop:
            kwargs["crop"] = body.crop
            params["crop"] = body.crop

    run = AdminTaskRun(
        task_key=body.task_key,
        task_name=definition["task_name"],
        status="queued",
        params_json=params,
        triggered_by=str(ctx.user.id),
    )
    db.add(run)
    await db.flush()
    if settings.work_queue_mode == "claim":
        # claim 模式的 Celery Redis 在下载机本地，先交给下载机再投递本机队列。
        await wi.enqueue_work_item(
            db,
            type="admin_task",
            payload={
                "admin_task_run_id": str(run.id),
                "task_key": body.task_key,
                "task_name": definition["task_name"],
                "kwargs": kwargs,
            },
        )
        await db.commit()
        return _to_task_out(run)
    try:
        from app.celery_client import send_task

        async_result = await asyncio.to_thread(
            send_task, definition["task_name"], kwargs=kwargs, queue="ingest"
        )
        run.celery_task_id = str(async_result.id)
    except Exception as exc:
        run.status = "failed"
        run.error = f"任务投递失败：{str(exc)[:3900]}"
        run.finished_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(status_code=503, detail="定时任务投递失败") from exc
    await db.commit()
    return _to_task_out(run)


__all__ = ["router"]
