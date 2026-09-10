# -*- coding: utf-8 -*-
"""Celery task: generate 选地体检 PDF and store in object storage."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm.attributes import flag_modified

from app.core.database_sync import SyncSession
from app.core.logging import logger
from app.tasks.storage_tasks import upload_file_via_storage
from app.models.tables import Job
from app.reports.land_assessment.service import generate_assessment_pdf
from app.worker import celery_app


def _update_job(
    session,
    job: Job,
    status: str,
    progress: dict | None = None,
    error: str | None = None):
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
            "assessment_mq_result_publish_failed",
            mq_task_id=mq_task_id,
            error=str(e),
        )


@celery_app.task(
    name="app.tasks.assessment_report.generate_assessment_report",
    bind=True,
    max_retries=1,
    default_retry_delay=30)
def generate_assessment_report(
    self, job_id: str, mq_task_id: str | None = None
) -> dict:
    """Generate land assessment PDF for a field job.

    When ``mq_task_id`` is set (CloudAMQP assessment_report), publish a
    ResultMessage with OSS ``public_url`` / ``object_key`` on completion.
    """
    session = SyncSession()
    field_id_str: str | None = None
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            logger.error("assessment_job_missing", job_id=job_id)
            _publish_mq_result(
                mq_task_id=mq_task_id,
                status="failed",
                field_id=None,
                error="job not found",
                extras={"source": "assessment_report", "job_id": job_id},
            )
            return {"error": "job not found"}

        if not job.field_id:
            _update_job(session, job, "failed", error="field_id required")
            _publish_mq_result(
                mq_task_id=mq_task_id,
                status="failed",
                field_id=None,
                error="field_id required",
                extras={"source": "assessment_report", "job_id": job_id},
            )
            return {"error": "field_id required"}

        field_id_str = str(job.field_id)
        _update_job(
            session,
            job,
            "running",
            progress={"stage": "scoring", "percent": 10})

        result = generate_assessment_pdf(session=session, field_id=job.field_id)
        pdf_path = Path(result["out_path"])
        if not pdf_path.exists():
            _update_job(session, job, "failed", error="PDF not produced")
            _publish_mq_result(
                mq_task_id=mq_task_id,
                status="failed",
                field_id=field_id_str,
                error="PDF not produced",
                extras={"source": "assessment_report", "job_id": job_id},
            )
            return {"error": "PDF not produced"}

        _update_job(
            session,
            job,
            "running",
            progress={"stage": "uploading", "percent": 70, "score": result["score"]})

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        object_key = f"reports/default/{job.field_id}/assessment-{ts}.pdf"
        upload_result = upload_file_via_storage(
            object_key,
            str(pdf_path),
            content_type="application/pdf")
        public_url = None
        if isinstance(upload_result, dict):
            public_url = upload_result.get("public_url")
        if not public_url:
            try:
                from openfarm_common.storage import get_storage

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
            "scorecard": result.get("scorecard"),
            "rs": {
                "absolute_open_water_scenes": (result.get("rs") or {}).get(
                    "absolute_open_water_scenes"
                ),
                "open_water_dates": (result.get("rs") or {}).get("open_water_dates"),
                "rs_flood_level": (result.get("rs") or {}).get("rs_flood_level"),
            },
        }
        _update_job(session, job, "succeeded", progress=progress)
        logger.info(
            "assessment_report_done",
            job_id=job_id,
            field_id=field_id_str,
            object_key=object_key,
            public_url=public_url,
            score=result["score"],
            mq_task_id=mq_task_id,
        )

        oss_urls: dict[str, str] = {}
        if public_url:
            oss_urls["assessment_pdf"] = public_url
        mq_payload = {
            "kind": "assessment_report",
            "job_id": job_id,
            "field_id": field_id_str,
            "object_key": object_key,
            "public_url": public_url,
            "filename": progress["filename"],
            "score": result["score"],
            "grade": result["grade"],
            "light": result.get("light"),
            "one_liner": result.get("one_liner"),
            "area_mu": result.get("area_mu"),
            "content_type": "application/pdf",
        }
        _publish_mq_result(
            mq_task_id=mq_task_id,
            status="success",
            field_id=field_id_str,
            payload=mq_payload,
            oss_urls=oss_urls,
            extras={
                "source": "assessment_report",
                "job_id": job_id,
            },
        )
        return progress
    except Exception as exc:
        logger.exception("assessment_report_failed", job_id=job_id, error=str(exc))
        try:
            job = session.get(Job, uuid.UUID(job_id))
            if job:
                field_id_str = field_id_str or (
                    str(job.field_id) if job.field_id else None
                )
                _update_job(session, job, "failed", error=str(exc)[:2000])
        except Exception:
            pass
        _publish_mq_result(
            mq_task_id=mq_task_id,
            status="failed",
            field_id=field_id_str,
            error=str(exc)[:500],
            extras={"source": "assessment_report", "job_id": job_id},
        )
        raise
    finally:
        session.close()
