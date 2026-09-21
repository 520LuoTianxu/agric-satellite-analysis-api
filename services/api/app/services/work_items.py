"""Postgres work_items enqueue + claim helpers (HTTP claim control plane)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from agric_satellite_analysis_common.trace import stamp_trace_on_payload

from app.core.config import settings
from app.core.logging import logger
from app.models.tables import DownloadWorker, Job, WorkItem

MAX_WORK_ITEM_ATTEMPTS = 3
WORK_ITEM_LEASE_EXHAUSTED_ERROR = "下载机任务连续3次未完成，已标记失败"

CLAIMABLE_TYPES = frozenset(
    {
        "assessment_report",
        "season_growth_report",
        "land_bootstrap",
        "satellite_analysis",  # agri optical + S1 chunk wave via backfill
        "satellite_batch",  # 请求地块按5×5公里聚合，共用一个下载窗口
        "agri_bridge",
        "weather_backfill",
        "soil_fetch",
        "admin_task",
    }
)

# Types where claim agent completes the lease after Celery dispatch (fan-out /
# fire-and-forget). Report types stay leased until the Celery task POSTs complete.
COMPLETE_ON_DISPATCH_TYPES = frozenset(
    {
        "land_bootstrap",
        "satellite_analysis",
        "satellite_batch",
        "agri_bridge",
        "weather_backfill",
        "soil_fetch",
        "admin_task",
    }
)


def work_queue_mode() -> str:
    mode = (settings.work_queue_mode or "legacy").strip().lower()
    if mode not in ("legacy", "claim", "dual"):
        return "legacy"
    return mode


def should_enqueue_work_items() -> bool:
    return work_queue_mode() in ("claim", "dual")


def should_publish_mq() -> bool:
    return work_queue_mode() in ("legacy", "dual")


def should_run_claim_agent() -> bool:
    """Download claim poller may run only in claim mode (never dual).

    dual = API enqueues work_items AND publishes MQ; download must consume MQ
    only, otherwise the same logical task is double-dispatched.
    """
    return work_queue_mode() == "claim"


def work_item_idempotency_key(
    type: str,
    *,
    task_id: str | None = None,
    extras: dict[str, Any] | None = None,
) -> str | None:
    """Stable idempotency key for enqueue from publish / routers."""
    extras = extras or {}
    job_id = extras.get("job_id")
    if type in ("assessment_report", "season_growth_report") and job_id:
        return f"{type}:{job_id}"
    if type == "soil_fetch" and job_id:
        return f"{type}:{job_id}"
    if task_id:
        return f"{type}:{task_id}"
    return None


def _parent_job_id_from_payload(payload: dict[str, Any]) -> uuid.UUID | None:
    """从派发载荷提取父 Job，写入索引列供运维任务树快速查询。"""
    extras = payload.get("extras")
    extras = extras if isinstance(extras, dict) else {}
    # 普通任务直接把 job_id 放在 payload/extras；一键报告的下载机任务
    # 会先执行 land_bootstrap，再通过 followup_* 派发报告，因此父 Job
    # 还可能嵌套在 followup_assessment/followup_season_growth 中。
    values = [
        payload.get("job_id"),
        payload.get("parent_job_id"),
        extras.get("job_id"),
        extras.get("parent_job_id"),
        extras.get("sentinel_job_id"),
        extras.get("bridge_job_id"),
    ]
    for container in (payload, extras):
        for followup_key in ("followup_assessment", "followup_season_growth"):
            followup = container.get(followup_key)
            if isinstance(followup, dict):
                values.extend(
                    [followup.get("job_id"), followup.get("parent_job_id")]
                )

    for value in values:
        if isinstance(value, uuid.UUID):
            return value
        if isinstance(value, str):
            try:
                return uuid.UUID(value)
            except ValueError:
                continue
    return None


def _revive_failed_work_item(item: WorkItem, *, now: datetime) -> bool:
    """重派同一幂等任务时恢复失败工作项，避免生成重复的下载任务。"""
    if item.status != "failed":
        return False
    progress = dict(item.progress_json or {})
    try:
        requeue_count = int(progress.get("requeue_count") or 0) + 1
    except (TypeError, ValueError):
        requeue_count = 1
    if item.error:
        progress["last_failed_error"] = item.error
    progress.update(
        {
            "requeue_count": requeue_count,
            "requeued_at": now.isoformat(),
        }
    )
    item.status = "pending"
    item.lease_owner = None
    item.lease_until = None
    item.attempts = 0
    item.error = None
    item.result_json = None
    item.progress_json = progress
    item.updated_at = now
    return True


def enqueue_work_item_sync(
    *,
    type: str,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
    idempotency_key: str | None = None,
) -> str | None:
    """Sync insert for publish_api_task path (API has DATABASE_URL).

    Returns work item id string, or None when type is not claimable.
    """
    if type not in CLAIMABLE_TYPES:
        return None
    payload = dict(payload or {})
    payload = stamp_trace_on_payload(payload)
    from agric_satellite_analysis_common.database_sync import SyncSession
    from app.models.tables import WorkItem

    session = SyncSession()
    try:
        if idempotency_key:
            existing = session.execute(
                select(WorkItem).where(WorkItem.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            if existing:
                if _revive_failed_work_item(
                    existing, now=datetime.now(timezone.utc)
                ):
                    session.commit()
                return str(existing.id)
        item = WorkItem(
            type=type,
            parent_job_id=_parent_job_id_from_payload(payload),
            payload_json=payload,
            status="pending",
            priority=int(priority),
            idempotency_key=idempotency_key,
            attempts=0,
        )
        session.add(item)
        session.commit()
        logger.info(
            "work_item_enqueued_sync",
            work_id=str(item.id),
            type=type,
            idempotency_key=idempotency_key,
        )
        return str(item.id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def reaper_expired_leases(db: AsyncSession) -> int:
    """Return expired leases to pending; exhaust the third claim as failed."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(WorkItem)
        .where(
            WorkItem.status == "leased",
            WorkItem.lease_until.is_not(None),
            WorkItem.lease_until < now,
        )
        .with_for_update(skip_locked=True)
    )
    items = list(result.scalars().all())
    for item in items:
        item.lease_owner = None
        item.lease_until = None
        item.updated_at = now
        # attempts只在真正领取时递增；这样“第三次领取后仍未完成”才是第三次失败。
        if int(item.attempts or 0) >= MAX_WORK_ITEM_ATTEMPTS:
            item.status = "failed"
            item.error = WORK_ITEM_LEASE_EXHAUSTED_ERROR
            await _mark_parent_job_failed(
                db, item, error=WORK_ITEM_LEASE_EXHAUSTED_ERROR, now=now
            )
        else:
            item.status = "pending"
    if items:
        await db.flush()
        logger.info("work_items_lease_reaped", count=len(items))
    return len(items)


