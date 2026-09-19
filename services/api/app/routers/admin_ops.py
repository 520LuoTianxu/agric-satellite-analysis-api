"""管理员运维面板：下载机心跳与受控 Beat 任务运行记录。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session, get_db
from app.core.config import settings
from app.models.tables import AdminTaskRun, DownloadWorker, Job, WorkItem
from app.middleware.auth import OrgContext, require_roles
from app.services import work_items as wi

router = APIRouter(prefix="/admin/ops", tags=["admin-ops"])
_admin = require_roles("owner", "admin")

_TASK_CATALOG: dict[str, dict[str, str]] = {
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
    "mysql-land-sync": {
        "label": "同步 MySQL 地块数据",
        "description": "从外部 MySQL 同步地块主数据到 PostgreSQL，并为新增或变更地块派发遥感处理任务。",
        "task_name": "app.services.mysql_land_sync.run_land_sync",
        "schedule": "每天 23:00（北京时间，API 机）",
    },
}

# 取消后的运行记录不再读取 Celery 结果覆盖，避免页面重新刷新后恢复成运行中。
_TERMINAL_STATUSES = {"success", "failed", "cancelled"}


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


class JobMonitorOut(BaseModel):
    id: uuid.UUID
    land_id: str | None = None
    type: str
    status: str
    progress_summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobDetailOut(JobMonitorOut):
    params_json: dict[str, Any] | None = None
    progress_json: dict[str, Any] | None = None


class WorkItemMonitorOut(BaseModel):
    id: uuid.UUID
    type: str
    status: str
    priority: int
    lease_owner: str | None = None
    lease_until: datetime | None = None
    attempts: int
    progress_summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WorkItemDetailOut(WorkItemMonitorOut):
    idempotency_key: str | None = None
    payload_json: dict[str, Any] = Field(default_factory=dict)
    progress_json: dict[str, Any] | None = None
    result_json: dict[str, Any] | None = None


class ExecutionOverviewOut(BaseModel):
    generated_at: datetime
    job_counts: dict[str, int]
    work_item_counts: dict[str, int]
    jobs: list[JobMonitorOut]
    work_items: list[WorkItemMonitorOut]


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
        queue_name=worker.queue_name or "cpu_compute",
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


_PROGRESS_SUMMARY_KEYS = (
    "phase",
    "current_step",
    "message",
    "total",
    "completed",
    "scenes_total",
    "scenes_done",
    "products_published",
    "failed",
    "pending_jobs",
    "results_pending",
    "land_count",
    "batch_count",
    "rows_count",
)


def _progress_summary(progress: Any) -> dict[str, Any]:
    """只提取列表页需要的轻量进度字段，完整 JSON 通过详情接口按需读取。"""
    if not isinstance(progress, dict):
        return {}
    return {
        key: progress[key]
        for key in _PROGRESS_SUMMARY_KEYS
        if key in progress and isinstance(progress[key], (str, int, float, bool))
    }


def _to_job_monitor_out(job: Job) -> JobMonitorOut:
    return JobMonitorOut(
        id=job.id,
        land_id=job.land_id,
        type=job.type,
        status=job.status,
        progress_summary=_progress_summary(job.progress_json),
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _to_job_detail_out(job: Job) -> JobDetailOut:
    return JobDetailOut(
        **_to_job_monitor_out(job).model_dump(),
        params_json=dict(job.params_json or {}) if job.params_json else None,
        progress_json=dict(job.progress_json or {}) if job.progress_json else None,
    )


def _to_work_item_monitor_out(item: WorkItem) -> WorkItemMonitorOut:
    return WorkItemMonitorOut(
        id=item.id,
        type=item.type,
        status=item.status,
        priority=int(item.priority or 0),
        lease_owner=item.lease_owner,
        lease_until=item.lease_until,
        attempts=int(item.attempts or 0),
        progress_summary=_progress_summary(item.progress_json),
        error=item.error,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _to_work_item_detail_out(item: WorkItem) -> WorkItemDetailOut:
    return WorkItemDetailOut(
        **_to_work_item_monitor_out(item).model_dump(),
        idempotency_key=item.idempotency_key,
        payload_json=dict(item.payload_json or {}),
        progress_json=dict(item.progress_json or {}) if item.progress_json else None,
        result_json=dict(item.result_json or {}) if item.result_json else None,
    )


async def _status_counts(db: AsyncSession, model: Any) -> dict[str, int]:
    rows = (
        await db.execute(
            select(model.status, func.count(model.id)).group_by(model.status)
        )
    ).all()
    counts = {str(status): int(count) for status, count in rows}
    counts["all"] = sum(counts.values())
    return counts


def _enabled_task_keys() -> set[str]:
    """读取与 Beat 共用的环境开关，仅用于管理页展示。"""
    from agric_satellite_analysis_common.celery_app import enabled_beat_schedule

    return set(enabled_beat_schedule())


def _task_outputs() -> list[ScheduledTaskOut]:
    enabled_names = _enabled_task_keys()
    switch_name_by_key = {
        "daily-weather": "fetch-weather-daily",
        "daily-satellite": "refresh-satellite-overview-daily",
        "overview-refresh": "refresh-overview-stats-daily",
    }
    result: list[ScheduledTaskOut] = []
    for key, item in _TASK_CATALOG.items():
        # MySQL 源只允许 API 机访问，所以它不是 Celery/download worker 任务，
        # 页面仍提供手动触发，但启用状态直接反映 API 的源开关。
        enabled = (
            settings.mysql_source_enabled
            if key == "mysql-land-sync"
            else switch_name_by_key[key] in enabled_names
        )
        result.append(
            ScheduledTaskOut(
                key=key,
                label=item["label"],
                description=item["description"],
                task_name=item["task_name"],
                schedule=item["schedule"],
                enabled=enabled,
            )
        )
    return result


async def _run_api_admin_task(run_id: uuid.UUID) -> None:
    """在 API 进程执行仅 API 可访问的管理员任务，并持久化最终状态。"""
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        run = await db.get(AdminTaskRun, run_id)
        if run is None:
            return
        run.status = "running"
        run.started_at = run.started_at or now
        run.updated_at = now
        await db.commit()

    try:
        from app.services.mysql_land_sync import run_land_sync

        # MySQL 凭据只在 API 机，不能通过 claim payload 或 Celery 传给下载机。
        result = await run_land_sync()
    except Exception as exc:
        async with async_session() as db:
            run = await db.get(AdminTaskRun, run_id)
            if run is not None:
                run.status = "failed"
                run.error = str(exc)[:4000]
                run.finished_at = datetime.now(timezone.utc)
                run.updated_at = run.finished_at
                await db.commit()
        return

    async with async_session() as db:
        run = await db.get(AdminTaskRun, run_id)
        if run is not None:
            run.status = "success"
            run.result_json = result
            run.finished_at = datetime.now(timezone.utc)
            run.updated_at = run.finished_at
            await db.commit()


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


@router.get("/execution", response_model=ExecutionOverviewOut)
async def execution_overview(
    _: Annotated[OrgContext, Depends(_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=200),
    job_status: str | None = Query(default=None, max_length=20),
    work_item_status: str | None = Query(default=None, max_length=20),
):
    """返回全量状态统计和最近执行明细，供管理员定位“看似未完成”的任务。"""
    now = datetime.now(timezone.utc)
    # AsyncSession 不能被多个协程并发使用；这里保持同一事务连接串行查询，
    # 避免管理页高频刷新时触发 SQLAlchemy 的并发状态错误。
    job_counts = await _status_counts(db, Job)
    work_item_counts = await _status_counts(db, WorkItem)

    jobs_stmt = select(Job)
    if job_status:
        jobs_stmt = jobs_stmt.where(Job.status == job_status)
    jobs_stmt = jobs_stmt.order_by(Job.created_at.desc()).limit(limit)

    work_items_stmt = select(WorkItem)
    if work_item_status:
        work_items_stmt = work_items_stmt.where(WorkItem.status == work_item_status)
    work_items_stmt = work_items_stmt.order_by(WorkItem.updated_at.desc()).limit(limit)

    jobs = await db.execute(jobs_stmt)
    work_items = await db.execute(work_items_stmt)
    return ExecutionOverviewOut(
        generated_at=now,
        job_counts=job_counts,
        work_item_counts=work_item_counts,
        jobs=[_to_job_monitor_out(item) for item in jobs.scalars().all()],
        work_items=[
            _to_work_item_monitor_out(item) for item in work_items.scalars().all()
        ],
    )


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
async def job_detail(
    job_id: uuid.UUID,
    _: Annotated[OrgContext, Depends(_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """读取单个 Job 的完整参数和进度，列表页只在展开时调用。"""
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _to_job_detail_out(job)


@router.get("/work-items/{work_item_id}", response_model=WorkItemDetailOut)
async def work_item_detail(
    work_item_id: uuid.UUID,
    _: Annotated[OrgContext, Depends(_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """读取单个 work_item 的完整 payload、租约和结果。"""
    item = await db.get(WorkItem, work_item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="work item not found")
    return _to_work_item_detail_out(item)


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
    if body.task_key == "mysql-land-sync":
        # 该同步器会访问 API 机上的源 MySQL，并在 PostgreSQL 内做批量 upsert；
        # 不放入下载机 claim 队列，避免泄露源库连接信息且不占用卫星下载 worker。
        await db.commit()
        asyncio.create_task(_run_api_admin_task(run.id))
        return _to_task_out(run)
    # 将管理员运行 ID 传给任务本身；下载机 claim/legacy 两种模式都能回写真实状态，
    # 同时避免手动触发任务被误认为是 Beat 自动执行记录。
    kwargs["admin_task_run_id"] = str(run.id)
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
