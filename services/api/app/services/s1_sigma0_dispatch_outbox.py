"""恢复 S1 Sigma0 回算的持久化 MQ 派发意图。"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import uuid

from sqlalchemy import text

from app.core.logging import logger

OUTBOX_POLL_SECONDS = 5
OUTBOX_CLAIM_LIMIT = 25
OUTBOX_LEASE_SECONDS = 900

_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?i)(['"]?(?:password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret[_-]?key|secret|api[_-]?key|access[_-]?key)['"]?\s*[:=]\s*)(?:'[^']*'|"[^"]*"|[^,\s;&}]+)"""
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*")

# 若Worker已开始则视为派发成功；若子Job已终结则丢弃意图，避免重复或无效投递。
_RECONCILE_STARTED_OR_TERMINAL_JOBS = text(
    """
    WITH reconciled AS (
        UPDATE agric_satellite.s1_sigma0_dispatch_outbox AS outbox
        SET status = CASE WHEN job.status = 'running' THEN 'completed' ELSE 'discarded' END,
            lease_owner = NULL,
            lease_until = NULL,
            last_error = NULL,
            completed_at = now(),
            updated_at = now()
        FROM agric_satellite.jobs AS job
        WHERE outbox.job_id = job.id
          AND outbox.status IN ('pending', 'processing')
          AND (outbox.status = 'pending' OR outbox.lease_until <= now())
          AND job.status IN (
              'running', 'completed', 'succeeded', 'done', 'success', 'failed',
              'cancelled', 'partial'
          )
        RETURNING outbox.job_id, outbox.attempts, outbox.status
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
    WHERE job.id = reconciled.job_id
    """
)

# 同一语句用行锁和租约领取；多个API实例可并发扫描，崩溃后过期租约可重新派发。
_CLAIM_DISPATCHES = text(
    """
    WITH eligible AS (
        SELECT outbox.job_id
        FROM agric_satellite.s1_sigma0_dispatch_outbox AS outbox
        JOIN agric_satellite.jobs AS job ON job.id = outbox.job_id
        WHERE job.status = 'pending'
          AND (
              (outbox.status = 'pending' AND outbox.available_at <= now())
              OR (outbox.status = 'processing' AND outbox.lease_until <= now())
          )
        ORDER BY outbox.available_at, outbox.created_at
        FOR UPDATE OF outbox SKIP LOCKED
        LIMIT :limit
    )
    UPDATE agric_satellite.s1_sigma0_dispatch_outbox AS outbox
    SET status = 'processing',
        attempts = outbox.attempts + 1,
        lease_owner = :worker_id,
        lease_until = now() + make_interval(secs => :lease_seconds),
        updated_at = now()
    FROM eligible
    WHERE outbox.job_id = eligible.job_id
    RETURNING outbox.job_id, outbox.land_id, outbox.attempts
    """
)

# 只有持有当前租约的dispatcher能封口，避免旧实例覆盖重新领取后的状态。
_COMPLETE_DISPATCH = text(
    """
    UPDATE agric_satellite.s1_sigma0_dispatch_outbox
    SET status = 'completed',
        lease_owner = NULL,
        lease_until = NULL,
        last_error = NULL,
        completed_at = now(),
        updated_at = now()
    WHERE job_id = :job_id
      AND status = 'processing'
      AND lease_owner = :worker_id
    RETURNING job_id
    """
)

# 派发异常按5秒起步、指数增长且最多1小时重试；临时故障不会把遥感Job永久失败。
_RETRY_DISPATCH = text(
    """
    UPDATE agric_satellite.s1_sigma0_dispatch_outbox
    SET status = 'pending',
        available_at = now() + make_interval(
            secs => LEAST(3600, 5 * power(2, LEAST(attempts - 1, 10))::integer)
        ),
        lease_owner = NULL,
        lease_until = NULL,
        last_error = :error,
        updated_at = now()
    WHERE job_id = :job_id
      AND status = 'processing'
      AND lease_owner = :worker_id
    RETURNING job_id, attempts, available_at
    """
)

# 同事务回写Job轻量进度，让现有运维任务列表/详情能看到派发退避状态。
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
    WHERE id = :job_id
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
    WHERE id = :job_id
    """
)


def _safe_error(error: Exception) -> str:
    """保存有限且不含连接凭据的错误摘要，便于运维排查派发重试。"""
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
        job_id = str(row["job_id"])
        try:
            # MQ客户端是同步接口；线程池等待确认，避免阻塞FastAPI事件循环。
            await asyncio.to_thread(
                publish_api_task,
                type="satellite_batch",
                land_id=str(row["land_id"]),
                task_id=job_id,
                extras={"job_id": job_id},
            )
        except Exception as exc:
            safe_error = _safe_error(exc)
            async with async_session() as db:
                retried = (
                    await db.execute(
                        _RETRY_DISPATCH,
                        {
                            "job_id": row["job_id"],
                            "worker_id": worker_id,
                            "error": safe_error,
                        },
                    )
                ).mappings().first()
                if retried:
                    # 任务仍保持pending；详情中明确提示它在等待MQ恢复，而非遥感计算失败。
                    await db.execute(
                        _UPDATE_JOB_DISPATCH_RETRY,
                        {
                            "job_id": row["job_id"],
                            "attempts": retried["attempts"],
                            "available_at": retried["available_at"].isoformat(),
                        },
                    )
                await db.commit()
            if retried:
                logger.error(
                    "s1_sigma0_dispatch_retry_scheduled",
                    job_id=job_id,
                    attempts=retried["attempts"],
                    available_at=str(retried["available_at"]),
                    error=safe_error,
                )
            else:
                logger.warning(
                    "s1_sigma0_dispatch_retry_lease_lost",
                    job_id=job_id,
                    error=safe_error,
                )
            continue

        async with async_session() as db:
            completed = (
                await db.execute(
                    _COMPLETE_DISPATCH,
                    {"job_id": row["job_id"], "worker_id": worker_id},
                )
            ).scalar_one_or_none()
            if completed is not None:
                await db.execute(
                    _UPDATE_JOB_DISPATCH_PUBLISHED,
                    {"job_id": row["job_id"], "attempts": row["attempts"]},
                )
            await db.commit()
        if completed is None:
            # MQ已确认但租约已被回收时，允许新dispatcher重试，语义为至少一次。
            logger.warning("s1_sigma0_dispatch_confirm_after_lease_loss", job_id=job_id)
        else:
            logger.info(
                "s1_sigma0_dispatch_completed",
                job_id=job_id,
                attempts=row["attempts"],
            )
    return len(claimed)


async def run_s1_sigma0_dispatch_outbox() -> None:
    """周期恢复legacy/dual模式中尚未得到MQ确认的S1派发意图。"""
    scan_retry_seconds = OUTBOX_POLL_SECONDS
    while True:
        try:
            claimed_count = await _dispatch_batch()
            scan_retry_seconds = OUTBOX_POLL_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "s1_sigma0_dispatch_outbox_scan_failed",
                error=_safe_error(exc),
                retry_in_seconds=scan_retry_seconds,
            )
            await asyncio.sleep(scan_retry_seconds)
            scan_retry_seconds = min(60, scan_retry_seconds * 2)
            continue

        # 满批时继续排空，空闲/部分批次则等待下一次轮询。
        if claimed_count < OUTBOX_CLAIM_LIMIT:
            await asyncio.sleep(OUTBOX_POLL_SECONDS)
