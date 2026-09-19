"""管理员运维面板：下载机心跳与受控 Beat 任务运行记录。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
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
    parent_job_id: uuid.UUID | None = None
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
    parent_job_id: uuid.UUID | None = None
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


class ExecutionGroupOut(BaseModel):
    """任务树的一级节点；列表页只返回父任务和聚合后的子任务计数。"""

    id: str
    parent_job_id: uuid.UUID | None = None
    type: str
    status: str
    land_id: str | None = None
    progress_summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    child_counts: dict[str, int] = Field(default_factory=dict)


class ExecutionGroupDetailOut(ExecutionGroupOut):
    """父任务详情及其所有子 Job/WorkItem，供前端弹窗按需展开。"""

    parent_job: JobDetailOut | None = None
    jobs: list[JobDetailOut] = Field(default_factory=list)
    work_items: list[WorkItemDetailOut] = Field(default_factory=list)


class ExecutionOverviewOut(BaseModel):
    generated_at: datetime
    job_counts: dict[str, int]
    work_item_counts: dict[str, int]
    group_counts: dict[str, int]
    group_has_more: bool
    job_has_more: bool
    work_item_has_more: bool
    groups: list[ExecutionGroupOut]
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


_EXECUTION_TERMINAL_STATUSES = {
    "completed",
    "succeeded",
    "done",
    "success",
    "failed",
    "cancelled",
    "partial",
}
_EXECUTION_SUCCESS_STATUSES = {"completed", "succeeded", "done", "success"}
_EXECUTION_FAILED_STATUSES = {"failed", "cancelled", "partial"}


@dataclass
class _ExecutionGroup:
    """兼容历史 JSON 关联的任务树中间结构，不改变现有业务表。"""

    key: str
    parent_job: Job | None = None
    jobs: list[tuple[Job, uuid.UUID | None]] = field(default_factory=list)
    work_items: list[tuple[WorkItem, uuid.UUID | None]] = field(default_factory=list)
    expected_job_ids: set[uuid.UUID] = field(default_factory=set)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _job_parent_id_from_params(
    job: Job, job_ids: set[uuid.UUID]
) -> uuid.UUID | None:
    params = _json_dict(job.params_json)
    # overview_run_id 是每日全国态势子批次沿用的父任务引用；
    # parent_job_id/parent_id 兼容后续新增的显式父子任务写法。
    for key in ("overview_run_id", "parent_job_id", "parent_id"):
        parent_id = _as_uuid(params.get(key))
        if parent_id and parent_id != job.id and parent_id in job_ids:
            return parent_id
    return None


def _work_item_parent_id(item: WorkItem) -> uuid.UUID | None:
    payload = _json_dict(item.payload_json)
    extras = _json_dict(payload.get("extras"))
    # claim payload 的 job_id 通常放在 extras 内；保留顶层兼容旧任务和手工派发。
    for value in (
        payload.get("job_id"),
        payload.get("parent_job_id"),
        extras.get("job_id"),
        extras.get("parent_job_id"),
    ):
        parent_id = _as_uuid(value)
        if parent_id:
            return parent_id
    return None


def _root_job_id(
    job_id: uuid.UUID, parent_by_child: dict[uuid.UUID, uuid.UUID]
) -> uuid.UUID:
    """把多层父子关系折叠到页面一级节点，且对历史脏数据防循环。"""
    current = job_id
    visited: set[uuid.UUID] = set()
    while current in parent_by_child and current not in visited:
        visited.add(current)
        parent_id = parent_by_child[current]
        if parent_id == current:
            break
        current = parent_id
    return current


def _build_execution_groups(
    jobs: list[Job], work_items: list[WorkItem]
) -> list[_ExecutionGroup]:
    """根据已落库的父子引用生成一级任务组，失败子任务不会被丢失。"""
    jobs_by_id = {job.id: job for job in jobs}
    job_ids = set(jobs_by_id)
    parent_by_child: dict[uuid.UUID, uuid.UUID] = {}
    expected_by_parent: dict[uuid.UUID, set[uuid.UUID]] = {}

    for parent in jobs:
        params = _json_dict(parent.params_json)
        declared_ids = params.get("job_ids")
        if isinstance(declared_ids, list):
            expected = expected_by_parent.setdefault(parent.id, set())
            for raw_id in declared_ids:
                child_id = _as_uuid(raw_id)
                if child_id and child_id != parent.id:
                    expected.add(child_id)
                    # 即使子 Job 尚未落库，也先保留父子路径；这样其迟到的
                    # WorkItem 不会被错误拆成另一个一级任务组。
                    parent_by_child[child_id] = parent.id
        parent_id = _job_parent_id_from_params(parent, job_ids)
        if parent_id:
            parent_by_child[parent.id] = parent_id

    groups: dict[str, _ExecutionGroup] = {}

    def group_for_parent(job_id: uuid.UUID) -> _ExecutionGroup:
        root_id = _root_job_id(job_id, parent_by_child)
        key = f"job:{root_id}"
        group = groups.setdefault(key, _ExecutionGroup(key=key))
        if root_id in jobs_by_id:
            group.parent_job = jobs_by_id[root_id]
        # 父任务的 job_ids 是期望集合，即使某个子 Job 因异常没有落库，
        # 也要在管理页显示为 missing/pending，而不是静默减少总数。
        for parent_id, expected_ids in expected_by_parent.items():
            if _root_job_id(parent_id, parent_by_child) == root_id:
                group.expected_job_ids.update(expected_ids)
        return group

    for job in jobs:
        direct_parent_id = parent_by_child.get(job.id)
        if direct_parent_id:
            group_for_parent(direct_parent_id).jobs.append((job, direct_parent_id))
        else:
            group_for_parent(job.id)

    for item in work_items:
        direct_parent_id = _work_item_parent_id(item)
        if direct_parent_id:
            group_for_parent(direct_parent_id).work_items.append(
                (item, direct_parent_id)
            )
            continue
        key = f"work:{item.id}"
        groups.setdefault(key, _ExecutionGroup(key=key)).work_items.append(
            (item, None)
        )

    return list(groups.values())


def _group_child_counts(group: _ExecutionGroup) -> dict[str, int]:
    # 同一个执行单元可能同时有 Job 和 claim WorkItem 两条记录；当 WorkItem
    # 已经指向某个子 Job 时只按 Job 计进度，避免 20 个任务被错误显示成 40 个。
    child_job_ids = {job.id for job, _ in group.jobs}
    statuses = [job.status for job, _ in group.jobs]
    statuses.extend(
        item.status
        for item, parent_job_id in group.work_items
        if parent_job_id not in child_job_ids
    )
    missing = len(group.expected_job_ids - {job.id for job, _ in group.jobs})
    terminal = sum(status in _EXECUTION_TERMINAL_STATUSES for status in statuses)
    completed = sum(status in _EXECUTION_SUCCESS_STATUSES for status in statuses)
    failed = sum(status in _EXECUTION_FAILED_STATUSES for status in statuses)
    pending = sum(status in {"pending", "queued"} for status in statuses) + missing
    total = len(statuses) + missing
    return {
        "total": total,
        "jobs": len(group.jobs) + missing,
        "work_items": len(group.work_items),
        "terminal": terminal,
        "completed": completed,
        "failed": failed,
        "pending": pending,
        "running": max(0, total - terminal - pending),
        "missing": missing,
    }


def _group_status(group: _ExecutionGroup) -> str:
    # 有真实父 Job 时优先使用父 Job 状态；overview_daily 的 partial 正是
    # “子任务允许失败，但所有子任务已终态”的最终业务状态。
    if group.parent_job is not None:
        return group.parent_job.status
    counts = _group_child_counts(group)
    child_job_ids = {job.id for job, _ in group.jobs}
    statuses = [job.status for job, _ in group.jobs]
    statuses.extend(
        item.status
        for item, parent_job_id in group.work_items
        if parent_job_id not in child_job_ids
    )
    if counts["missing"] or any(
        status not in _EXECUTION_TERMINAL_STATUSES for status in statuses
    ):
        return "running" if counts["running"] else "pending"
    if counts["failed"] and counts["completed"]:
        return "partial"
    if counts["failed"]:
        return "failed"
    return "completed"


def _max_datetime(values: list[datetime | None]) -> datetime | None:
    valid = [value for value in values if value is not None]
    return max(valid) if valid else None


def _group_created_at(group: _ExecutionGroup) -> datetime | None:
    if group.parent_job is not None:
        return group.parent_job.created_at
    return min(
        (value for value in [
            *(job.created_at for job, _ in group.jobs),
            *(item.created_at for item, _ in group.work_items),
        ] if value is not None),
        default=None,
    )


def _group_land_id(group: _ExecutionGroup) -> str | None:
    if group.parent_job is not None and group.parent_job.land_id:
        return group.parent_job.land_id
    for job, _ in group.jobs:
        if job.land_id:
            return job.land_id
    for item, _ in group.work_items:
        land_id = _json_dict(item.payload_json).get("land_id")
        if land_id:
            return str(land_id)
    return None


def _group_type(group: _ExecutionGroup) -> str:
    if group.parent_job is not None:
        return group.parent_job.type
    if group.jobs:
        return group.jobs[0][0].type
    if group.work_items:
        return group.work_items[0][0].type
    return "unknown"


def _group_error(group: _ExecutionGroup) -> str | None:
    if group.parent_job is not None and group.parent_job.error:
        return group.parent_job.error
    for job, _ in group.jobs:
        if job.error:
            return job.error
    for item, _ in group.work_items:
        if item.error:
            return item.error
    return None


def _group_sort_value(group: _ExecutionGroup) -> datetime:
    values: list[datetime | None] = [
        group.parent_job.created_at if group.parent_job is not None else None,
        *(job.created_at for job, _ in group.jobs),
        *(job.finished_at for job, _ in group.jobs),
        *(item.updated_at for item, _ in group.work_items),
    ]
    return _max_datetime(values) or datetime.min.replace(tzinfo=timezone.utc)


def _to_job_monitor_out(
    job: Job, parent_job_id: uuid.UUID | None = None
) -> JobMonitorOut:
    return JobMonitorOut(
        id=job.id,
        land_id=job.land_id,
        type=job.type,
        status=job.status,
        parent_job_id=parent_job_id,
        progress_summary=_progress_summary(job.progress_json),
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _to_job_detail_out(
    job: Job, parent_job_id: uuid.UUID | None = None
) -> JobDetailOut:
    return JobDetailOut(
        **_to_job_monitor_out(job, parent_job_id).model_dump(),
        params_json=dict(job.params_json or {}) if job.params_json else None,
        progress_json=dict(job.progress_json or {}) if job.progress_json else None,
    )


def _to_work_item_monitor_out(
    item: WorkItem, parent_job_id: uuid.UUID | None = None
) -> WorkItemMonitorOut:
    return WorkItemMonitorOut(
        id=item.id,
        type=item.type,
        status=item.status,
        parent_job_id=parent_job_id,
        priority=int(item.priority or 0),
        lease_owner=item.lease_owner,
        lease_until=item.lease_until,
        attempts=int(item.attempts or 0),
        progress_summary=_progress_summary(item.progress_json),
        error=item.error,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _to_work_item_detail_out(
    item: WorkItem, parent_job_id: uuid.UUID | None = None
) -> WorkItemDetailOut:
    return WorkItemDetailOut(
        **_to_work_item_monitor_out(item, parent_job_id).model_dump(),
        idempotency_key=item.idempotency_key,
        payload_json=dict(item.payload_json or {}),
        progress_json=dict(item.progress_json or {}) if item.progress_json else None,
        result_json=dict(item.result_json or {}) if item.result_json else None,
    )


def _to_execution_group_out(group: _ExecutionGroup) -> ExecutionGroupOut:
    parent = group.parent_job
    first_job = group.jobs[0][0] if group.jobs else None
    first_item = group.work_items[0][0] if group.work_items else None
    return ExecutionGroupOut(
        id=group.key,
        parent_job_id=parent.id if parent is not None else None,
        type=_group_type(group),
        status=_group_status(group),
        land_id=_group_land_id(group),
        progress_summary=_progress_summary(
            parent.progress_json
            if parent is not None
            else (first_job.progress_json if first_job is not None else first_item.progress_json if first_item is not None else {})
        ),
        error=_group_error(group),
        created_at=_group_created_at(group),
        started_at=(
            parent.started_at
            if parent is not None
            else min(
                (value for value in [
                    *(job.started_at for job, _ in group.jobs),
                    *(item.created_at for item, _ in group.work_items),
                ] if value is not None),
                default=None,
            )
        ),
        finished_at=(
            parent.finished_at
            if parent is not None
            else _max_datetime([
                *(job.finished_at for job, _ in group.jobs),
                *(item.updated_at for item, _ in group.work_items),
            ])
        ),
        child_counts=_group_child_counts(group),
    )


def _to_execution_group_detail_out(
    group: _ExecutionGroup,
) -> ExecutionGroupDetailOut:
    return ExecutionGroupDetailOut(
        **_to_execution_group_out(group).model_dump(),
        parent_job=(
            _to_job_detail_out(group.parent_job)
            if group.parent_job is not None
            else None
        ),
        jobs=[
            _to_job_detail_out(job, parent_job_id)
            for job, parent_job_id in group.jobs
        ],
        work_items=[
            _to_work_item_detail_out(item, parent_job_id)
            for item, parent_job_id in group.work_items
        ],
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
    group_status: str | None = Query(default=None, max_length=20),
    job_offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    work_item_offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    group_offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
):
    """返回原始计数及父任务列表，父任务详情通过独立接口按需读取。"""
    now = datetime.now(timezone.utc)
    # AsyncSession 不能被多个协程并发使用；这里保持同一事务连接串行查询，
    # 避免管理页高频刷新时触发 SQLAlchemy 的并发状态错误。
    job_counts = await _status_counts(db, Job)
    work_item_counts = await _status_counts(db, WorkItem)

    jobs_stmt = select(Job)
    if job_status:
        jobs_stmt = jobs_stmt.where(Job.status == job_status)
    jobs_stmt = (
        jobs_stmt.order_by(Job.created_at.desc(), Job.id.desc())
        .offset(job_offset)
        .limit(limit)
    )

    work_items_stmt = select(WorkItem)
    if work_item_status:
        work_items_stmt = work_items_stmt.where(WorkItem.status == work_item_status)
    work_items_stmt = (
        work_items_stmt.order_by(WorkItem.updated_at.desc(), WorkItem.id.desc())
        .offset(work_item_offset)
        .limit(limit)
    )

    jobs_result = await db.execute(jobs_stmt)
    work_items_result = await db.execute(work_items_stmt)
    job_rows = jobs_result.scalars().all()
    work_item_rows = work_items_result.scalars().all()

    # 现有库通过 params/payload 中的 JSON 关联父子任务，暂时没有单独的
    # parent_job_id 列；这里用轻量的全量 ORM 行构建一级任务组，保证历史数据
    # 也能被正确归并。后续数据量增长后可将该关联正规化并迁移到索引列。
    all_jobs = (await db.execute(select(Job))).scalars().all()
    all_work_items = (await db.execute(select(WorkItem))).scalars().all()
    groups = sorted(
        _build_execution_groups(all_jobs, all_work_items),
        key=_group_sort_value,
        reverse=True,
    )
    group_counts_counter = Counter(_group_status(group) for group in groups)
    group_counts = {status: int(count) for status, count in group_counts_counter.items()}
    group_counts["all"] = len(groups)
    filtered_groups = (
        [group for group in groups if _group_status(group) == group_status]
        if group_status
        else groups
    )
    group_rows = filtered_groups[group_offset : group_offset + limit]
    job_total = job_counts["all"] if not job_status else job_counts.get(job_status, 0)
    work_item_total = (
        work_item_counts["all"]
        if not work_item_status
        else work_item_counts.get(work_item_status, 0)
    )
    return ExecutionOverviewOut(
        generated_at=now,
        job_counts=job_counts,
        work_item_counts=work_item_counts,
        group_counts=group_counts,
        group_has_more=group_offset + len(group_rows) < len(filtered_groups),
        job_has_more=job_offset + len(job_rows) < job_total,
        work_item_has_more=work_item_offset + len(work_item_rows) < work_item_total,
        groups=[_to_execution_group_out(group) for group in group_rows],
        jobs=[_to_job_monitor_out(item) for item in job_rows],
        work_items=[_to_work_item_monitor_out(item) for item in work_item_rows],
    )


@router.get(
    "/execution-groups/{group_id}", response_model=ExecutionGroupDetailOut
)
async def execution_group_detail(
    group_id: str,
    _: Annotated[OrgContext, Depends(_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """返回一个父任务及全部子 Job/WorkItem，供监控页弹窗查看。"""
    all_jobs = (await db.execute(select(Job))).scalars().all()
    all_work_items = (await db.execute(select(WorkItem))).scalars().all()
    group = next(
        (
            candidate
            for candidate in _build_execution_groups(all_jobs, all_work_items)
            if candidate.key == group_id
        ),
        None,
    )
    if group is None:
        raise HTTPException(status_code=404, detail="execution group not found")
    return _to_execution_group_detail_out(group)


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
