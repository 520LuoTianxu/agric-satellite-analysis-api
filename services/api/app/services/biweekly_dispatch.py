"""Bounded satellite_batch dispatch for biweekly backfill.

This is the API-machine linkage: create_satellite_batch_jobs + the existing
WorkItem / outbox path already selected by work_queue_mode. It does not
invent a new job type, does not default to a three-year window, and does
not call generate.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

from agric_satellite_analysis_common.biweekly.calendar import MAX_BACKFILL_DAYS, parse_day
from agric_satellite_analysis_common.biweekly.models import ReportError

from app.core.config import settings

DispatchFn = Callable[[str, dict], Awaitable[dict[str, Any]]]


def _validate_window(window: dict) -> tuple[date, date]:
    date_from = parse_day(window["date_from"])
    date_to = parse_day(window["date_to_inclusive"])
    span = int(window.get("span_days") or (date_to - date_from).days + 1)
    if span <= 0 or span > MAX_BACKFILL_DAYS or span >= 365:
        raise ReportError("BACKFILL_WINDOW_UNBOUNDED")
    if window.get("years") or window.get("months"):
        raise ReportError("BACKFILL_WINDOW_UNBOUNDED")
    if date_from > date_to:
        raise ReportError("BACKFILL_WINDOW_UNBOUNDED")
    return date_from, date_to


async def dispatch_bounded_backfill(land_id: str, window: dict) -> dict[str, Any]:
    """Create bounded satellite_batch jobs and stage them on this API machine.

    Returns dispatched=true only after jobs were created and staged (WorkItem
    and/or outbox, according to work_queue_mode). Does not call generate.
    """
    date_from, date_to = _validate_window(window)
    from app.core.database import async_session
    from app.services.satellite_batch import create_satellite_batch_jobs
    from app.services.smart_land_backfill import LandSelectionError, ensure_land_parcels
    from app.services.work_items import should_enqueue_work_items, should_publish_mq

    try:
        async with async_session() as db:
            try:
                lands, _selection = await ensure_land_parcels(
                    db, [str(land_id)], max_lands=1, allow_partial=False
                )
            except LandSelectionError as exc:
                return {
                    "dispatched": False,
                    "dispatched_to_production": False,
                    "reason": "LAND_NOT_AVAILABLE",
                    "detail": str(exc),
                    "job_ids": [],
                    "job_count": 0,
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "sensors": ["S2", "S1"],
                    "work_queue_mode": settings.work_queue_mode,
                }
            groups, jobs, _meta = await create_satellite_batch_jobs(
                db,
                lands,
                date_from=date_from,
                date_to=date_to,
                sensors=["S2", "S1"],
                force=False,
                durable_dispatch=True,
            )
            await db.commit()
            job_ids = [str(job.id) for job in jobs]
            dispatched = bool(job_ids)
            return {
                "dispatched": dispatched,
                "dispatched_to_production": dispatched,
                "reason": None if dispatched else "NO_JOBS_CREATED",
                "job_ids": job_ids,
                "job_count": len(job_ids),
                "group_count": len(groups),
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "sensors": ["S2", "S1"],
                "land_id": str(land_id),
                "work_queue_mode": settings.work_queue_mode,
                "staged_work_items": should_enqueue_work_items(),
                "staged_mq_outbox": should_publish_mq(),
                "note": (
                    "已通过 API 机 create_satellite_batch_jobs 写入本机 Job，并按 work_queue_mode "
                    "进入 WorkItem 和/或卫星批次出站箱。下载机领取已有 satellite_batch 任务即可。"
                    "补数完成不会自动出报。"
                ),
            }
    except Exception as exc:  # noqa: BLE001 — surface a dispatch gap, do not generate
        return {
            "dispatched": False,
            "dispatched_to_production": False,
            "reason": "DISPATCH_FAILED",
            "detail": type(exc).__name__,
            "job_ids": [],
            "job_count": 0,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "sensors": ["S2", "S1"],
            "work_queue_mode": settings.work_queue_mode,
        }
