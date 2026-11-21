"""Backfill-then-report flow for official biweekly PDFs.

Backfill failures stay in the backfill stage. Generate/PDF failures stay in
the generate stage. A failed scene read is SCENE_QUERY_FAILED, never
disguised as "no optics".
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from agric_satellite_analysis_common.biweekly.calendar import bounded_backfill_window, prior_period
from agric_satellite_analysis_common.biweekly.models import ReportError

from app.core.logging import logger
from app.services.biweekly_dispatch import (
    BIWEEKLY_PERIOD_DISPLAY_NAME,
    BIWEEKLY_PERIOD_JOB_TYPE,
    BIWEEKLY_SEASON_DISPLAY_NAME,
    BIWEEKLY_SEASON_JOB_TYPE,
    dispatch_bounded_backfill,
    dispatch_biweekly_report,
)
from app.services.biweekly_scenes import (
    load_land_meta,
    load_scene_observations,
    load_weather_series,
    summarize_weather,
)
from app.services.biweekly_trigger import build_trigger_service

TERMINAL_SUCCESS = {"completed", "succeeded"}
TERMINAL_FAILURE = {"failed", "cancelled"}
TERMINAL = TERMINAL_SUCCESS | TERMINAL_FAILURE


def _pdf_renderer():
    def _render(facts, dest):
        from app.reports.biweekly.official_pdf import render_official_biweekly_pdf

        render_official_biweekly_pdf(facts, dest)

    return _render


def trigger_service(smart=None):
    # API no longer renders official PDFs; the download machine does.
    return build_trigger_service(smart, use_official_pdf=False)


def _eligible(rows: list[dict] | None) -> list[dict]:
    if not rows:
        return []
    return [row for row in rows if row.get("quality_status") == "eligible_descriptive"]


def _s1_count(rows: list[dict] | None) -> int:
    if not rows:
        return 0
    return sum(1 for row in rows if str(row.get("sensor") or "").upper() == "S1")


async def _last_optical_observations(land_id: str, periods: list[dict], period_id: str) -> list[dict] | None:
    """Walk back to the last period that has eligible optics. None = query failed."""
    ids = [str(row["period_id"]) for row in periods]
    try:
        index = ids.index(str(period_id))
    except ValueError:
        return []
    collected: list[dict] | None = []
    for prior in reversed(periods[:index]):
        rows = await load_scene_observations(land_id, prior["start"], prior["end_exclusive"])
        if rows is None:
            return None if collected == [] and prior is periods[index - 1] else (collected or [])
        optical = _eligible(rows)
        if optical:
            return rows
    return collected


async def _enrich_payload(land_id: str, period: dict, periods: list[dict]) -> dict[str, Any]:
    observations = await load_scene_observations(land_id, period["start"], period["end_exclusive"])
    if observations is None:
        if period.get("snapshot"):
            # Local pack periods can still generate from the fixture snapshot.
            return {
                "observation_source": "pack_snapshot",
            }
        raise ReportError("SCENE_QUERY_FAILED")
    prior_obs = await _last_optical_observations(land_id, periods, period["period_id"])
    if prior_obs is None:
        prior_obs = []
    meta = await load_land_meta(land_id)
    start = period["start"]
    end_inclusive = period.get("end_inclusive") or period["end_exclusive"]
    try:
        antecedent_end = (date.fromisoformat(str(start)[:10]) - timedelta(days=1)).isoformat()
        antecedent_start = (date.fromisoformat(str(start)[:10]) - timedelta(days=30)).isoformat()
    except ValueError:
        antecedent_start = antecedent_end = None
    weather_daily = await load_weather_series(land_id, start, end_inclusive)
    weather_period = summarize_weather(weather_daily) if weather_daily is not None else None
    weather_antecedent = None
    if antecedent_start and antecedent_end:
        antecedent_rows = await load_weather_series(land_id, antecedent_start, antecedent_end)
        weather_antecedent = summarize_weather(antecedent_rows) if antecedent_rows is not None else None
        if weather_daily and antecedent_rows:
            weather_daily = list(antecedent_rows) + list(weather_daily)
    return {
        "observations_override": observations,
        "prior_observations_override": prior_obs,
        "observation_source": "parcel_scene_products",
        "land_name": (meta or {}).get("land_name"),
        "weather_daily": weather_daily,
        "weather_period": weather_period,
        "weather_antecedent": weather_antecedent,
        "s1_count": _s1_count(observations),
    }


async def finish_generate_job(service, job_id: str) -> dict:
    """Load live scenes and render the official PDF or an evidence-limited brief."""
    job = service.get_job(job_id)
    if job.get("status") in {"ready", "evidence_limited"}:
        return job
    if job.get("error_code") == "BACKFILL_FAILED":
        return job
    land_id = job["land_id"]
    season_id = job["season_id"]
    period_id = job["period_id"]
    period = service.catalog.period(land_id, season_id, period_id)
    periods = service.catalog.periods(land_id, season_id)
    try:
        fields = await _enrich_payload(land_id, period, periods)
    except ReportError as exc:
        note = "读取已入库景失败，不能当作本期没有可用光学。"
        if exc.code == "SCENE_QUERY_FAILED":
            return service.fail_stage(job_id, "SCENE_QUERY_FAILED", note=note)
        return service.fail_stage(job_id, exc.code or "GENERATE_FAILED", note=str(exc.code))
    service.set_payload_fields(job_id, **fields)
    # run_job only proceeds from queued/running/dispatched
    current = service.get_job(job_id)
    if current["status"] == "failed":
        return current
    try:
        finished = service.run_job(job_id)
    except ReportError as exc:
        return service.fail_stage(job_id, exc.code or "GENERATE_FAILED", note="出报失败，补数阶段已经结束。")
    if finished.get("status") == "failed" and not finished.get("error_code"):
        return service.fail_stage(job_id, "GENERATE_FAILED", note="出报失败，补数阶段已经结束。")
    result = dict(finished.get("result") or {})
    result["stage"] = "done"
    if finished.get("status") == "ready":
        result["note"] = "补数阶段已结束，正式报告 PDF 已生成。"
    elif finished.get("status") == "evidence_limited":
        result["note"] = "补数阶段已结束。本期确实没有可用光学观测，因此只给出证据受限简报，不是正式 PDF。"
    service.merge_result(job_id, result, status=finished.get("status"))
    return service.get_job(job_id)


def backfill_is_hard_failure(dispatch: dict) -> bool:
    reason = dispatch.get("reason")
    job_ids = list(dispatch.get("job_ids") or [])
    if reason in {"DISPATCH_FAILED", "LAND_NOT_AVAILABLE", "BACKFILL_WINDOW_UNBOUNDED"}:
        return True
    if dispatch.get("dispatched") and job_ids:
        return False
    return reason not in {None, "NO_JOBS_CREATED"}



async def _dispatch_official_report(service, job: dict) -> dict:
    """Hand official PDF generation to the download machine. Never render locally."""
    existing = ((job.get("result") or {}).get("report") or {})
    if existing.get("dispatched") and existing.get("job_ids"):
        return service.merge_result(job["job_id"], {"stage": "report"}, status="dispatched")
    period = service.catalog.period(job["land_id"], job["season_id"], job["period_id"])
    payload = job.get("payload") or {}
    backfill = (job.get("result") or {}).get("backfill") or {}
    report = await dispatch_biweekly_report(
        job["land_id"],
        {
            "date_from": period["start"],
            "date_to_inclusive": period.get("end_inclusive") or period["end_exclusive"],
        },
        extra_params={
            "biweekly_job_id": job["job_id"],
            "biweekly_land_id": str(job["land_id"]),
            "biweekly_season_id": job["season_id"],
            "biweekly_period_id": str(job["period_id"]),
            "reference_planting_instance_id": payload.get("reference_planting_instance_id"),
            "parent_season_job_id": payload.get("parent_season_job_id"),
            "wait_for_job_ids": list(backfill.get("job_ids") or []),
        },
    )
    if not report.get("dispatched"):
        reason = report.get("reason") or "DISPATCH_FAILED"
        code = "LAND_NOT_AVAILABLE" if reason == "LAND_NOT_AVAILABLE" else "REPORT_DISPATCH_FAILED"
        return service.fail_stage(
            job["job_id"],
            code,
            note="出报任务下发失败。失败发生在把出报交给下载机时，不是因为没有可用光学，也不是本机出报渲染失败。",
        )
    return service.merge_result(
        job["job_id"],
        {
            "stage": "report",
            "report": report,
            "note": "出报任务已下发给下载机。API 机不渲染 PDF。",
        },
        status="dispatched",
    )


async def start_period_generate(
    service,
    *,
    land_id: str,
    season_id: str,
    period_id: str,
    reference_planting_instance_id: str | None = None,
    native_plan: dict | None = None,
    parent_season_job_id: str | None = None,
    season_job_type: bool = False,
) -> dict:
    """Dispatch bounded backfill first, then generate if backfill need not wait."""
    job = service.request_generate(
        land_id,
        season_id,
        period_id,
        reference_planting_instance_id=reference_planting_instance_id,
        native_plan=native_plan,
        parent_season_job_id=parent_season_job_id,
        defer_run=True,
    )
    periods = service.catalog.periods(land_id, season_id)
    window = bounded_backfill_window(periods, period_id)
    dispatch = await dispatch_bounded_backfill(
        land_id,
        window,
        job_type=BIWEEKLY_SEASON_JOB_TYPE if season_job_type else BIWEEKLY_PERIOD_JOB_TYPE,
        display_name=BIWEEKLY_SEASON_DISPLAY_NAME if season_job_type else BIWEEKLY_PERIOD_DISPLAY_NAME,
        extra_params={
            "biweekly_job_id": job["job_id"],
            "biweekly_land_id": str(land_id),
            "biweekly_season_id": season_id,
            "biweekly_period_id": str(period_id),
            "biweekly_generate_after": True,
            "parent_season_job_id": parent_season_job_id,
        },
    )
    job = service.record_generate_backfill(job["job_id"], dispatch)
    if job["status"] == "failed":
        return job
    if (job.get("result") or {}).get("backfill", {}).get("status") == "dispatched":
        return service.merge_result(
            job["job_id"],
            {
                "note": "本期补数已派发到下载机；出报将在补数完成后由下载机进行。API 机不渲染 PDF。",
            },
            status="dispatched",
        )
    return await _dispatch_official_report(service, job)


async def start_season_generate(
    service,
    *,
    land_id: str,
    season_id: str,
    reference_planting_instance_id: str | None = None,
) -> dict:
    """Dispatch each period's bounded backfill and generate a report per period."""
    periods = service.catalog.periods(land_id, season_id)
    if not periods:
        raise ReportError("SEASON_NOT_FOUND")
    native = None
    if hasattr(service.catalog, "native_plan"):
        native = service.catalog.native_plan(land_id, season_id)
    season_job = service.request_season_backfill(land_id, season_id)
    child_ids: list[str] = []
    period_results: list[dict[str, Any]] = []
    waiting = False
    all_backfill_failed = True
    for row in periods:
        child = await start_period_generate(
            service,
            land_id=land_id,
            season_id=season_id,
            period_id=row["period_id"],
            reference_planting_instance_id=reference_planting_instance_id,
            native_plan=native,
            parent_season_job_id=season_job["job_id"],
            season_job_type=True,
        )
        child_ids.append(child["job_id"])
        period_results.append(
            {
                "period_id": str(row["period_id"]),
                "generate_job_id": child["job_id"],
                "status": child.get("status"),
                "error_code": child.get("error_code"),
                "artifact_kind": (child.get("artifact") or {}).get("kind"),
                "backfill": (child.get("result") or {}).get("backfill"),
            }
        )
        if child.get("status") != "failed" or child.get("error_code") != "BACKFILL_FAILED":
            all_backfill_failed = False
        if child.get("status") == "dispatched":
            waiting = True
    result = {
        "stage": "backfill" if waiting else "done",
        "scope": "season",
        "period_results": period_results,
        "generate_job_ids": child_ids,
        "dispatched_to_production": any(
            (item.get("backfill") or {}).get("status") == "dispatched" for item in period_results
        ),
        "note": (
            "全季已按期派发有界补数；各期出报将由下载机在补数完成后进行。API 机不渲染 PDF。"
            if waiting
            else "全季各期任务已下发给下载机（补数与出报）。API 机不渲染 PDF。"
        ),
    }
    if all_backfill_failed and period_results:
        result["note"] = "全季各期补数均失败，尚未出报。失败发生在补数阶段。"
        service.record_backfill_dispatch(season_job["job_id"], {"dispatched": False, "reason": "BACKFILL_FAILED", "job_ids": []})
        return service.merge_result(season_job["job_id"], result, status="failed")
    if waiting:
        # Keep the season job in dispatched while children wait.
        service.record_backfill_dispatch(
            season_job["job_id"],
            {
                "dispatched": True,
                "reason": None,
                "job_ids": [
                    job_id
                    for item in period_results
                    for job_id in ((item.get("backfill") or {}).get("job_ids") or [])
                ],
            },
        )
        return service.merge_result(season_job["job_id"], result, status="dispatched")
    service.record_backfill_dispatch(
        season_job["job_id"],
        {"dispatched": True, "reason": "NO_JOBS_CREATED", "job_ids": []},
    )
    # record_backfill_dispatch may mark failed when no jobs; overwrite to ready.
    return service.merge_result(season_job["job_id"], result, status="ready")


