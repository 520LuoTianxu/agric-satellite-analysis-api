# -*- coding: utf-8 -*-
"""Land assessment (选地体检) PDF report endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated
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
from app.schemas.monitoring import JobOut
from pydantic import BaseModel, Field as PydanticField


class AssessmentGenerateRequest(BaseModel):
    """Optional crop bind when legacy fields lack crop_type."""

    crop_type: str | None = PydanticField(
        default=None,
        description="Catalog key from GET /v1/crops; binds to field if missing")


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
    status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def create_assessment_report(
    request: Request,
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: AssessmentGenerateRequest | None = None):
    """Enqueue a 选地体检（白话版）PDF generation job."""
    field = await _get_field(field_id, ctx.org_id, db)

    from app.core.crops import crop_name_zh, normalize_crop_key, require_crop_key

    crop_key = normalize_crop_key(field.crop_type)
    requested = (body.crop_type if body else None) or None
    if not crop_key and requested:
        try:
            crop_key = require_crop_key(requested)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        field.crop_type = crop_key
        await db.flush()
    if not crop_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "crop_required",
                "message": "请先选择作物后再生成选地报告",
                "crops_path": "/v1/crops",
            })

    # Reuse in-flight job if one is pending/running
    existing = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "assessment_report",
                Job.status.in_(("pending", "running")))
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    job = Job(

        field_id=field_id,
        type="assessment_report",
        status="pending",
        params_json={
            "kind": "land_assessment_plain",
            "crop_type": crop_key,
            "crop_name_zh": crop_name_zh(crop_key),
        })
    db.add(job)
    await db.flush()

    await db.commit()

    try:
        from app.mq_publish import publish_api_task

        mq_task_id = publish_api_task(
            type="assessment_report",
            field_id=str(field_id),
            extras={
                "job_id": str(job.id),
                "crop_type": crop_key,
                "crop_name_zh": crop_name_zh(crop_key),
            },
        )
        logger.info(
            "assessment_job_dispatched",
            job_id=str(job.id),
            field_id=str(field_id),
            mq_task_id=mq_task_id,
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
    db: Annotated[AsyncSession, Depends(get_db)]):
    """Download the latest succeeded assessment PDF for a field."""
    await _get_field(field_id, ctx.org_id, db)

    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "assessment_report",
                Job.status == "succeeded")
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
    return Response(
        content=data,
        media_type="application/pdf",
        headers=headers)


@router.get(
    "/fields/{field_id}/assessment-report/latest/meta",
    response_model=JobOut)
async def get_latest_assessment_meta(
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)]):
    """Return the latest assessment job (any status) for UI polling."""
    await _get_field(field_id, ctx.org_id, db)
    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "assessment_report")
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="No assessment report yet")
    return job
