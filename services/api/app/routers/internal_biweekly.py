"""Internal endpoints so the download machine can load inputs and return PDFs."""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from agric_satellite_analysis_common.biweekly.models import ReportError

from app.core.logging import logger
from app.middleware.internal_auth import InternalAuth
from app.services.biweekly_report_flow import _enrich_payload, _roll_up_season
from app.services.biweekly_trigger import build_trigger_service

router = APIRouter(prefix="/internal/biweekly", tags=["internal-biweekly"])


class CompleteRequest(BaseModel):
    status: str
    error_code: str | None = None
    note: str | None = None
    filename: str | None = None
    mime: str | None = None
    kind: str | None = None
    content_base64: str | None = None
    facts: dict[str, Any] | None = None
    report_job_id: str | None = None


def _service():
    return build_trigger_service(use_official_pdf=False)


@router.get("/jobs/{job_id}/inputs")
async def biweekly_report_inputs(job_id: str, _: InternalAuth) -> dict[str, Any]:
    """Return scene, weather, and plan inputs for official v8 PDF generation."""
    service = _service()
    try:
        job = service.get_job(job_id)
    except ReportError as exc:
        raise HTTPException(status_code=404, detail={"error": exc.code}) from exc
    if job.get("kind") != "generate":
        raise HTTPException(status_code=409, detail={"error": "JOB_KIND_UNSUPPORTED"})
    land_id = job["land_id"]
    season_id = job["season_id"]
    period_id = job["period_id"]
    period = service.catalog.period(land_id, season_id, period_id)
    periods = service.catalog.periods(land_id, season_id)
    try:
        fields = await _enrich_payload(land_id, period, periods)
    except ReportError as exc:
        note = "读取已入库景失败，不能当作本期没有可用光学。"
        return {
            "job_id": job_id,
            "land_id": land_id,
            "season_id": season_id,
            "period_id": period_id,
            "error_code": exc.code,
            "note": note if exc.code == "SCENE_QUERY_FAILED" else str(exc.code),
        }
    payload = dict(job.get("payload") or {})
    if not payload.get("native_plan") and hasattr(service.catalog, "native_plan"):
        fields.setdefault("native_plan", service.catalog.native_plan(land_id, season_id))
    service.set_payload_fields(job_id, **fields)
    job = service.get_job(job_id)
    facts, status = service.prepare_generate_facts(job)
    return {
        "job_id": job_id,
        "land_id": land_id,
        "season_id": season_id,
        "period_id": period_id,
        "error_code": None,
        "status": status,
        "facts": facts,
        "reference_planting_instance_id": (job.get("payload") or {}).get(
            "reference_planting_instance_id"
        ),
    }


@router.post("/jobs/{job_id}/complete")
async def biweekly_report_complete(
    job_id: str,
    body: CompleteRequest,
    _: InternalAuth,
) -> dict[str, Any]:
    """Attach a download-machine artifact to the file-backed generate job."""
    service = _service()
    try:
        job = service.get_job(job_id)
    except ReportError as exc:
        raise HTTPException(status_code=404, detail={"error": exc.code}) from exc
    if job.get("kind") != "generate":
        raise HTTPException(status_code=409, detail={"error": "JOB_KIND_UNSUPPORTED"})
    status = body.status
    if status == "failed":
        job = service.fail_stage(
            job_id,
            body.error_code or "GENERATE_FAILED",
            note=body.note,
        )
    else:
        if not body.content_base64:
            raise HTTPException(status_code=400, detail={"error": "ARTIFACT_MISSING"})
        try:
            content = base64.b64decode(body.content_base64)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail={"error": "ARTIFACT_CORRUPT"}) from exc
        if status == "ready":
            kind = body.kind or "official_visual_artifact"
            mime = body.mime or "application/pdf"
            filename = body.filename or f"种植双周报_{job.get('land_id')}.pdf"
        else:
            kind = body.kind or "evidence_limited_brief"
            mime = body.mime or "application/json"
            filename = body.filename or "evidence-limited.json"
        try:
            job = service.attach_remote_artifact(
                job_id,
                status=status,
                content=content,
                filename=filename,
                mime=mime,
                kind=kind,
                facts=body.facts,
                error_code=body.error_code,
                note=body.note,
            )
        except ReportError as exc:
            raise HTTPException(status_code=400, detail={"error": exc.code}) from exc
    parent_id = (job.get("payload") or {}).get("parent_season_job_id")
    if parent_id:
        try:
            await _roll_up_season(service, service.get_job(parent_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "biweekly_season_rollup_failed job_id=%s err_type=%s",
                job_id,
                type(exc).__name__,
            )
    if body.report_job_id:
        try:
            from app.core.database import async_session
            from app.models.tables import Job
            from uuid import UUID

            async with async_session() as db:
                row = await db.get(Job, UUID(str(body.report_job_id)))
                if row is not None:
                    row.status = (
                        "completed"
                        if status in {"ready", "evidence_limited"}
                        else "failed"
                    )
                    row.progress_json = {
                        **(row.progress_json or {}),
                        "biweekly_job_id": job_id,
                        "status": status,
                        "error_code": body.error_code,
                        "artifact_kind": (job.get("artifact") or {}).get("kind"),
                    }
                    if status not in {"ready", "evidence_limited"}:
                        row.error = body.error_code or body.note
                    await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "biweekly_report_job_patch_failed job_id=%s err_type=%s",
                body.report_job_id,
                type(exc).__name__,
            )
    return {"ok": True, "job": {"job_id": job["job_id"], "status": job["status"], "error_code": job.get("error_code"), "artifact": job.get("artifact")}}
