"""恢复通用卫星批任务的持久化MQ派发意图。"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import uuid

from sqlalchemy import text

from app.core.logging import logger

OUTBOX_POLL_SECONDS = 2
OUTBOX_CLAIM_LIMIT = 25
OUTBOX_LEASE_SECONDS = 900

_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?i)(['"]?(?:password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret[_-]?key|secret|api[_-]?key|access[_-]?key)['"]?\s*[:=]\s*)(?:'[^']*'|"[^"]*"|[^,\s;&}]+)"""
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*")

# Worker 已开始的Job视为派发成功；终态Job则丢弃意图，避免不必要的重复投递。
_RECONCILE_STARTED_OR_TERMINAL_JOBS = text(
    """
    WITH reconciled AS (
        UPDATE agric_satellite.satellite_batch_dispatch_outbox AS outbox
        SET status = CASE WHEN job.status = 'running' THEN 'completed' ELSE 'discarded' END,
            lease_owner = NULL,
            lease_until = NULL,
            last_error = NULL,
            completed_at = now(),
            updated_at = now()
        FROM agric_satellite.jobs AS job
        WHERE outbox.task_id = job.id
          AND outbox.status IN ('pending', 'processing')
          AND (outbox.status = 'pending' OR outbox.lease_until <= now())
          AND job.status IN (
              'running', 'completed', 'succeeded', 'done', 'success', 'failed',
              'cancelled', 'partial'
          )
        RETURNING outbox.task_id, outbox.attempts, outbox.status
      )
    UPDATE agric_satellite.jobs AS job
    SET progress_json =
        ((CASE WHEN jsonb_typeof(job.progress_json) = 'object'
               THEN job.progress_json ELSE '{}'::jsonb END)
         - 'dispatch_last_error' - 'dispatch_available_at')
        || jsonb_build_object(
            'dispatch_status', CASE
                WHEN reconciled.status = 'completed' THEN 'published'
                ELSE 'discarded'
            END,
            'dispatch_attempts', CAST(reconciled.attempts AS integer)
        )
    FROM reconciled
    WHERE job.id = reconciled.task_id
    """
)

# 数据库租约负责多API实例协调；API进程崩溃后，过期租约会允许另一实例继续派发。
_CLAIM_DISPATCHES = text(
    """
    WITH eligible AS (
        SELECT outbox.task_id
        FROM agric_satellite.satellite_batch_dispatch_outbox AS outbox
        JOIN agric_satellite.jobs AS job ON job.id = outbox.task_id
        WHERE job.status = 'pending'
          AND (
              (outbox.status = 'pending' AND outbox.available_at <= now())
              OR (outbox.status = 'processing' AND outbox.lease_until <= now())
          )
        ORDER BY outbox.available_at, outbox.created_at
        FOR UPDATE OF outbox SKIP LOCKED
        LIMIT :limit
    )
    UPDATE agric_satellite.satellite_batch_dispatch_outbox AS outbox
    SET status = 'processing',
        attempts = outbox.attempts + 1,
        lease_owner = :worker_id,
        lease_until = now() + make_interval(secs => :lease_seconds),
        updated_at = now()
    FROM eligible
    WHERE outbox.task_id = eligible.task_id
    RETURNING outbox.task_id, outbox.land_id, outbox.extras_json,
              outbox.priority, outbox.attempts
    """
)

# 只有仍持有租约的实例可确认成功，防止过期实例覆盖后来者的派发状态。
_COMPLETE_DISPATCH = text(
    """
    UPDATE agric_satellite.satellite_batch_dispatch_outbox
    SET status = 'completed',
        lease_owner = NULL,
        lease_until = NULL,
        last_error = NULL,
        completed_at = now(),
        updated_at = now()
    WHERE task_id = :task_id
      AND status = 'processing'
      AND lease_owner = :worker_id
    RETURNING task_id
    """
)

# MQ暂时不可用时按指数间隔重试，避免一次故障把所有遥感Job永久标为失败。
_RETRY_DISPATCH = text(
    """
    UPDATE agric_satellite.satellite_batch_dispatch_outbox
    SET status = 'pending',
        available_at = now() + make_interval(
            secs => LEAST(3600, 5 * power(2, LEAST(attempts - 1, 10))::integer)
        ),
        lease_owner = NULL,
        lease_until = NULL,
        last_error = :error,
        updated_at = now()
    WHERE task_id = :task_id
      AND status = 'processing'
      AND lease_owner = :worker_id
    RETURNING task_id, attempts, available_at
    """
)

# Job进度仅保存派发阶段和次数；连接串等底层异常留在受控的Outbox字段与日志中。
_UPDATE_JOB_DISPATCH_RETRY = text(
    """
    UPDATE agric_satellite.jobs
    SET progress_json =
        ((CASE WHEN jsonb_typeof(progress_json) = 'object'
               THEN progress_json ELSE '{}'::jsonb END)
         - 'dispatch_last_error' - 'dispatch_available_at')
        || jsonb_build_object(
            'dispatch_status', 'retrying',
            'dispatch_attempts', CAST(:attempts AS integer),
            'dispatch_available_at', CAST(:available_at AS text)
        )
    WHERE id = :task_id
    """
)
_UPDATE_JOB_DISPATCH_PUBLISHED = text(
    """
    UPDATE agric_satellite.jobs
    SET progress_json =
        ((CASE WHEN jsonb_typeof(progress_json) = 'object'
               THEN progress_json ELSE '{}'::jsonb END)
         - 'dispatch_last_error' - 'dispatch_available_at')
        || jsonb_build_object(
            'dispatch_status', 'published',
            'dispatch_attempts', CAST(:attempts AS integer)
        )
    WHERE id = :task_id
    """
)


