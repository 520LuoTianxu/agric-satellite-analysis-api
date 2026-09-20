# -*- coding: utf-8 -*-
"""Celery task: generate 选地体检 PDF and store in object storage."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from app.core.database_sync import SyncSession
from app.core.logging import logger
from app.tasks.storage_tasks import upload_file_via_storage
from app.models.tables import Job, SoilProfile, WeatherDaily
from app.reports.land_assessment.scorecard_view import scorecard_public_view
from app.reports.land_assessment.service import generate_assessment_pdf
from app.worker import celery_app

ASSESSMENT_BATCH_MAX_COMPENSATIONS = 2
ASSESSMENT_COMPENSATION_DELAY_SECONDS = 30


def _update_job(
    session,
    job: Job | None,
    status: str,
    progress: dict | None = None,
    error: str | None = None,
    *,
    job_id: str | None = None,
):
    from app.core.job_http import update_job_record

    update_job_record(
        session,
        job,
        status,
        progress=progress,
        error=error,
        job_id=job_id or (str(job.id) if job is not None else None),
    )


def _publish_mq_result(
    *,
    mq_task_id: str | None,
    status: str,
    land_id: str | None,
    error: str | None = None,
    payload: dict | None = None,
    oss_urls: dict[str, str] | None = None,
    extras: dict | None = None,
) -> None:
    if not mq_task_id:
        return
    try:
        from agric_satellite_analysis_common.mq_results import publish_task_result

        publish_task_result(
            task_id=mq_task_id,
            status=status,
            land_id=str(land_id) if land_id else None,
            error=error,
            payload=payload,
            oss_urls=oss_urls or {},
            extras=extras or {},
            collect_parcel_urls=False,
            upload_summary_if_empty=False,
        )
    except Exception as e:
        logger.warning(
            "assessment_mq_result_publish_failed",
            mq_task_id=mq_task_id,
            error=str(e),
        )



def _maybe_sync_session():
    """Open SyncSession only when PG reads are still allowed on this host."""
    try:
        from agric_satellite_analysis_common.internal_api import (
            ingest_pg_reads_allowed,
            internal_api_enabled,
        )

        if internal_api_enabled() and not ingest_pg_reads_allowed():
            return None
    except ImportError:
        pass
    return SyncSession()


def _data_readiness_http(
    land_id: str,
    date_from: str | None,
    date_to: str | None,
) -> dict | None:
    try:
        from agric_satellite_analysis_common.internal_api import data_readiness, internal_api_enabled

        if not internal_api_enabled():
            return None
        return data_readiness(
            str(land_id), date_from=date_from, date_to=date_to
        )
    except Exception as exc:
        logger.warning(
            "assessment_data_readiness_http_failed",
            land_id=str(land_id),
            error=str(exc),
        )
        return None


def _resolve_job(session, job_id: str | None) -> Job | None:
    if not job_id or session is None:
        return None
    try:
        return session.get(Job, uuid.UUID(str(job_id)))
    except (ValueError, TypeError):
        return None


def _assessment_job_snapshot(
    session, job: Job | None, job_id: str | None
) -> tuple[dict, dict, str | None]:
    """读取批次报告的补偿元数据，兼容 API 机与下载机分离部署。"""
    params = dict(getattr(job, "params_json", None) or {})
    progress = dict(getattr(job, "progress_json", None) or {})
    land_id = str(getattr(job, "land_id", None) or "") or None
    if params and land_id:
        return params, progress, land_id
    try:
        from agric_satellite_analysis_common.internal_api import get_job, internal_api_enabled

        if not job_id or not internal_api_enabled():
            return params, progress, land_id
        remote = get_job(str(job_id))
        if not isinstance(remote, dict):
            return params, progress, land_id
        if not params and isinstance(remote.get("params_json"), dict):
            params = dict(remote["params_json"])
        if not progress and isinstance(remote.get("progress_json"), dict):
            progress = dict(remote["progress_json"])
        land_id = str(remote.get("land_id") or land_id or "") or None
    except Exception as exc:
        logger.warning(
            "assessment_compensation_job_snapshot_failed",
            job_id=job_id,
            error=str(exc),
        )
    return params, progress, land_id


def _compensation_progress(progress: dict, attempt: int, *, active: bool) -> dict:
    """给任务进度补充可审计的总次数与当前补偿次数。"""
    output = dict(progress or {})
    output.update(
        {
            "compensation_attempt": attempt,
            "compensation_count": attempt,
            "compensation_max": ASSESSMENT_BATCH_MAX_COMPENSATIONS,
            "total_attempt": attempt + 1,
            "compensation_active_attempt": attempt if active else None,
        }
    )
    return output


def _active_compensation_attempt(progress: dict) -> int | None:
    try:
        value = progress.get("compensation_active_attempt")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _schedule_assessment_compensation(
    task,
    session,
    job: Job | None,
    *,
    task_kwargs: dict,
    compensation_attempt: int,
    error: str,
) -> bool:
    """失败批次报告按原 Job 幂等补偿，最多额外执行两次。"""
    job_id = str(task_kwargs.get("job_id") or "") or None
    if not job_id:
        return False

    params, current_progress, snapshot_land_id = _assessment_job_snapshot(
        session, job, job_id
    )
    # 只有批量报告子任务进入补偿；单地块报告保持原有失败语义。
    if not params.get("batch_id"):
        return False

    try:
        completed_compensations = int(
            current_progress.get("compensation_count")
            or params.get("compensation_count")
            or 0
        )
    except (TypeError, ValueError):
        completed_compensations = 0
    active_attempt = _active_compensation_attempt(current_progress)

    # 旧任务的失败结果可能晚于补偿任务到达；已存在更高补偿序号时只认
    # 已经入队的那一次，避免同一个地块被重复补偿并行执行。
    if active_attempt is not None and active_attempt > compensation_attempt:
        return True

    next_attempt = max(completed_compensations, compensation_attempt) + 1
    if next_attempt > ASSESSMENT_BATCH_MAX_COMPENSATIONS:
        final_progress = _compensation_progress(
            current_progress, completed_compensations, active=False
        )
        final_progress.update(
            {
                "stage": "failed",
                "last_error": str(error)[:2000],
            }
        )
        if job or job_id:
            _update_job(
                session,
                job,
                "failed",
                progress=final_progress,
                error=str(error)[:2000],
                job_id=job_id,
            )
        return False

    retry_kwargs = dict(task_kwargs)
    retry_kwargs["job_id"] = job_id
    retry_kwargs["land_id"] = (
        retry_kwargs.get("land_id") or snapshot_land_id or params.get("land_id")
    )
    if not retry_kwargs.get("land_id"):
        return False
    for key in ("crop_type", "crop_name_zh", "date_from", "date_to", "years"):
        if retry_kwargs.get(key) is None and params.get(key) is not None:
            retry_kwargs[key] = params[key]
    if retry_kwargs.get("pull_data") is None:
        retry_kwargs["pull_data"] = bool(params.get("pull_data", True))
    # 天气/土壤的旧 Celery ID 只属于上一次派发；补偿任务重新按现有数据判定，
    # 不重复等待已经结束或失效的旧 ID。
    retry_kwargs.pop("wait_celery_ids", None)
    retry_kwargs["compensation_attempt"] = next_attempt
    try:
        job_uuid = uuid.UUID(job_id)
        retry_kwargs["mq_task_id"] = str(
            uuid.uuid5(job_uuid, f"assessment-compensation:{next_attempt}")
        )
    except (ValueError, TypeError):
        retry_kwargs["mq_task_id"] = str(uuid.uuid4())

    retry_progress = _compensation_progress(
        current_progress, next_attempt, active=True
    )
    retry_progress.update(
        {
            "stage": "compensating",
            "percent": min(95, max(5, int(current_progress.get("percent") or 5))),
            "last_error": str(error)[:2000],
        }
    )
    try:
        # 先落库“补偿中”再投递，重复失败通知可以据此识别已入队的补偿。
        _update_job(
            session,
            job,
            "running",
            progress=retry_progress,
            job_id=job_id,
        )
        task.apply_async(
            kwargs=retry_kwargs,
            countdown=ASSESSMENT_COMPENSATION_DELAY_SECONDS,
        )
    except Exception as exc:
        logger.exception(
            "assessment_compensation_dispatch_failed",
            job_id=job_id,
            attempt=next_attempt,
            error=str(exc),
        )
        failure_progress = _compensation_progress(
            retry_progress, next_attempt, active=False
        )
        failure_progress.update(
            {"stage": "failed", "last_error": str(exc)[:2000]}
        )
        try:
            _update_job(
                session,
                job,
                "failed",
                progress=failure_progress,
                error=f"补偿任务派发失败：{str(exc)[:1800]}",
                job_id=job_id,
            )
        except Exception:
            logger.exception(
                "assessment_compensation_failure_state_update_failed",
                job_id=job_id,
            )
        return False
    logger.warning(
        "assessment_compensation_scheduled",
        job_id=job_id,
        attempt=next_attempt,
        max_compensations=ASSESSMENT_BATCH_MAX_COMPENSATIONS,
    )
    return True


def _celery_ids_ready(wait_celery_ids: list[str] | None) -> tuple[bool, list[str]]:
    """Return (all_ready, pending_ids). Missing/unknown ids treated as ready."""
    if not wait_celery_ids:
        return True, []
    pending: list[str] = []
    try:
        from celery.result import AsyncResult
    except Exception:
        return True, []
    for cid in wait_celery_ids:
        if not cid:
            continue
        try:
            r = AsyncResult(str(cid), app=celery_app)
            # pending/started/retry → not ready; success/failure/revoked → ready
            if not r.ready():
                pending.append(str(cid))
        except Exception:
            # Broker blip: do not block forever on inspection errors
            continue
    return (len(pending) == 0), pending


def _active_backfill_jobs(session, land_id: str, wave_cutoff: datetime) -> int:
    """Count in-flight index / agri-optical / S1 backfill child jobs."""
    rows = session.execute(
        select(func.count())
        .select_from(Job)
        .where(
            Job.land_id == str(land_id),
            Job.status.in_(("pending", "running")),
            Job.params_json["is_backfill"].as_boolean().is_(True),
            Job.type.notin_(
                ("backfill", "agri_bridge", "assessment_report", "season_growth_report")
            ),
            Job.created_at >= wave_cutoff,
        )
    ).scalar()
    return int(rows or 0)


def _parse_iso_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _agri_rs_coverage_ok(
    session,
    land_id: str,
    date_from: str | None,
    date_to: str | None,
) -> dict:
    """True when agric_satellite.parcel_scene_products already cover the assessment window.

    Used to avoid blocking PDF generation on staggered skip-noop backfill chunks
    (90-day shards with 30s countdown) when S2/S1 lonlat rows already exist.
    """
    from sqlalchemy import text as sa_text

    out = {
        "ok": False,
        "land_id": None,
        "s2_dates": 0,
        "s1_dates": 0,
        "span_days": None,
        "s2_min": None,
        "s1_min": None,
    }
    start = _parse_iso_date(date_from)
    end = _parse_iso_date(date_to)
    if not start or not end or end < start:
        return out

    land_id = str(land_id)
    out["land_id"] = land_id

    span_days = (end - start).days + 1
    out["span_days"] = span_days
    # Roughly ≥ monthly S2 and bi-monthly S1 across the window; clamp for short seasons.
    s2_min = max(6, min(48, span_days // 30))
    s1_min = max(3, min(24, span_days // 60))
    out["s2_min"] = s2_min
    out["s1_min"] = s1_min

    row = (
        session.execute(
            sa_text(
                """
            SELECT
              COUNT(DISTINCT date) FILTER (WHERE sensor = 'S2') AS s2_dates,
              COUNT(DISTINCT date) FILTER (WHERE sensor = 'S1') AS s1_dates
            FROM agric_satellite.parcel_scene_products
            WHERE land_id = :land_id
              AND date >= :d0
              AND date <= :d1
              AND COALESCE(scene_id, '') NOT LIKE '%_decloud'
              AND COALESCE(pixel_data->>'source', '') <> 'uncrtaints_decloud'
            """
            ),
            {"land_id": str(land_id), "d0": start, "d1": end},
        )
        .mappings()
        .first()
    )
    s2_dates = int((row or {}).get("s2_dates") or 0)
    s1_dates = int((row or {}).get("s1_dates") or 0)
    out["s2_dates"] = s2_dates
    out["s1_dates"] = s1_dates
    out["ok"] = s2_dates >= s2_min and s1_dates >= s1_min
    return out


def _weather_row_count(
    session, land_id: str, date_from: str | None, date_to: str | None
) -> int:
    q = (
        select(func.count())
        .select_from(WeatherDaily)
        .where(WeatherDaily.land_id == str(land_id))
    )
    if date_from:
        try:
            q = q.where(
                WeatherDaily.date >= datetime.fromisoformat(date_from[:10]).date()
            )
        except ValueError:
            pass
    if date_to:
        try:
            q = q.where(
                WeatherDaily.date <= datetime.fromisoformat(date_to[:10]).date()
            )
        except ValueError:
            pass
    return int(session.execute(q).scalar() or 0)


def _soil_ready(session, land_id: str) -> bool:
    row = session.execute(
        select(SoilProfile.id).where(SoilProfile.land_id == str(land_id)).limit(1)
    ).first()
    return bool(row)



def _resolve_wait_started_at(job, job_id_str: str | None) -> datetime:
    """Stable wait clock across retries when local Job row is absent.

    Prefer local created_at/started_at; else GET /internal/jobs/{id} so each
    30s retry does not reset started_at=now (which would stall min_wait_seconds).
    """
    if job is not None:
        if getattr(job, "created_at", None):
            return job.created_at
        if getattr(job, "started_at", None):
            return job.started_at
    if job_id_str:
        try:
            from agric_satellite_analysis_common.internal_api import get_job, internal_api_enabled

            if internal_api_enabled():
                remote = get_job(str(job_id_str))
                for key in ("created_at", "started_at"):
                    raw = remote.get(key) if isinstance(remote, dict) else None
                    if not raw:
                        continue
                    s = str(raw).replace("Z", "+00:00")
                    return datetime.fromisoformat(s)
        except Exception as exc:
            logger.warning(
                "assessment_wait_started_at_http_failed",
                job_id=job_id_str,
                error=str(exc),
            )
    return datetime.now(timezone.utc)


def bootstrap_pulls_ready(
    session,
    *,
    land_id: str,
    date_from: str | None,
    date_to: str | None,
    wait_celery_ids: list[str] | None,
    wave_cutoff: datetime,
    weather_min_rows: int = 7,
    started_at: datetime | None = None,
    min_wait_seconds: int = 45,
    require_rs_coverage: bool = False,
) -> dict:
    """Check whether weather + soil + RS wave are ready for assessment PDF.

    Returns a status dict used by the Celery waiter (and unit tests).

    ``min_wait_seconds`` guards the legacy race where assessment starts before
    bootstrap has created agri_optical child jobs — "0 active RS" must not
    look ready in the first seconds.
    """
    celery_ready, pending_ids = _celery_ids_ready(wait_celery_ids)
    readiness = _data_readiness_http(land_id, date_from, date_to)
    if readiness is not None:
        weather_rows = int(readiness.get("weather_rows") or 0)
        soil_ok = bool(readiness.get("soil_ok"))
        # Active RS jobs still need local Job table or celery ids — skip PG count.
        active_rs = 0
        if session is not None:
            try:
                active_rs = _active_backfill_jobs(session, land_id, wave_cutoff)
            except Exception:
                active_rs = 0
        span = readiness.get("span_days")
        s2_min = max(6, min(48, int(span) // 30)) if span else None
        s1_min = max(3, min(24, int(span) // 60)) if span else None
        s2_dates = int(readiness.get("s2_dates") or 0)
        s1_dates = int(readiness.get("s1_dates") or 0)
        coverage = {
            "ok": bool(
                s2_min is not None
                and s1_min is not None
                and s2_dates >= s2_min
                and s1_dates >= s1_min
            ),
            "land_id": readiness.get("land_id"),
            "s2_dates": s2_dates,
            "s1_dates": s1_dates,
            "span_days": span,
            "s2_min": s2_min,
            "s1_min": s1_min,
        }
    else:
        if session is None:
            raise RuntimeError(
                "bootstrap_pulls_ready needs SyncSession or internal data-readiness"
            )
        weather_rows = _weather_row_count(session, land_id, date_from, date_to)
        soil_ok = _soil_ready(session, land_id)
        active_rs = _active_backfill_jobs(session, land_id, wave_cutoff)
        coverage = _agri_rs_coverage_ok(session, land_id, date_from, date_to)

    weather_ok = weather_rows >= weather_min_rows
    # After top-level weather/soil/indices orchestration finishes, wait until
    # child agri_optical / index jobs drain (or never started). If agri lonlat
    # coverage for the window is already sufficient, do not block on staggered
    # skip-noop backfill chunks still sitting in the ingest queue.
    rs_ok = celery_ready and (active_rs == 0 or bool(coverage.get("ok")))
    # pull_data waits: empty coverage must not look RS-ready forever when the
    # backfill wave never started (active_rs==0 on HTTP-only download host).
    if require_rs_coverage:
        rs_ok = rs_ok and bool(coverage.get("ok"))

    elapsed_ok = True
    if started_at is not None and min_wait_seconds > 0:
        now = datetime.now(timezone.utc)
        started = (
            started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
        )
        elapsed_ok = (now - started).total_seconds() >= min_wait_seconds

    # Without explicit celery ids, require soil+weather evidence before trusting
    # an empty RS wave (avoids soil-only race with parallel MQ publish).
    if not wait_celery_ids:
        rs_ok = rs_ok and weather_ok and soil_ok and elapsed_ok

    ready = celery_ready and weather_ok and soil_ok and rs_ok and elapsed_ok
    return {
        "ready": ready,
        "celery_ready": celery_ready,
        "pending_celery_ids": pending_ids,
        "weather_rows": weather_rows,
        "weather_ok": weather_ok,
        "soil_ok": soil_ok,
        "active_rs_jobs": active_rs,
        "rs_ok": rs_ok,
        "elapsed_ok": elapsed_ok,
        "rs_coverage_ok": bool(coverage.get("ok")),
        "rs_coverage": {
            k: coverage.get(k)
            for k in (
                "land_id",
                "s2_dates",
                "s1_dates",
                "s2_min",
                "s1_min",
                "span_days",
            )
        },
    }


@celery_app.task(
    name="app.tasks.assessment_report.generate_assessment_report",
    bind=True,
    max_retries=90,
    default_retry_delay=30,
)
def generate_assessment_report(
    self,
    job_id: str | None = None,
    mq_task_id: str | None = None,
    work_item_id: str | None = None,
    land_id: str | None = None,
    crop_type: str | None = None,
    crop_name_zh: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    years: int | None = None,
    pull_data: bool = False,
    wait_celery_ids: list | None = None,
    compensation_attempt: int = 0,
) -> dict:
    """Generate land assessment PDF for one canonical land parcel.

    Cross-host safe: ``land_id`` is the source of truth for PDF generation.
    When ``job_id`` is present *and* a Job row exists in *this* DB, update
    progress as before. Missing local Job is not a hard failure — still
    generate/upload/publish ResultMessage (with ``job_id`` in payload) so the
    process-host writer can update the API Job.

    When ``pull_data`` is true (one-click CTA), wait for bootstrap weather +
    soil + agri RS wave before building the PDF so we do not race a soil-only
    report. After max retries, proceed with whatever data is available.
    """
    session = _maybe_sync_session()
    land_id_str: str | None = str(land_id) if land_id else None
    job_id_str: str | None = str(job_id) if job_id else None
    job: Job | None = None
    task_kwargs = {
        "job_id": job_id_str,
        "mq_task_id": mq_task_id,
        "work_item_id": work_item_id,
        "land_id": land_id_str,
        "crop_type": crop_type,
        "crop_name_zh": crop_name_zh,
        "date_from": date_from,
        "date_to": date_to,
        "years": years,
        "pull_data": pull_data,
        "wait_celery_ids": wait_celery_ids,
    }
    try:
        job = _resolve_job(session, job_id_str)
        snapshot_params, snapshot_progress, _ = _assessment_job_snapshot(
            session, job, job_id_str
        )
        active_attempt = _active_compensation_attempt(snapshot_progress)
        if (
            snapshot_params.get("batch_id")
            and active_attempt is not None
            and active_attempt > compensation_attempt
        ):
            # 旧任务可能因租约回收迟到执行；补偿已经入队时直接结束旧执行，
            # 防止它覆盖新任务的进度或再次消耗补偿次数。
            return {
                "status": "compensating",
                "compensation_attempt": active_attempt,
            }

        if not land_id_str and job and job.land_id:
            land_id_str = str(job.land_id)

        if not land_id_str:
            logger.error(
                "assessment_land_id_missing",
                job_id=job_id_str,
                mq_task_id=mq_task_id,
            )
            if _schedule_assessment_compensation(
                self,
                session,
                job,
                task_kwargs=task_kwargs,
                compensation_attempt=compensation_attempt,
                error="land_id required",
            ):
                return {
                    "status": "compensating",
                    "compensation_attempt": compensation_attempt + 1,
                }
            if job or job_id_str:
                _update_job(session, job, "failed", error="land_id required", job_id=job_id_str)
            _publish_mq_result(
                mq_task_id=mq_task_id,
                status="failed",
                land_id=None,
                error="land_id required",
                extras={
                    "source": "assessment_report",
                    **({"job_id": job_id_str} if job_id_str else {}),
                },
            )
            return {"error": "land_id required"}

        if job_id_str and not job:
            # Cross-host: Job lives on API DB; download host has none.
            logger.info(
                "assessment_job_absent_local",
                job_id=job_id_str,
                land_id=land_id_str,
                mq_task_id=mq_task_id,
            )

        if pull_data:
            wave_cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
            if job and job.created_at:
                # Include bootstrap fan-out shortly before assessment job row
                wave_cutoff = job.created_at - timedelta(minutes=2)
            weather_min_rows = 7
            if date_from and date_to:
                try:
                    span = (
                        datetime.fromisoformat(date_to[:10]).date()
                        - datetime.fromisoformat(date_from[:10]).date()
                    ).days + 1
                    weather_min_rows = max(1, min(7, span))
                except ValueError:
                    pass
            # Do not wait on bridge_after_backfill id itself forever via AsyncResult
            # alone — it retries up to ~90min; we still list top-level pull ids and
            # use active_rs_jobs for the RS wave.
            started_at = _resolve_wait_started_at(job, job_id_str)
            status = bootstrap_pulls_ready(
                session,
                land_id=land_id_str,
                date_from=date_from,
                date_to=date_to,
                wait_celery_ids=[str(x) for x in (wait_celery_ids or []) if x],
                wave_cutoff=wave_cutoff,
                weather_min_rows=weather_min_rows,
                started_at=started_at,
                min_wait_seconds=45,
                require_rs_coverage=True,
            )
            if not status["ready"]:
                retries = int(getattr(self.request, "retries", 0) or 0)
                max_r = int(getattr(self, "max_retries", 90) or 90)
                progress_wait = {
                    "stage": "waiting_for_data",
                    "percent": min(55, 10 + retries),
                    "pull_data": True,
                    "weather_rows": status["weather_rows"],
                    "weather_ok": status["weather_ok"],
                    "soil_ok": status["soil_ok"],
                    "active_rs_jobs": status["active_rs_jobs"],
                    "rs_ok": status["rs_ok"],
                    "rs_coverage_ok": status.get("rs_coverage_ok"),
                    "rs_coverage": status.get("rs_coverage"),
                    "celery_ready": status["celery_ready"],
                    "pending_celery": len(status["pending_celery_ids"]),
                }
                if compensation_attempt:
                    progress_wait = _compensation_progress(
                        progress_wait, compensation_attempt, active=True
                    )
                if job or job_id_str:
                    _update_job(session, job, "running", progress=progress_wait, job_id=job_id_str)
                if retries < max_r:
                    logger.info(
                        "assessment_waiting_for_bootstrap",
                        land_id=land_id_str,
                        job_id=job_id_str,
                        retry=retries,
                        **{
                            k: status[k]
                            for k in (
                                "weather_rows",
                                "weather_ok",
                                "soil_ok",
                                "active_rs_jobs",
                                "rs_ok",
                                "celery_ready",
                            )
                        },
                    )
                    raise self.retry(countdown=30)
                logger.warning(
                    "assessment_bootstrap_wait_timeout",
                    land_id=land_id_str,
                    job_id=job_id_str,
                    **{
                        k: status[k]
                        for k in (
                            "weather_rows",
                            "weather_ok",
                            "soil_ok",
                            "active_rs_jobs",
                            "rs_ok",
                            "celery_ready",
                        )
                    },
                )

        scoring_progress = {
            "stage": "scoring",
            "percent": 60 if pull_data else 10,
        }
        if compensation_attempt:
            scoring_progress = _compensation_progress(
                scoring_progress, compensation_attempt, active=True
            )
        if job or job_id_str:
            _update_job(
                session,
                job,
                "running",
                progress=scoring_progress,
                job_id=job_id_str,
            )

        result = generate_assessment_pdf(
            session=session, land_id=land_id_str
        )
        pdf_path = Path(result["out_path"])
        if not pdf_path.exists():
            if _schedule_assessment_compensation(
                self,
                session,
                job,
                task_kwargs=task_kwargs,
                compensation_attempt=compensation_attempt,
                error="PDF not produced",
            ):
                return {
                    "status": "compensating",
                    "compensation_attempt": compensation_attempt + 1,
                }
            if job or job_id_str:
                _update_job(session, job, "failed", error="PDF not produced", job_id=job_id_str)
            _publish_mq_result(
                mq_task_id=mq_task_id,
                status="failed",
                land_id=land_id_str,
                error="PDF not produced",
                extras={
                    "source": "assessment_report",
                    **({"job_id": job_id_str} if job_id_str else {}),
                },
            )
            return {"error": "PDF not produced"}

        uploading_progress = {
            "stage": "uploading",
            "percent": 70,
            "score": result["score"],
        }
        if compensation_attempt:
            uploading_progress = _compensation_progress(
                uploading_progress, compensation_attempt, active=True
            )
        if job or job_id_str:
            _update_job(
                session,
                job,
                "running",
                progress=uploading_progress,
                job_id=job_id_str,
            )

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        object_key = f"reports/default/{land_id_str}/assessment-{ts}.pdf"
        upload_result = upload_file_via_storage(
            object_key, str(pdf_path), content_type="application/pdf"
        )
        public_url = None
        if isinstance(upload_result, dict):
            public_url = upload_result.get("public_url")
        if not public_url:
            try:
                from agric_satellite_analysis_common.storage import get_storage

                public_url = get_storage().public_url(object_key)
            except Exception as e:
                logger.warning(
                    "assessment_public_url_fallback_failed",
                    object_key=object_key,
                    error=str(e),
                )

        flood = result.get("flood_evidence")
        flood_summary = None
        if flood:
            flood_summary = {
                "absolute_open_water_scenes": flood.get("absolute_open_water_scenes"),
                "selected_count": flood.get("selected_count"),
                "analysis": flood.get("analysis"),
                "all_dates": flood.get("all_dates"),
                "scenes": [
                    {
                        "date": s.get("date"),
                        "wet_mean": s.get("wet_mean"),
                        "ndvi_mean": s.get("ndvi_mean"),
                        "kind": s.get("kind"),
                        "analysis": s.get("analysis"),
                        "precip_prior_15d": {
                            k: v
                            for k, v in (s.get("precip_prior_15d") or {}).items()
                            if k != "days"
                        },
                        "media": {
                            "preview_url": (s.get("media") or {}).get("preview_url"),
                            "has_oss": (s.get("media") or {}).get("has_oss"),
                        },
                    }
                    for s in (flood.get("scenes") or [])
                ],
            }
        public_scorecard = scorecard_public_view(
            result.get("scorecard"), ai=result.get("ai")
        )
        progress = {
            "stage": "done",
            "percent": 100,
            "object_key": object_key,
            "public_url": public_url,
            "filename": result.get("download_filename")
            or f"{result['field_name'] or 'field'}地块--分析报告.pdf",
            "score": result["score"],
            "grade": result["grade"],
            "light": result["light"],
            "one_liner": result.get("one_liner"),
            "area_mu": result.get("area_mu"),
            "indices_source": result.get("indices_source"),
            "content_type": "application/pdf",
            "flood_evidence": flood_summary,
            "scorecard": public_scorecard,
            "rs": {
                "absolute_open_water_scenes": (result.get("rs") or {}).get(
                    "absolute_open_water_scenes"
                ),
                "open_water_dates": (result.get("rs") or {}).get("open_water_dates"),
                "rs_flood_level": (result.get("rs") or {}).get("rs_flood_level"),
            },
        }
        if crop_type:
            progress["crop_type"] = crop_type
        if crop_name_zh:
            progress["crop_name_zh"] = crop_name_zh
        if date_from:
            progress["date_from"] = date_from
        if date_to:
            progress["date_to"] = date_to
        if years is not None:
            progress["years"] = years
        if pull_data:
            progress["pull_data"] = True
        if compensation_attempt:
            progress = _compensation_progress(
                progress, compensation_attempt, active=False
            )

        # Soft note when series are sparse — PDF still generated with available data
        notes: list[str] = []
        n_rows = int(result.get("n_index_rows") or 0)
        if n_rows < 8:
            notes.append(
                "遥感指数样本偏少；若已排队拉取，稍后重生成可获得更完整长势评分"
            )
        if notes:
            progress["data_notes"] = notes
            progress["data_partial"] = True

        if job or job_id_str:
            _update_job(session, job, "succeeded", progress=progress, job_id=job_id_str)
        logger.info(
            "assessment_report_done",
            job_id=job_id_str,
            land_id=land_id_str,
            object_key=object_key,
            public_url=public_url,
            score=result["score"],
            mq_task_id=mq_task_id,
            local_job_updated=bool(job),
            pull_data=bool(pull_data),
        )

        oss_urls: dict[str, str] = {}
        if public_url:
            oss_urls["assessment_pdf"] = public_url
        mq_payload = {
            "kind": "assessment_report",
            "land_id": land_id_str,
            "object_key": object_key,
            "public_url": public_url,
            "filename": progress["filename"],
            "score": result["score"],
            "grade": result["grade"],
            "light": result.get("light"),
            "one_liner": result.get("one_liner"),
            "area_mu": result.get("area_mu"),
            "content_type": "application/pdf",
            "scorecard": public_scorecard,
        }
        if job_id_str:
            mq_payload["job_id"] = job_id_str
        if crop_type:
            mq_payload["crop_type"] = crop_type
        if crop_name_zh:
            mq_payload["crop_name_zh"] = crop_name_zh
        if compensation_attempt:
            mq_payload["compensation_attempt"] = compensation_attempt

        extras_out: dict = {"source": "assessment_report"}
        if job_id_str:
            extras_out["job_id"] = job_id_str
        _publish_mq_result(
            mq_task_id=mq_task_id,
            status="success",
            land_id=land_id_str,
            payload=mq_payload,
            oss_urls=oss_urls,
            extras=extras_out,
        )
        try:
            from app.core.job_http import complete_work_item_http

            complete_work_item_http(work_item_id, mq_payload)
        except Exception:
            pass
        # When HTTP writes on and local job missing, still patch API job via apply
        if job is None and job_id_str:
            try:
                from agric_satellite_analysis_common.internal_api import apply_results, http_writes_enabled

                if http_writes_enabled():
                    apply_results(mq_payload)
            except Exception as e:
                logger.warning(
                    "assessment_http_apply_failed",
                    job_id=job_id_str,
                    error=str(e),
                )
        return progress
    except Exception as exc:
        from celery.exceptions import Retry

        if isinstance(exc, Retry):
            raise
        logger.exception(
            "assessment_report_failed",
            job_id=job_id_str,
            land_id=land_id_str,
            error=str(exc),
        )
        try:
            if job is None and job_id_str:
                job = _resolve_job(session, job_id_str)
            if _schedule_assessment_compensation(
                self,
                session,
                job,
                task_kwargs=task_kwargs,
                compensation_attempt=compensation_attempt,
                error=str(exc),
            ):
                return {
                    "status": "compensating",
                    "compensation_attempt": compensation_attempt + 1,
                }
            if job or job_id_str:
                if job and not land_id_str and job.land_id:
                    land_id_str = str(job.land_id)
                _update_job(
                    session,
                    job,
                    "failed",
                    error=str(exc)[:2000],
                    job_id=job_id_str,
                )
        except Exception:
            logger.exception(
                "assessment_report_failure_state_update_failed",
                job_id=job_id_str,
            )
        extras_fail: dict = {"source": "assessment_report"}
        if job_id_str:
            extras_fail["job_id"] = job_id_str
        if compensation_attempt:
            extras_fail["compensation_attempt"] = compensation_attempt
        fail_payload = {
            "kind": "assessment_report",
            "land_id": land_id_str,
            "error": str(exc)[:2000],
            **({"job_id": job_id_str} if job_id_str else {}),
        }
        if compensation_attempt:
            fail_payload["compensation_attempt"] = compensation_attempt
        _publish_mq_result(
            mq_task_id=mq_task_id,
            status="failed",
            land_id=land_id_str,
            error=str(exc)[:500],
            extras=extras_fail,
            payload=fail_payload,
        )
        try:
            from app.core.job_http import fail_work_item_http

            fail_work_item_http(work_item_id, str(exc)[:2000])
        except Exception:
            pass
        if job is None and job_id_str:
            try:
                from agric_satellite_analysis_common.internal_api import apply_results, http_writes_enabled

                if http_writes_enabled():
                    apply_results({**fail_payload, "status": "failed"})
            except Exception:
                pass
        raise
    finally:
        if session is not None:
            session.close()
