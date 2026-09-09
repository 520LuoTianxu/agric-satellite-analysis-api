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
from app.middleware.auth import OrgContext, get_org_context, require_roles
from app.models.tables import Field, Job
from app.schemas.monitoring import JobOut

router = APIRouter()
_writer = require_roles("owner", "admin", "member")


async def _get_field(field_id: uuid.UUID, org_id: uuid.UUID, db: AsyncSession) -> Field:
    field = await db.get(Field, field_id)
    if not field or field.org_id != org_id or field.deleted_at is not None:
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
):
    """Enqueue a 选地体检（白话版）PDF generation job."""
    await _get_field(field_id, ctx.org_id, db)

    # Reuse in-flight job if one is pending/running
    existing = (
        await db.execute(
            select(Job)
            .where(
                Job.org_id == ctx.org_id,
                Job.field_id == field_id,
                Job.type == "assessment_report",
                Job.status.in_(("pending", "running")),
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    job = Job(
        org_id=ctx.org_id,
        field_id=field_id,
        type="assessment_report",
        status="pending",
        params_json={"kind": "land_assessment_plain"},
        created_by=ctx.user.id,
    )
    db.add(job)
    await db.flush()

    try:
        from app.worker import celery_app

        celery_app.send_task(
            "app.tasks.assessment_report.generate_assessment_report",
            args=[str(job.id)],
        )
        logger.info(
            "assessment_job_dispatched",
            job_id=str(job.id),
            field_id=str(field_id),
        )
    except Exception as e:
        logger.error(
            "assessment_job_dispatch_failed",
            job_id=str(job.id),
            error=str(e),
        )
        job.status = "failed"
        job.error = f"dispatch failed: {e}"

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
                Job.org_id == ctx.org_id,
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
        raise HTTPException(status_code=404, detail="Report object not found in storage")

    data = storage.get_bytes(object_key)
    filename = progress.get("filename") or "选地分析报告.pdf"
    # RFC 5987 for Chinese filenames
    disp = f"attachment; filename=\"assessment.pdf\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": disp,
            "X-Assessment-Job-Id": str(job.id),
            "X-Assessment-Score": str(progress.get("score", "")),
        },
    )


@router.get(
    "/fields/{field_id}/assessment-report/latest/meta",
    response_model=JobOut,
)
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
                Job.org_id == ctx.org_id,
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
