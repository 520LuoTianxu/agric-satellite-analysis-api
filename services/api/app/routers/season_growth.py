# -*- coding: utf-8 -*-
"""Season growth (生育期长势) PDF report endpoints."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field as PydanticField, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.logging import logger
from app.core.rate_limit import limiter
from app.core.storage import get_storage
from app.middleware.auth import OrgContext, get_org_context, require_roles, org_scope
from app.models.tables import Field, Job
from app.schemas.monitoring import JobOut


class SeasonGrowthGenerateRequest(BaseModel):
    start_date: str = PydanticField(..., description="YYYY-MM-DD")
    end_date: str = PydanticField(..., description="YYYY-MM-DD")
    crops: list[str] = PydanticField(default_factory=list)
    label: str | None = None
    material_keys: list[str] = PydanticField(default_factory=list)

    @field_validator("start_date", "end_date")
    @classmethod
    def _valid_iso(cls, v: str) -> str:
        try:
            date.fromisoformat(str(v)[:10])
        except ValueError as e:
            raise ValueError("date must be YYYY-MM-DD") from e
        return str(v)[:10]


class MaterialUploadOut(BaseModel):
    key: str
    url: str | None = None
    filename: str | None = None
    bytes: int | None = None


router = APIRouter()
_writer = require_roles("owner", "admin", "member")


async def _get_field(field_id: uuid.UUID, org_id: uuid.UUID, db: AsyncSession) -> Field:
    field = await db.get(Field, field_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Field not found")
    return field


def _window_key(
    start: str, end: str, crops: list[str] | None, label: str | None
) -> str:
    crops_s = ",".join(sorted(crops or []))
    return f"{start}|{end}|{crops_s}|{label or ''}"


@router.post(
    "/fields/{field_id}/season-growth-report",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("5/minute")
async def create_season_growth_report(
    request: Request,
    field_id: uuid.UUID,
    body: SeasonGrowthGenerateRequest,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Enqueue a 生育期长势 PDF generation job."""
    await _get_field(field_id, ctx.org_id, db)

    if date.fromisoformat(body.end_date) < date.fromisoformat(body.start_date):
        raise HTTPException(status_code=422, detail="end_date must be >= start_date")

    # Reuse in-flight pending/running job for same field (like assessment)
    existing = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "season_growth_report",
                Job.status.in_(("pending", "running")),
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    params: dict[str, Any] = {
        "kind": "season_growth",
        "start_date": body.start_date,
        "end_date": body.end_date,
        "crops": list(body.crops or []),
        "label": body.label,
        "material_keys": list(body.material_keys or []),
        "window_key": _window_key(
            body.start_date, body.end_date, body.crops, body.label
        ),
    }
    job = Job(
        field_id=field_id,
        type="season_growth_report",
        status="pending",
        params_json=params,
    )
    db.add(job)
    await db.flush()
    await db.commit()

    try:
        from app.mq_publish import publish_api_task

        mq_task_id = publish_api_task(
            type="season_growth_report",
            field_id=str(field_id),
            extras={
                "job_id": str(job.id),
                "start_date": body.start_date,
                "end_date": body.end_date,
                "crops": list(body.crops or []),
                "label": body.label,
                "material_keys": list(body.material_keys or []),
            },
        )
        logger.info(
            "season_growth_job_dispatched",
            job_id=str(job.id),
            field_id=str(field_id),
            mq_task_id=mq_task_id,
        )
    except Exception as e:
        logger.error(
            "season_growth_job_dispatch_failed",
            job_id=str(job.id),
            error=str(e),
        )
        job = await db.get(Job, job.id) or job
        job.status = "failed"
        job.error = f"dispatch failed: {e}"
        await db.commit()

    return job


@router.post(
    "/fields/{field_id}/season-growth-report/materials",
    response_model=MaterialUploadOut,
)
@limiter.limit("20/minute")
async def upload_season_growth_material(
    request: Request,
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
):
    """Upload an optional material file; returns storage key for generate body."""
    await _get_field(field_id, ctx.org_id, db)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="file too large (max 20MB)")

    safe_name = (file.filename or "material.bin").replace("/", "_").replace("\\", "_")
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    object_key = f"reports/season_growth/{field_id}/{ts}-{safe_name}"
    storage = get_storage()
    content_type = file.content_type or "application/octet-stream"
    storage.put_bytes(object_key, raw, content_type=content_type)

    url = None
    try:
        url = storage.public_url(object_key)
    except Exception:
        url = None
    return MaterialUploadOut(
        key=object_key, url=url, filename=safe_name, bytes=len(raw)
    )


@router.get("/fields/{field_id}/season-growth-report/latest")
async def get_latest_season_growth_report(
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Download the latest succeeded season-growth PDF for a field."""
    await _get_field(field_id, ctx.org_id, db)

    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "season_growth_report",
                Job.status == "succeeded",
            )
            .order_by(Job.finished_at.desc().nullslast(), Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="No season growth report yet")

    progress = job.progress_json or {}
    object_key = progress.get("object_key")
    if not object_key:
        raise HTTPException(status_code=404, detail="Report file missing")

    storage = get_storage()
    if not storage.exists(object_key):
        raise HTTPException(
            status_code=404, detail="Report object not found in storage"
        )

    data = storage.get_bytes(object_key)
    filename = progress.get("filename") or "生育期长势分析报告.pdf"
    disp = (
        f'attachment; filename="season-growth.pdf"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    headers = {
        "Content-Disposition": disp,
        "X-Season-Growth-Job-Id": str(job.id),
    }
    public_url = progress.get("public_url")
    if public_url:
        headers["X-Season-Growth-Public-Url"] = str(public_url)
    return Response(content=data, media_type="application/pdf", headers=headers)


@router.get(
    "/fields/{field_id}/season-growth-report/latest/meta",
    response_model=JobOut,
)
async def get_latest_season_growth_meta(
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the latest season-growth job (any status) for UI polling."""
    await _get_field(field_id, ctx.org_id, db)
    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.field_id == field_id,
                Job.type == "season_growth_report",
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="No season growth report yet")
    return job
