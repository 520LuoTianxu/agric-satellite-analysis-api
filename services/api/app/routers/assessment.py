# -*- coding: utf-8 -*-
"""Land assessment (选地体检) PDF report endpoints."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.logging import logger
from app.core.rate_limit import limiter
from app.core.storage import get_storage
from app.middleware.auth import OrgContext, get_org_context, require_roles, org_scope
from app.models.tables import Field, Job
from app.reports.land_assessment.scorecard_view import scorecard_public_view
from app.reports.land_assessment.window import resolve_assessment_window
from app.schemas.monitoring import JobOut
from pydantic import BaseModel, Field as PydanticField, field_validator, model_validator


class AssessmentGenerateRequest(BaseModel):
    """Crop bind + optional historical window + one-click data pull."""

    crop_type: str | None = PydanticField(
        default=None,
        description="Catalog key from GET /v1/crops; binds to field if provided/missing",
    )
    date_from: str | None = PydanticField(
        default=None,
        description="YYYY-MM-DD start of historical window; date_to is always today",
    )
    years: int | None = PydanticField(
        default=None,
        ge=1,
        le=20,
        description="If date_from omitted, start = today − years (default 3)",
    )
    pull_data: bool = PydanticField(
        default=True,
        description="Queue weather + RS indices + soil bootstrap before PDF",
    )

    @field_validator("date_from", mode="before")
    @classmethod
    def _empty_date_from(cls, v: Any) -> Any:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @model_validator(mode="after")
    def _validate_date_from_iso(self) -> "AssessmentGenerateRequest":
        if self.date_from:
            try:
                date.fromisoformat(self.date_from[:10])
            except ValueError as e:
                raise ValueError("date_from must be YYYY-MM-DD") from e
            self.date_from = self.date_from[:10]
        return self


class AssessmentDimensionOut(BaseModel):
    key: str
    score: float
    light: str | None = None
    weight: str | None = None


class AssessmentOverallOut(BaseModel):
    score: float
    grade: str | None = None
    light: str | None = None
    one_liner: str | None = None


class AssessmentConfidenceOut(BaseModel):
    score: float


class AssessmentScorecardOut(BaseModel):
    """Six-dimension land-assessment scorecard from a succeeded report job."""

    job_id: uuid.UUID
    overall: AssessmentOverallOut
    dimensions: list[AssessmentDimensionOut]
    confidence: AssessmentConfidenceOut | None = None
    generated_at: datetime | None = None


router = APIRouter()
_writer = require_roles("owner", "admin", "member")


async def _get_field(field_id: uuid.UUID, org_id: uuid.UUID, db: AsyncSession) -> Field:
    field = await db.get(Field, field_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Field not found")
    return field


@router.post(
    "/fields/{field_id}/assessment-report",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("5/minute")
async def create_assessment_report(
    request: Request,
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: AssessmentGenerateRequest | None = None,
):
    """Enqueue data pulls (optional) + 选地体检（白话版）PDF generation.

    Never refuses generation merely because weather / soil / RS are incomplete;
    missing series are scored with available data inside the PDF worker.
    """
    field = await _get_field(field_id, ctx.org_id, db)

    from app.core.crops import crop_name_zh, normalize_crop_key, require_crop_key

    req = body or AssessmentGenerateRequest()
    crop_key = normalize_crop_key(field.crop_type)
    requested = req.crop_type or None
    crop_changed = False
    if requested:
        try:
            new_key = require_crop_key(requested)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        if crop_key != new_key:
            field.crop_type = new_key
            crop_changed = True
            await db.flush()
        crop_key = new_key
    if not crop_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "crop_required",
                "message": "请先选择作物后再生成选地报告",
                "crops_path": "/v1/crops",
            },
        )

    try:
        date_from, date_to, weather_days, years_used = resolve_assessment_window(
            date_from=req.date_from,
            years=req.years,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    pull_data = bool(req.pull_data)

    # Reuse in-flight job if one is pending/running
    existing = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "assessment_report",
                Job.status.in_(("pending", "running")),
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        # Persist crop bind even when PDF job is already in flight
        if crop_changed:
            await db.commit()
        return existing

    job = Job(
        field_id=field_id,
        type="assessment_report",
        status="pending",
        params_json={
            "kind": "land_assessment_plain",
            "crop_type": crop_key,
            "crop_name_zh": crop_name_zh(crop_key),
            "date_from": date_from,
            "date_to": date_to,
            "years": years_used,
            "pull_data": pull_data,
            "weather_days": weather_days,
        },
    )
    db.add(job)
    await db.flush()

    await db.commit()

    try:
        from app.mq_publish import publish_api_task

        if pull_data:
            # Do NOT publish assessment_report in parallel — that raced PDF ahead of
            # weather/RS (soil-only reports). Bootstrap fans out pulls with allow_agri
            # and enqueues assessment as followup after Celery ids are known.
            assessment_mq_task_id = str(uuid.uuid4())
            bootstrap_extras: dict[str, Any] = {
                "date_from": date_from,
                "date_to": date_to,
                "days": weather_days,
                "weather_days": weather_days,
                "years": years_used,
                "source": "assessment_one_click",
                "allow_agri": True,
                "with_bridge": True,
                "followup_assessment": {
                    "job_id": str(job.id),
                    "mq_task_id": assessment_mq_task_id,
                    "crop_type": crop_key,
                    "crop_name_zh": crop_name_zh(crop_key),
                    "date_from": date_from,
                    "date_to": date_to,
                    "years": years_used,
                },
            }
            bootstrap_task_id = publish_api_task(
                type="field_bootstrap",
                field_id=str(field_id),
                extras=bootstrap_extras,
            )
            logger.info(
                "assessment_bootstrap_dispatched",
                job_id=str(job.id),
                field_id=str(field_id),
                mq_task_id=bootstrap_task_id,
                assessment_mq_task_id=assessment_mq_task_id,
                date_from=date_from,
                date_to=date_to,
                weather_days=weather_days,
                pull_data=True,
            )
        else:
            mq_task_id = publish_api_task(
                type="assessment_report",
                field_id=str(field_id),
                extras={
                    "job_id": str(job.id),
                    "crop_type": crop_key,
                    "crop_name_zh": crop_name_zh(crop_key),
                    "date_from": date_from,
                    "date_to": date_to,
                    "years": years_used,
                    "pull_data": False,
                },
            )
            logger.info(
                "assessment_job_dispatched",
                job_id=str(job.id),
                field_id=str(field_id),
                mq_task_id=mq_task_id,
                date_from=date_from,
                date_to=date_to,
                pull_data=False,
            )
    except Exception as e:
        logger.error(
            "assessment_job_dispatch_failed",
            job_id=str(job.id),
            error=str(e),
        )
        # Re-open session state after commit for failure marking
        job = await db.get(Job, job.id) or job
        job.status = "failed"
        job.error = f"dispatch failed: {e}"
        await db.commit()

    return job


@router.get("/fields/{field_id}/assessment-report/latest")
async def get_latest_assessment_report(
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Download the latest succeeded assessment PDF for a field."""
    await _get_field(field_id, ctx.org_id, db)

    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "assessment_report",
                Job.status == "succeeded",
            )
            .order_by(Job.finished_at.desc().nullslast(), Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="No assessment report yet")

    progress = job.progress_json or {}
    object_key = progress.get("object_key")
    if not object_key:
        raise HTTPException(status_code=404, detail="Report file missing")

    storage = get_storage()
    if not storage.exists(object_key):
        raise HTTPException(
            status_code=404, detail="Report object not found in storage"
        )

    # Prefer streaming from storage; public_url (OSS) is also in progress for
    # process-host / frontend download without proxying through API.
    data = storage.get_bytes(object_key)
    filename = progress.get("filename") or "选地分析报告.pdf"
    # RFC 5987 for Chinese filenames
    disp = (
        f"attachment; filename=\"assessment.pdf\"; filename*=UTF-8''{quote(filename)}"
    )
    headers = {
        "Content-Disposition": disp,
        "X-Assessment-Job-Id": str(job.id),
        "X-Assessment-Score": str(progress.get("score", "")),
    }
    public_url = progress.get("public_url")
    if public_url:
        headers["X-Assessment-Public-Url"] = str(public_url)
    return Response(content=data, media_type="application/pdf", headers=headers)


