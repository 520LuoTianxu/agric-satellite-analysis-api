# -*- coding: utf-8 -*-
"""Celery task: generate 选地体检 PDF and store in object storage."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm.attributes import flag_modified

from app.core.database_sync import SyncSession
from app.core.logging import logger
from app.core.storage import get_storage
from app.models.tables import Job
from app.reports.land_assessment.service import generate_assessment_pdf
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


@celery_app.task(
    name="app.tasks.assessment_report.generate_assessment_report",
    bind=True,
    max_retries=1,
    default_retry_delay=30,
)
def generate_assessment_report(self, job_id: str) -> dict:
    """Generate land assessment PDF for a field job."""
    session = SyncSession()
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            logger.error("assessment_job_missing", job_id=job_id)
            return {"error": "job not found"}

        if not job.field_id:
            _update_job(session, job, "failed", error="field_id required")
            return {"error": "field_id required"}

        _update_job(
            session,
            job,
            "running",
            progress={"stage": "scoring", "percent": 10},
        )

        result = generate_assessment_pdf(session=session, field_id=job.field_id)
        pdf_path = Path(result["out_path"])
        if not pdf_path.exists():
            _update_job(session, job, "failed", error="PDF not produced")
            return {"error": "PDF not produced"}

        _update_job(
            session,
            job,
            "running",
            progress={"stage": "uploading", "percent": 70, "score": result["score"]},
        )

        storage = get_storage()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        object_key = f"reports/{job.org_id}/{job.field_id}/assessment-{ts}.pdf"
        storage.upload_file(
            object_key,
            str(pdf_path),
            content_type="application/pdf",
        )

        progress = {
            "stage": "done",
            "percent": 100,
            "object_key": object_key,
            "filename": f"{result['field_name'] or 'field'}_选地分析报告.pdf",
            "score": result["score"],
            "grade": result["grade"],
            "light": result["light"],
            "one_liner": result.get("one_liner"),
            "area_mu": result.get("area_mu"),
            "indices_source": result.get("indices_source"),
            "content_type": "application/pdf",
        }
        _update_job(session, job, "succeeded", progress=progress)
        logger.info(
            "assessment_report_done",
            job_id=job_id,
            field_id=str(job.field_id),
            object_key=object_key,
            score=result["score"],
        )
        return progress
    except Exception as exc:
        logger.exception("assessment_report_failed", job_id=job_id, error=str(exc))
        try:
            job = session.get(Job, uuid.UUID(job_id))
            if job:
                _update_job(session, job, "failed", error=str(exc)[:2000])
        except Exception:
            pass
        raise
    finally:
        session.close()
