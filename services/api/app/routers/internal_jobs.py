"""Internal jobs get/patch for download-host workers (D2)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import Job

router = APIRouter(prefix="/internal/jobs", tags=["internal-jobs"])


class InternalJobOut(BaseModel):
    id: uuid.UUID
    land_id: str | None = None
    type: str
    status: str
    progress_json: dict[str, Any] | None = None
    error: str | None = None
    params_json: dict[str, Any] | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class InternalJobPatch(BaseModel):
    status: str | None = Field(default=None, max_length=20)
    progress_json: dict[str, Any] | None = None
    error: str | None = None
    # When true, merge progress_json into existing rather than replace.
    merge_progress: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    touch_started: bool = False
    touch_finished: bool = False


def _to_out(job: Job) -> InternalJobOut:
    return InternalJobOut.model_validate(job)


@router.get("/{job_id}", response_model=InternalJobOut)
async def get_job(
    job_id: uuid.UUID,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _to_out(job)


@router.patch("/{job_id}", response_model=InternalJobOut)
async def patch_job(
    job_id: uuid.UUID,
    body: InternalJobPatch,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Update job status / progress (compat with ingest SyncSession writes).

    D2 callers may still write via DATABASE_URL; this endpoint exists for
    gradual cutover and for workers that already use API_BASE_URL.
    """
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    now = datetime.now(timezone.utc)
    if body.status is not None:
        job.status = body.status
        if body.status == "running" and job.started_at is None:
            job.started_at = now
        if body.status in ("succeeded", "failed", "cancelled", "completed"):
            if job.finished_at is None:
                job.finished_at = now

    if body.progress_json is not None:
        if body.merge_progress and isinstance(job.progress_json, dict):
            merged = dict(job.progress_json)
            merged.update(body.progress_json)
            # Deep-merge "steps" if both sides have it
            old_steps = job.progress_json.get("steps")
            new_steps = body.progress_json.get("steps")
            if isinstance(old_steps, dict) and isinstance(new_steps, dict):
                steps = dict(old_steps)
                for k, v in new_steps.items():
                    if isinstance(v, dict) and isinstance(steps.get(k), dict):
                        entry = dict(steps[k])
                        entry.update(v)
                        steps[k] = entry
                    else:
                        steps[k] = v
                merged["steps"] = steps
            job.progress_json = merged
        else:
            job.progress_json = body.progress_json
        flag_modified(job, "progress_json")

    if body.error is not None:
        job.error = body.error

    if body.started_at is not None:
        job.started_at = body.started_at
    elif body.touch_started and job.started_at is None:
        job.started_at = now

    if body.finished_at is not None:
        job.finished_at = body.finished_at
    elif body.touch_finished:
        job.finished_at = now

    await db.commit()
    await db.refresh(job)
    return _to_out(job)