def _safe_error(error: Exception) -> str:
    """保存不含连接凭据且长度受控的错误摘要供管理员排障。"""
    message = re.sub(
        r"((?:amqps?|postgresql(?:\+\w+)?|redis(?:s)?):\/\/)[^@\s]+@",
        r"\1<redacted>@",
        str(error),
        flags=re.IGNORECASE,
    )
    # 错误文本可能来自驱动/客户端并包含JSON或查询参数，需一并遮蔽常见密钥字段与Bearer令牌。
    message = _SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", message)
    message = _BEARER_TOKEN_RE.sub(r"\1<redacted>", message)
    return f"{type(error).__name__}: {message}"[:1000]


async def _dispatch_batch() -> int:
    from app.core.database import async_session

    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4()}"
    async with async_session() as db:
        # 不打断仍持有有效租约的发布线程；只协调待重试或租约过期的意图。
        await db.execute(_RECONCILE_STARTED_OR_TERMINAL_JOBS)
        claimed = list(
            (
                await db.execute(
                    _CLAIM_DISPATCHES,
                    {
                        "worker_id": worker_id,
                        "limit": OUTBOX_CLAIM_LIMIT,
                        "lease_seconds": OUTBOX_LEASE_SECONDS,
                    },
                )
            )
            .mappings()
            .all()
        )
        await db.commit()

    if not claimed:
        return 0

    from app.mq_publish import publish_api_task

    for row in claimed:
        task_id = str(row["task_id"])
        try:
            # MQ发布为同步I/O，放入线程池等待broker确认，避免阻塞API事件循环。
            await asyncio.to_thread(
                publish_api_task,
                type="satellite_batch",
                land_id=str(row["land_id"]),
                task_id=task_id,
                extras=dict(row["extras_json"] or {}),
                priority=int(row["priority"]),
            )
        except Exception as exc:
            safe_error = _safe_error(exc)
            async with async_session() as db:
                retried = (
                    await db.execute(
                        _RETRY_DISPATCH,
                        {
                            "task_id": row["task_id"],
                            "worker_id": worker_id,
                            "error": safe_error,
                        },
                    )
                ).mappings().first()
                if retried:
                    await db.execute(
                        _UPDATE_JOB_DISPATCH_RETRY,
                        {
                            "task_id": row["task_id"],
                            "attempts": retried["attempts"],
                            "available_at": retried["available_at"].isoformat(),
                        },
                    )
                await db.commit()
            if retried:
                logger.error(
                    "satellite_batch_dispatch_retry_scheduled",
                    task_id=task_id,
                    attempts=retried["attempts"],
                    available_at=str(retried["available_at"]),
                    error=safe_error,
                )
            else:
                logger.warning(
                    "satellite_batch_dispatch_retry_lease_lost",
                    task_id=task_id,
                    error=safe_error,
                )
            continue

        async with async_session() as db:
            completed = (
                await db.execute(
                    _COMPLETE_DISPATCH,
                    {"task_id": row["task_id"], "worker_id": worker_id},
                )
            ).scalar_one_or_none()
            if completed is not None:
                await db.execute(
                    _UPDATE_JOB_DISPATCH_PUBLISHED,
                    {"task_id": row["task_id"], "attempts": row["attempts"]},
                )
            await db.commit()
        if completed is None:
            # broker已确认但租约已失效时允许新实例重发，语义为至少一次而非exactly-once。
            logger.warning(
                "satellite_batch_dispatch_confirm_after_lease_loss", task_id=task_id
            )
        else:
            logger.info(
                "satellite_batch_dispatch_completed",
                task_id=task_id,
                attempts=row["attempts"],
            )
    return len(claimed)


async def run_satellite_batch_dispatch_outbox() -> None:
    """周期恢复legacy/dual模式中尚未得到MQ确认的通用遥感批任务。"""
    scan_retry_seconds = OUTBOX_POLL_SECONDS
    while True:
        try:
            claimed_count = await _dispatch_batch()
            scan_retry_seconds = OUTBOX_POLL_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "satellite_batch_dispatch_outbox_scan_failed",
                error=_safe_error(exc),
                retry_in_seconds=scan_retry_seconds,
            )
            await asyncio.sleep(scan_retry_seconds)
            scan_retry_seconds = min(60, scan_retry_seconds * 2)
            continue

        # 满批时立即继续排空积压；空闲或部分批次等待下一轮，限制空查询频率。
        if claimed_count < OUTBOX_CLAIM_LIMIT:
            await asyncio.sleep(OUTBOX_POLL_SECONDS)


__all__ = ["run_satellite_batch_dispatch_outbox"]
