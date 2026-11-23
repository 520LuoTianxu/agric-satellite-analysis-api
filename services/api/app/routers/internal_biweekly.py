"""Internal endpoints so the download machine can load inputs and return PDFs."""

from __future__ import annotations

import asyncio
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


class OssArtifactIn(BaseModel):
    bucket: str
    key: str
    size_bytes: int
    sha256: str
    content_type: str | None = None


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
    oss: OssArtifactIn | None = None
    oss_bucket: str | None = None
    oss_key: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    content_type: str | None = None


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
    try:
        # Official inputs run without SmartPlantingCatalog; use the window
        # already stored on the generate job. Do not invent a calendar.
        period = service.period_for_job(job)
        periods = service.periods_for_job(job)
        fields = await _enrich_payload(land_id, period, periods, job_id=job_id)
    except ReportError as exc:
        note = "读取已入库景失败，不能当作本期没有可用光学。"
        if exc.code == "SCENE_QUERY_FAILED":
            body_note = note
        elif exc.code in {"PERIOD_NOT_FOUND", "PERIOD_WINDOW_INVALID"}:
            body_note = "任务已存在，但不能从日历或缺窗口还原本期区间，不能伪造种植日历。"
        else:
            body_note = str(exc.code)
        return {
            "job_id": job_id,
            "land_id": land_id,
            "season_id": season_id,
            "period_id": period_id,
            "error_code": exc.code,
            "note": body_note,
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
    """Register a pre-uploaded OSS object, or accept legacy base64 from old machines."""
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
        if status == "ready":
            kind = body.kind or "official_visual_artifact"
            mime = body.mime or body.content_type or "application/pdf"
            filename = body.filename or f"种植双周报_{job.get('land_id')}.pdf"
        elif status == "registration_pending":
            kind = body.kind or "official_visual_artifact"
            mime = body.mime or body.content_type or "application/pdf"
            filename = body.filename or f"种植双周报_{job.get('land_id')}.pdf"
        else:
            kind = body.kind or "evidence_limited_brief"
            mime = body.mime or body.content_type or "application/json"
            filename = body.filename or "evidence-limited.json"
        from agric_satellite_analysis_common.biweekly.official_ref import parse_oss_ref
        from agric_satellite_analysis_common.biweekly.models import ReportError as _RE

        try:
            oss_ref = parse_oss_ref(
                body.model_dump(),
                land_id=str(job.get("land_id") or ""),
                job_id=job_id,
                kind=kind,
                expected_bucket=service.expected_oss_bucket or "",
            )
        except _RE as exc:
            raise HTTPException(status_code=400, detail={"error": exc.code}) from exc
        try:
            if oss_ref is not None:
                oss_ref["filename"] = filename
                oss_ref["mime"] = mime
                oss_ref["content_type"] = oss_ref.get("content_type") or mime
                # Full-object checksum is API-to-OSS I/O. Run it off the
                # event loop so a 9MB hash cannot stall other requests the
                # way a huge content_base64 callback used to.
                inspect = status != "registration_pending"

                def _attach():
                    return service.attach_oss_artifact(
                        job_id,
                        status=status,
                        artifact=oss_ref,
                        facts=body.facts,
                        error_code=body.error_code,
                        note=body.note,
                        inspect=inspect,
                    )

                job = await asyncio.to_thread(_attach)
            elif body.content_base64:
                try:
                    content = base64.b64decode(body.content_base64)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=400, detail={"error": "ARTIFACT_CORRUPT"}) from exc
                def _attach_legacy():
                    return service.attach_remote_artifact(
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

                job = await asyncio.to_thread(_attach_legacy)
            else:
                raise HTTPException(status_code=400, detail={"error": "ARTIFACT_MISSING"})
        except HTTPException:
            raise
        except ReportError as exc:
            code = 409 if exc.code in {"ARTIFACT_CONFLICT", "ARTIFACT_CORRUPT", "ARTIFACT_SIZE_MISMATCH", "ARTIFACT_UNAVAILABLE"} else 400
            if exc.code == "OFFICIAL_STORE_UNAVAILABLE":
                code = 503
            raise HTTPException(status_code=code, detail={"error": exc.code}) from exc
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
                    official_status = job.get("status")
                    artifact = job.get("artifact") or {}
                    if official_status in {"ready", "evidence_limited"}:
                        row.status = "completed"
                        row.error = None
                    elif official_status == "failed":
                        row.status = "failed"
                        row.error = body.error_code or body.note or job.get("error_code")
                    # else: official ready was kept (stale fail ignored) or still pending; do not downgrade.
                    row.progress_json = {
                        **(row.progress_json or {}),
                        "biweekly_job_id": job_id,
                        "status": official_status,
                        "error_code": job.get("error_code") or body.error_code,
                        "artifact_kind": artifact.get("kind"),
                        "oss_bucket": artifact.get("bucket"),
                        "oss_key": artifact.get("key"),
                        "oss_sha256": artifact.get("sha256"),
                        "oss_size_bytes": artifact.get("size_bytes"),
                    }
                    await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "biweekly_report_job_patch_failed job_id=%s err_type=%s",
                body.report_job_id,
                type(exc).__name__,
            )
    return {"ok": True, "job": {"job_id": job["job_id"], "status": job["status"], "error_code": job.get("error_code"), "artifact": job.get("artifact")}}
