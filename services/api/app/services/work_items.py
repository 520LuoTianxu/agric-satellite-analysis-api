"""Postgres work_items enqueue + claim helpers (HTTP claim control plane)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from agric_satellite_analysis_common.trace import stamp_trace_on_payload

from app.core.config import settings
from app.core.logging import logger
from app.models.tables import WorkItem

CLAIMABLE_TYPES = frozenset(
    {
        "assessment_report",
        "season_growth_report",
        "land_bootstrap",
        "satellite_analysis",  # agri optical + S1 chunk wave via backfill
        "agri_bridge",
        "weather_backfill",
        "soil_fetch",
    }
)

# Types where claim agent completes the lease after Celery dispatch (fan-out /
# fire-and-forget). Report types stay leased until the Celery task POSTs complete.
COMPLETE_ON_DISPATCH_TYPES = frozenset(
    {
        "land_bootstrap",
        "satellite_analysis",
        "agri_bridge",
        "weather_backfill",
        "soil_fetch",
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
                return str(existing.id)
        item = WorkItem(
            type=type,
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
    """Return expired leases to pending (attempts++)."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(WorkItem)
        .where(
            WorkItem.status == "leased",
            WorkItem.lease_until.is_not(None),
            WorkItem.lease_until < now,
        )
        .values(
            status="pending",
            lease_owner=None,
            lease_until=None,
            attempts=WorkItem.attempts + 1,
            updated_at=now,
        )
        .returning(WorkItem.id)
    )
    ids = list(result.scalars().all())
    if ids:
        logger.info("work_items_lease_reaped", count=len(ids))
    return len(ids)


async def enqueue_work_item(
    db: AsyncSession,
    *,
    type: str,
    payload: dict[str, Any] | None = None,
    priority: int = 0,
    idempotency_key: str | None = None,
) -> WorkItem:
    """Insert a pending work_item (idempotent when key set)."""
    payload = dict(payload or {})
    payload = stamp_trace_on_payload(payload)
    if idempotency_key:
        existing = (
            await db.execute(
                select(WorkItem).where(WorkItem.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing:
            return existing

    item = WorkItem(
        type=type,
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


def _require_lease(item: WorkItem, worker_id: str | None) -> None:
    if item.status != "leased":
        raise ValueError(f"work item status is {item.status}, expected leased")
    if worker_id and item.lease_owner and item.lease_owner != worker_id:
        raise ValueError("lease_owner mismatch")


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
    item.lease_owner = None
    item.lease_until = None
    item.updated_at = now
    if retry:
        item.status = "pending"
    else:
        item.status = "failed"
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
    "claim_work_items",
    "complete_work_item",
    "enqueue_work_item",
    "enqueue_work_item_sync",
    "fail_work_item",
    "heartbeat_work_item",
    "progress_work_item",
    "reaper_expired_leases",
    "should_enqueue_work_items",
    "should_publish_mq",
    "should_run_claim_agent",
    "work_item_idempotency_key",
    "work_queue_mode",
]