async def enqueue_work_item(
    db: AsyncSession,
    *,
    type: str,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
    idempotency_key: str | None = None,
) -> WorkItem:
    """Insert a pending work_item; revive a failed row when the same key is retried."""
    payload = dict(payload or {})
    payload = stamp_trace_on_payload(payload)
    if idempotency_key:
        existing = (
            await db.execute(
                select(WorkItem).where(WorkItem.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing:
            if _revive_failed_work_item(existing, now=datetime.now(timezone.utc)):
                await db.flush()
            return existing

    item = WorkItem(
        type=type,
        parent_job_id=_parent_job_id_from_payload(payload),
        payload_json=payload,
        status="pending",
        priority=int(priority),
        idempotency_key=idempotency_key,
        attempts=0,
    )
    db.add(item)
    await db.flush()
    logger.info(
        "work_item_enqueued",
        work_id=str(item.id),
        type=type,
        idempotency_key=idempotency_key,
    )
    return item


async def claim_work_items(
    db: AsyncSession,
    *,
    worker_id: str,
    types: Sequence[str] | None = None,
    limit: int = 1,
    lease_seconds: int | None = None,
) -> list[WorkItem]:
    """Atomically claim pending rows with FOR UPDATE SKIP LOCKED."""
    if settings.work_reaper_on_claim:
        await reaper_expired_leases(db)

    limit = max(1, min(int(limit or settings.work_claim_default_limit), 50))
    lease_seconds = int(lease_seconds or settings.work_lease_seconds)
    lease_seconds = max(30, min(lease_seconds, 3600))
    now = datetime.now(timezone.utc)
    lease_until = now + timedelta(seconds=lease_seconds)

    type_filter = list(types) if types else None
    if type_filter:
        type_filter = [t for t in type_filter if t]
        if not type_filter:
            type_filter = None

    stmt = (
        select(WorkItem)
        .where(WorkItem.status == "pending")
        .order_by(WorkItem.priority.desc(), WorkItem.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if type_filter:
        stmt = stmt.where(WorkItem.type.in_(type_filter))

    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    for row in rows:
        row.status = "leased"
        row.lease_owner = worker_id
        # 每次重新领取都覆盖最近领取机；租约释放时不清除此字段，保留完整追溯信息。
        row.last_claimed_by = worker_id
        row.lease_until = lease_until
        row.attempts = int(row.attempts or 0) + 1
        row.updated_at = now
    if rows:
        await db.flush()
        logger.info(
            "work_items_claimed",
            count=len(rows),
            worker_id=worker_id,
            ids=[str(r.id) for r in rows],
        )
    return rows


async def touch_download_worker(
    db: AsyncSession,
    *,
    worker_id: str,
    claim_types: Sequence[str] | None,
    poll_interval_seconds: int,
    claim_count: int,
    queue_name: str,
    pending_queue_count: int | None,
    queue_depths: dict[str, int] | None,
) -> None:
    """记录一次成功 claim 请求，空队列也算心跳。"""
    now = datetime.now(timezone.utc)
    interval = max(1, min(int(poll_interval_seconds or 4), 3600))
    types = [str(item) for item in (claim_types or []) if str(item)]
    queue_name = (queue_name or "cpu_compute").strip()[:128] or "cpu_compute"
    pending = (
        max(0, min(int(pending_queue_count), 2_000_000_000))
        if pending_queue_count is not None
        else None
    )
    depths = {
        str(name)[:128]: max(0, min(int(count), 2_000_000_000))
        for name, count in (queue_depths or {}).items()
        if str(name).strip()
    }
    stmt = pg_insert(DownloadWorker).values(
        worker_id=worker_id,
        mode="claim",
        claim_types_json=types,
        poll_interval_seconds=interval,
        last_claim_count=int(claim_count),
        total_claims=1,
        queue_name=queue_name,
        queue_depths_json=depths,
        pending_queue_count=pending,
        last_claim_at=now,
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[DownloadWorker.worker_id],
        set_={
            "mode": "claim",
            "claim_types_json": types,
            "poll_interval_seconds": interval,
            "last_claim_count": int(claim_count),
            "total_claims": DownloadWorker.total_claims + 1,
            "queue_name": queue_name,
            "queue_depths_json": depths,
            "pending_queue_count": pending,
            "last_claim_at": now,
            "updated_at": now,
        },
    )
    await db.execute(stmt)


def _require_lease(item: WorkItem, worker_id: str | None) -> None:
    if item.status != "leased":
        raise ValueError(f"work item status is {item.status}, expected leased")
    if worker_id and item.lease_owner and item.lease_owner != worker_id:
        raise ValueError("lease_owner mismatch")


async def _mark_parent_job_failed(
    db: AsyncSession,
    item: WorkItem,
    *,
    error: str,
    now: datetime,
) -> None:
    """工作项重试耗尽时同步结束关联 Job，避免父任务永久等待。"""
    job_id = item.parent_job_id or _parent_job_id_from_payload(
        dict(item.payload_json or {})
    )
    if job_id is None:
        return
    job = await db.get(Job, job_id)
    if job is None or job.status in {"completed", "succeeded", "failed", "cancelled"}:
        return
    progress = dict(job.progress_json or {})
    progress.update(
        {
            "work_item_failed": True,
            "work_item_id": str(item.id),
            "work_item_attempts": int(item.attempts or 0),
        }
    )
    job.status = "failed"
    job.progress_json = progress
    job.error = (error or WORK_ITEM_LEASE_EXHAUSTED_ERROR)[:4000]
    job.finished_at = now


async def heartbeat_work_item(
    db: AsyncSession,
    work_id: uuid.UUID,
    *,
    worker_id: str | None = None,
    lease_seconds: int | None = None,
) -> WorkItem:
    item = await db.get(WorkItem, work_id)
    if not item:
        raise LookupError("not found")
    _require_lease(item, worker_id)
    lease_seconds = int(lease_seconds or settings.work_lease_seconds)
    lease_seconds = max(30, min(lease_seconds, 3600))
    now = datetime.now(timezone.utc)
    item.lease_until = now + timedelta(seconds=lease_seconds)
    item.updated_at = now
    await db.flush()
    return item


async def progress_work_item(
    db: AsyncSession,
    work_id: uuid.UUID,
    *,
    progress: dict[str, Any],
    worker_id: str | None = None,
) -> WorkItem:
    item = await db.get(WorkItem, work_id)
    if not item:
        raise LookupError("not found")
    _require_lease(item, worker_id)
    item.progress_json = dict(progress or {})
    item.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return item


async def complete_work_item(
    db: AsyncSession,
    work_id: uuid.UUID,
    *,
    result: dict[str, Any] | None = None,
    worker_id: str | None = None,
    apply_result: bool = True,
) -> WorkItem:
    item = await db.get(WorkItem, work_id)
    if not item:
        raise LookupError("not found")
    if item.status == "done":
        return item
    _require_lease(item, worker_id)
    now = datetime.now(timezone.utc)
    result_dict = dict(result or {})
    item.status = "done"
    item.result_json = result_dict
    # lease_owner 是临时租约状态，last_claimed_by 才是需要长期保留的领取记录。
    if not item.last_claimed_by:
        item.last_claimed_by = item.lease_owner
    item.lease_owner = None
    item.lease_until = None
    item.error = None
    item.updated_at = now
    await db.flush()
    logger.info("work_item_completed", work_id=str(work_id), type=item.type)

    # D3: apply domain upserts / job metadata via shared result_apply (SyncSession).
    if apply_result and result_dict:
        try:
            import asyncio
            from agric_satellite_analysis_common.result_apply import apply_complete_result

            apply_stats = await asyncio.to_thread(apply_complete_result, result_dict)
            # Stash apply stats only when something ran (avoid noise on ack payloads)
            if isinstance(apply_stats, dict) and not apply_stats.get("skipped"):
                merged = dict(result_dict)
                merged["_apply"] = apply_stats
                item.result_json = merged
                await db.flush()
        except Exception as exc:
            logger.exception(
                "work_item_complete_apply_failed",
                work_id=str(work_id),
                error=str(exc),
            )
            # Do not fail the lease completion — result_json is stored; ops can replay.
    return item


async def fail_work_item(
    db: AsyncSession,
    work_id: uuid.UUID,
    *,
    error: str,
    worker_id: str | None = None,
    retry: bool = False,
) -> WorkItem:
    item = await db.get(WorkItem, work_id)
    if not item:
        raise LookupError("not found")
    if item.status in ("done", "failed") and not retry:
        return item
    if item.status == "leased":
        _require_lease(item, worker_id)
    now = datetime.now(timezone.utc)
    item.error = (error or "failed")[:4000]
    # 失败或重试只释放当前租约，保留最近领取机用于排障和审计。
    if not item.last_claimed_by:
        item.last_claimed_by = item.lease_owner
    item.lease_owner = None
    item.lease_until = None
    item.updated_at = now
    # 下载机派发失败可回队，但第三次领取仍失败时必须在API侧收敛为终态。
    retry = bool(retry) and int(item.attempts or 0) < MAX_WORK_ITEM_ATTEMPTS
    if retry:
        item.status = "pending"
    else:
        item.status = "failed"
        await _mark_parent_job_failed(db, item, error=item.error, now=now)
    await db.flush()
    logger.info(
        "work_item_failed",
        work_id=str(work_id),
        type=item.type,
        retry=retry,
    )
    return item


__all__ = [
    "CLAIMABLE_TYPES",
    "COMPLETE_ON_DISPATCH_TYPES",
    "MAX_WORK_ITEM_ATTEMPTS",
    "WORK_ITEM_LEASE_EXHAUSTED_ERROR",
    "claim_work_items",
    "complete_work_item",
    "enqueue_work_item",
    "enqueue_work_item_sync",
    "fail_work_item",
    "heartbeat_work_item",
    "progress_work_item",
    "reaper_expired_leases",
    "touch_download_worker",
    "should_enqueue_work_items",
    "should_publish_mq",
    "should_run_claim_agent",
    "work_item_idempotency_key",
    "work_queue_mode",
]
