"""Bounded satellite_batch dispatch for biweekly backfill.

This is the API-machine linkage: create_satellite_batch_jobs + the existing
WorkItem / outbox path already selected by work_queue_mode. It does not
invent a new download pipeline: WorkItem/outbox claim type stays
satellite_batch so the download machine can pick the jobs up. Job.type and
the user-visible name are distinct biweekly backfill labels. It does not
default to a three-year window, and does not call generate.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

from agric_satellite_analysis_common.biweekly.calendar import (
    MAX_BACKFILL_DAYS,
    bounded_backfill_window,
    parse_day,
)
from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.scheduled_land_filter import (
    BYPASS_SCHEDULE_LAND_FILTER_PARAM,
)

from app.core.config import settings

DispatchFn = Callable[[str, dict], Awaitable[dict[str, Any]]]

BIWEEKLY_PERIOD_JOB_TYPE = "biweekly_period_backfill"
BIWEEKLY_SEASON_JOB_TYPE = "biweekly_season_backfill"
BIWEEKLY_PERIOD_DISPLAY_NAME = "双周报本期数据补齐"
BIWEEKLY_SEASON_DISPLAY_NAME = "双周报全季数据补齐"



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


async def dispatch_bounded_backfill(
    land_id: str,
    window: dict,
    *,
    job_type: str = BIWEEKLY_PERIOD_JOB_TYPE,
    display_name: str = BIWEEKLY_PERIOD_DISPLAY_NAME,
) -> dict[str, Any]:
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
                job_type=job_type,
                extra_params={
                    BYPASS_SCHEDULE_LAND_FILTER_PARAM: True,
                    "biweekly_user_backfill": True,
                    "display_type": job_type,
                    "display_name": display_name,
                },
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
                "job_type": job_type,
                "display_name": display_name,
                "claim_type": "satellite_batch",
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


async def dispatch_season_bounded_backfill(land_id: str, periods: list[dict]) -> dict[str, Any]:
    """Dispatch one bounded window per biweekly period in the season.

    Never collapses the season into a single window. Each window is the
    current period plus its prior comparison window and must honor
    MAX_BACKFILL_DAYS. Does not call generate.
    """
    if not periods:
        raise ReportError("SEASON_NOT_FOUND")
    period_results: list[dict[str, Any]] = []
    job_ids: list[str] = []
    for row in periods:
        window = bounded_backfill_window(periods, row["period_id"])
        dispatch = await dispatch_bounded_backfill(
            land_id,
            window,
            job_type=BIWEEKLY_SEASON_JOB_TYPE,
            display_name=BIWEEKLY_SEASON_DISPLAY_NAME,
        )
        entry = {
            "period_id": str(row["period_id"]),
            "window": {
                "date_from": window["date_from"],
                "date_to_inclusive": window["date_to_inclusive"],
                "span_days": window["span_days"],
                "years": window.get("years"),
                "months": window.get("months"),
            },
            "dispatched": bool(dispatch.get("dispatched")),
            "reason": dispatch.get("reason"),
            "job_ids": list(dispatch.get("job_ids") or []),
            "job_count": int(dispatch.get("job_count") or 0),
        }
        period_results.append(entry)
        job_ids.extend(entry["job_ids"])
    dispatched = any(item["dispatched"] for item in period_results)
    failed = [item for item in period_results if not item["dispatched"]]
    return {
        "dispatched": dispatched,
        "dispatched_to_production": dispatched,
        "reason": None if dispatched else (failed[0]["reason"] if failed else "DISPATCH_FAILED"),
        "job_ids": job_ids,
        "job_count": len(job_ids),
        "period_count": len(periods),
        "period_results": period_results,
        "failed_period_count": len(failed),
        "scope": "season",
        "land_id": str(land_id),
        "sensors": ["S2", "S1"],
        "job_type": BIWEEKLY_SEASON_JOB_TYPE,
        "display_name": BIWEEKLY_SEASON_DISPLAY_NAME,
        "claim_type": "satellite_batch",
        "note": "已按双周报周期分别派发有界 satellite_batch 任务。不会自动出报。",
    }