async def maybe_finalize_from_satellite_job(params: dict[str, Any] | None, status: str | None) -> None:
    """Called after a satellite_batch job reaches a terminal status."""
    if str(status or "") not in TERMINAL:
        return
    params = params or {}
    biweekly_job_id = params.get("biweekly_job_id")
    if not biweekly_job_id or not params.get("biweekly_generate_after"):
        return
    try:
        await maybe_finalize_biweekly_job(str(biweekly_job_id))
    except Exception as exc:  # noqa: BLE001 — never fail the job patch path
        logger.warning(
            "biweekly_finalize_hook_failed job_id=%s err_type=%s",
            biweekly_job_id,
            type(exc).__name__,
        )


async def maybe_finalize_biweekly_job(job_id: str, *, service=None) -> dict | None:
    service = service or trigger_service()
    try:
        job = service.get_job(job_id)
    except ReportError:
        return None
    if job.get("kind") != "generate":
        if job.get("kind") == "backfill_season":
            return await _roll_up_season(service, job)
        return job
    if job.get("status") in {"ready", "evidence_limited"}:
        return job
    if job.get("error_code") == "BACKFILL_FAILED":
        return job
    job_ids = list(((job.get("result") or {}).get("backfill") or {}).get("job_ids") or job.get("satellite_batch_job_ids") or [])
    if job_ids:
        outcomes = await _job_statuses(job_ids)
        if outcomes is None:
            return job
        if any(status not in TERMINAL for status in outcomes.values()):
            return job
        successes = [job_id for job_id, status in outcomes.items() if status in TERMINAL_SUCCESS]
        if not successes:
            return service.fail_stage(
                job["job_id"],
                "BACKFILL_FAILED",
                note="补数任务全部失败或取消，尚未出报。失败发生在补数阶段，不是因为没有可用光学。",
            )
    finished = await _dispatch_official_report(service, job)
    parent_id = (finished.get("payload") or {}).get("parent_season_job_id")
    if parent_id:
        await _roll_up_season(service, service.get_job(parent_id))
    return finished


