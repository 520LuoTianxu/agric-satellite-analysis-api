# -*- coding: utf-8 -*-
"""Celery task: generate 生育期长势 PDF and store in object storage."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm.attributes import flag_modified

from app.core.database_sync import SyncSession
from app.core.logging import logger
from app.models.tables import Job
from app.reports.season_growth.service import generate_season_growth_pdf
from app.tasks.assessment_report import bootstrap_pulls_ready
from app.tasks.storage_tasks import upload_file_via_storage
from app.worker import celery_app


def _update_job(
    session,
    job: Job,
    status: str,
    progress: dict | None = None,
    error: str | None = None,
):
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


def _publish_mq_result(
    *,
    mq_task_id: str | None,
    status: str,
    field_id: str | None,
    error: str | None = None,
    payload: dict | None = None,
    oss_urls: dict[str, str] | None = None,
    extras: dict | None = None,
) -> None:
    if not mq_task_id:
        return
    try:
        from openfarm_common.mq_results import publish_task_result

        publish_task_result(
            task_id=mq_task_id,
            status=status,
            field_id=str(field_id) if field_id else None,
            error=error,
            payload=payload,
            oss_urls=oss_urls or {},
            extras=extras or {},
            collect_parcel_urls=False,
            upload_summary_if_empty=False,
        )
    except Exception as e:
        logger.warning(
            "season_growth_mq_result_publish_failed",
            mq_task_id=mq_task_id,
            error=str(e),
        )


def _resolve_job(session, job_id: str | None) -> Job | None:
    if not job_id:
        return None
    try:
        return session.get(Job, uuid.UUID(str(job_id)))
    except (ValueError, TypeError):
        return None


@celery_app.task(
    name="app.tasks.season_growth_report.generate_season_growth_report",
    bind=True,
    max_retries=90,
    default_retry_delay=30,
)
def generate_season_growth_report(
    self,
    job_id: str | None = None,
    mq_task_id: str | None = None,
    field_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    crops: list | None = None,
    label: str | None = None,
    material_keys: list | None = None,
    pull_data: bool = False,
    wait_celery_ids: list | None = None,
) -> dict:
    """Generate season-growth PDF for a field + growing-season window.

    When ``pull_data`` is true (one-click CTA), wait for bootstrap weather +
    soil + agri RS wave before building the PDF so we do not race an empty
    S1/S2 window. After max retries, proceed with whatever data is available.
    """
    session = SyncSession()
    field_id_str: str | None = str(field_id) if field_id else None
    job_id_str: str | None = str(job_id) if job_id else None
    job: Job | None = None
    try:
        job = _resolve_job(session, job_id_str)

        params = {}
        if job and isinstance(job.params_json, dict):
            params = dict(job.params_json)

        if not field_id_str and job and job.field_id:
            field_id_str = str(job.field_id)

        start_date = start_date or params.get("start_date")
        end_date = end_date or params.get("end_date")
        crops = crops if crops is not None else params.get("crops")
        label = label if label is not None else params.get("label")
        material_keys = (
            material_keys if material_keys is not None else params.get("material_keys")
        )
        if not pull_data and params.get("pull_data") is not None:
            pull_data = bool(params.get("pull_data"))

        if not field_id_str:
            if job:
                _update_job(session, job, "failed", error="field_id required")
            _publish_mq_result(
                mq_task_id=mq_task_id,
                status="failed",
                field_id=None,
                error="field_id required",
                extras={
                    "source": "season_growth_report",
                    **({"job_id": job_id_str} if job_id_str else {}),
                },
            )
            return {"error": "field_id required"}

        if job_id_str and not job:
            logger.info(
                "season_growth_job_absent_local",
                job_id=job_id_str,
                field_id=field_id_str,
                mq_task_id=mq_task_id,
            )

        if not start_date or not end_date:
            err = "start_date and end_date required"
            if job:
                _update_job(session, job, "failed", error=err)
            _publish_mq_result(
                mq_task_id=mq_task_id,
                status="failed",
                field_id=field_id_str,
                error=err,
                extras={
                    "source": "season_growth_report",
                    **({"job_id": job_id_str} if job_id_str else {}),
                },
            )
            return {"error": err}

        if pull_data:
            wave_cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
            if job and job.created_at:
                wave_cutoff = job.created_at - timedelta(minutes=2)
            weather_min_rows = 7
            try:
                span = (
                    datetime.fromisoformat(str(end_date)[:10]).date()
                    - datetime.fromisoformat(str(start_date)[:10]).date()
                ).days + 1
                weather_min_rows = max(1, min(7, span))
            except ValueError:
                pass
            started_at = None
            if job and job.created_at:
                started_at = job.created_at
            elif job and job.started_at:
                started_at = job.started_at
            else:
                started_at = datetime.now(timezone.utc)
            status = bootstrap_pulls_ready(
                session,
                field_id=uuid.UUID(field_id_str),
                date_from=str(start_date)[:10],
                date_to=str(end_date)[:10],
                wait_celery_ids=[str(x) for x in (wait_celery_ids or []) if x],
                wave_cutoff=wave_cutoff,
                weather_min_rows=weather_min_rows,
                started_at=started_at,
                min_wait_seconds=45,
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
                if job:
                    _update_job(session, job, "running", progress=progress_wait)
                if retries < max_r:
                    logger.info(
                        "season_growth_waiting_for_bootstrap",
                        field_id=field_id_str,
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
                    "season_growth_bootstrap_wait_timeout",
                    field_id=field_id_str,
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

        if job:
            _update_job(
                session,
                job,
                "running",
                progress={"stage": "facts", "percent": 60 if pull_data else 15},
            )

        result = generate_season_growth_pdf(
            session=session,
            field_id=uuid.UUID(field_id_str),
            start_date=str(start_date),
            end_date=str(end_date),
            crops=list(crops or []),
            label=label,
            material_keys=list(material_keys or []),
        )
        pdf_path = Path(result["out_path"])
        if not pdf_path.exists():
            if job:
                _update_job(session, job, "failed", error="PDF not produced")
            _publish_mq_result(
                mq_task_id=mq_task_id,
                status="failed",
                field_id=field_id_str,
                error="PDF not produced",
                extras={
                    "source": "season_growth_report",
                    **({"job_id": job_id_str} if job_id_str else {}),
                },
            )
            return {"error": "PDF not produced"}

        if job:
            _update_job(
                session,
                job,
                "running",
                progress={"stage": "uploading", "percent": 70},
            )

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        object_key = f"reports/season_growth/{field_id_str}/season-growth-{ts}.pdf"
        upload_result = upload_file_via_storage(
            object_key, str(pdf_path), content_type="application/pdf"
        )
        public_url = None
        if isinstance(upload_result, dict):
            public_url = upload_result.get("public_url")
        if not public_url:
            try:
                from openfarm_common.storage import get_storage

                public_url = get_storage().public_url(object_key)
            except Exception as e:
                logger.warning(
                    "season_growth_public_url_fallback_failed",
                    object_key=object_key,
                    error=str(e),
                )

        summary = result.get("summary") or {}
        progress = {
            "stage": "done",
            "percent": 100,
            "object_key": object_key,
            "public_url": public_url,
            "filename": result.get("download_filename") or "生育期长势分析报告.pdf",
            "one_liner": summary.get("one_liner"),
            "llm_configured": summary.get("llm_configured"),
            "scenes": summary.get("scenes"),
            "ndvi_mean": summary.get("ndvi_mean"),
            "ndvi_peak": summary.get("ndvi_peak"),
            "harvest": summary.get("harvest"),
            "drought_scene_count": summary.get("drought_scene_count"),
            "flood_status": summary.get("flood_status"),
            "window": summary.get("window"),
            "content_type": "application/pdf",
        }

        if job:
            _update_job(session, job, "succeeded", progress=progress)
        logger.info(
            "season_growth_report_done",
            job_id=job_id_str,
            field_id=field_id_str,
            object_key=object_key,
            public_url=public_url,
            mq_task_id=mq_task_id,
            local_job_updated=bool(job),
        )

        oss_urls: dict[str, str] = {}
        if public_url:
            oss_urls["season_growth_pdf"] = public_url
        mq_payload = {
            "kind": "season_growth_report",
            "field_id": field_id_str,
            "object_key": object_key,
            "public_url": public_url,
            "filename": progress["filename"],
            "one_liner": progress.get("one_liner"),
            "llm_configured": progress.get("llm_configured"),
            "scenes": progress.get("scenes"),
            "ndvi_mean": progress.get("ndvi_mean"),
            "ndvi_peak": progress.get("ndvi_peak"),
            "harvest": progress.get("harvest"),
            "window": progress.get("window"),
            "content_type": "application/pdf",
        }
        if job_id_str:
            mq_payload["job_id"] = job_id_str

        extras_out: dict = {"source": "season_growth_report"}
        if job_id_str:
            extras_out["job_id"] = job_id_str
        _publish_mq_result(
            mq_task_id=mq_task_id,
            status="success",
            field_id=field_id_str,
            payload=mq_payload,
            oss_urls=oss_urls,
            extras=extras_out,
        )
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass
        return progress
    except Exception as exc:
        logger.exception(
            "season_growth_report_failed",
            job_id=job_id_str,
            field_id=field_id_str,
            error=str(exc),
        )
        try:
            if job is None and job_id_str:
                job = _resolve_job(session, job_id_str)
            if job:
                field_id_str = field_id_str or (
                    str(job.field_id) if job.field_id else None
                )
                _update_job(session, job, "failed", error=str(exc)[:2000])
        except Exception:
            pass
        extras_fail: dict = {"source": "season_growth_report"}
        if job_id_str:
            extras_fail["job_id"] = job_id_str
        _publish_mq_result(
            mq_task_id=mq_task_id,
            status="failed",
            field_id=field_id_str,
            error=str(exc)[:500],
            extras=extras_fail,
            payload={
                "kind": "season_growth_report",
                "field_id": field_id_str,
                **({"job_id": job_id_str} if job_id_str else {}),
            },
        )
        raise
    finally:
        session.close()