@router.get("/fields/{field_id}/assessment-report/latest/meta", response_model=JobOut)
async def get_latest_assessment_meta(
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the latest assessment job (any status) for UI polling."""
    await _get_field(field_id, ctx.org_id, db)
    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "assessment_report",
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="No assessment report yet")
    return job


def _scorecard_from_job(job: Job) -> dict[str, Any] | None:
    progress = job.progress_json or {}
    raw = progress.get("scorecard")
    if not isinstance(raw, dict):
        return None
    return scorecard_public_view(raw)


@router.get(
    "/fields/{field_id}/assessment-report/latest/scorecard",
    response_model=AssessmentScorecardOut,
)
async def get_latest_assessment_scorecard(
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the six-dimension scorecard from the latest succeeded report.

    PDF download is unchanged. Jobs that finished before scorecards were
    persisted return ``scorecard_unavailable`` so the UI can ask the user
    to regenerate rather than inventing numbers.
    """
    await _get_field(field_id, ctx.org_id, db)
    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "assessment_report",
                Job.status == "succeeded",
            )
            .order_by(Job.finished_at.desc().nullslast(), Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_assessment_report",
                "message": "No assessment report yet",
            },
        )
    view = _scorecard_from_job(job)
    if not view:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "scorecard_unavailable",
                "message": "Latest report has no stored scorecard; generate a new report",
                "job_id": str(job.id),
            },
        )
    return AssessmentScorecardOut(
        job_id=job.id,
        overall=AssessmentOverallOut(**view["overall"]),
        dimensions=[AssessmentDimensionOut(**d) for d in view["dimensions"]],
        confidence=(
            AssessmentConfidenceOut(**view["confidence"])
            if view.get("confidence")
            else None
        ),
        generated_at=job.finished_at or job.created_at,
    )