async def _roll_up_season(service, season_job: dict) -> dict:
    result = dict(season_job.get("result") or {})
    children = list(result.get("generate_job_ids") or [])
    if not children:
        return season_job
    period_results = []
    waiting = False
    statuses = []
    for child_id in children:
        try:
            child = service.get_job(child_id)
        except ReportError:
            waiting = True
            continue
        statuses.append(child.get("status"))
        if child.get("status") == "dispatched":
            waiting = True
        period_results.append(
            {
                "period_id": child.get("period_id"),
                "generate_job_id": child_id,
                "status": child.get("status"),
                "error_code": child.get("error_code"),
                "artifact_kind": (child.get("artifact") or {}).get("kind"),
            }
        )
    result["period_results"] = period_results
    if waiting:
        result["stage"] = "backfill"
        return service.merge_result(season_job["job_id"], result, status="dispatched")
    result["stage"] = "done"
    if statuses and all(status == "failed" and True for status in statuses) and all(
        service.get_job(child_id).get("error_code") == "BACKFILL_FAILED" for child_id in children
    ):
        result["note"] = "全季各期补数均失败，尚未出报。失败发生在补数阶段。"
        return service.merge_result(season_job["job_id"], result, status="failed")
    result["note"] = "全季各期任务已下发给下载机（补数与出报）。API 机不渲染 PDF。"
    return service.merge_result(season_job["job_id"], result, status="ready")


async def _job_statuses(job_ids: list[str]) -> dict[str, str] | None:
    try:
        from uuid import UUID

        from sqlalchemy import select

        from app.core.database import async_session
        from app.models.tables import Job

        ids = []
        for raw in job_ids:
            try:
                ids.append(UUID(str(raw)))
            except ValueError:
                continue
        if not ids:
            return {}
        async with async_session() as db:
            rows = (await db.execute(select(Job.id, Job.status).where(Job.id.in_(ids)))).all()
        found = {str(job_id): status for job_id, status in rows}
        # Missing rows are treated as still running so we do not finalize early.
        return {job_id: found.get(job_id, "running") for job_id in job_ids}
    except Exception as exc:  # noqa: BLE001
        logger.warning("biweekly_job_status_query_failed err_type=%s", type(exc).__name__)
        return None
