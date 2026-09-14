"""HTTP-first job / work-item writes for download-host isolation (D3).

When ``http_writes_enabled()`` (see openfarm_common.internal_api), prefer
PATCH /v1/internal/jobs and work complete/fail over SyncSession.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.core.logging import logger

__all__ = [
    "complete_work_item_http",
    "fail_work_item_http",
    "update_job_record",
]


def update_job_record(
    session,
    job,
    status: str,
    *,
    progress: dict | None = None,
    error: str | None = None,
    job_id: str | None = None,
) -> None:
    """Update a Job via internal HTTP when enabled, else SyncSession.

    ``job`` may be None when the row lives only on the API DB; in that case
    ``job_id`` is required for the HTTP path.
    """
    from openfarm_common.internal_api import http_writes_enabled, patch_job

    jid = str(job_id or (job.id if job is not None else "") or "")
    if http_writes_enabled() and jid:
        body: dict[str, Any] = {"status": status}
        if progress is not None:
            body["progress_json"] = progress
        if error is not None:
            body["error"] = error
        if status == "running":
            body["touch_started"] = True
        if status in ("succeeded", "failed", "cancelled", "completed"):
            body["touch_finished"] = True
        try:
            patch_job(jid, body)
            return
        except Exception as exc:
            logger.warning(
                "job_http_patch_failed",
                job_id=jid,
                error=str(exc),
                fallback_session=bool(job is not None and session is not None),
            )
            # Fall through to SyncSession when a local job row exists
            if job is None or session is None:
                raise

    if job is None:
        return
    job.status = status
    if progress is not None:
        job.progress_json = progress
        flag_modified(job, "progress_json")
    if error is not None:
        job.error = error
    if status == "running" and job.started_at is None:
        job.started_at = datetime.now(timezone.utc)
    if status in ("succeeded", "failed"):
        job.finished_at = datetime.now(timezone.utc)
    session.add(job)
    session.commit()


def complete_work_item_http(
    work_item_id: str | None,
    result: dict[str, Any],
    *,
    worker_id: str | None = None,
) -> None:
    if not work_item_id:
        return
    try:
        from openfarm_common.internal_api import complete_work, http_writes_enabled, internal_api_enabled

        if not (http_writes_enabled() or internal_api_enabled()):
            return
        complete_work(str(work_item_id), result, worker_id=worker_id)
    except Exception as exc:
        logger.warning(
            "work_item_complete_http_failed",
            work_item_id=work_item_id,
            error=str(exc),
        )


def fail_work_item_http(
    work_item_id: str | None,
    error: str,
    *,
    worker_id: str | None = None,
    retry: bool = False,
) -> None:
    if not work_item_id:
        return
    try:
        from openfarm_common.internal_api import fail_work, http_writes_enabled, internal_api_enabled

        if not (http_writes_enabled() or internal_api_enabled()):
            return
        fail_work(str(work_item_id), error, worker_id=worker_id, retry=retry)
    except Exception as exc:
        logger.warning(
            "work_item_fail_http_failed",
            work_item_id=work_item_id,
            error=str(exc),
        )
