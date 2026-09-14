"""Internal HTTP claim API for download-host work_items control plane."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import WorkItem
from app.services import work_items as wi

router = APIRouter(prefix="/internal/work", tags=["internal-work"])


class ClaimRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=256)
    types: list[str] | None = None
    limit: int = Field(default=1, ge=1, le=50)
    lease_seconds: int | None = Field(default=None, ge=30, le=3600)


class WorkItemOut(BaseModel):
    id: uuid.UUID
    type: str
    payload_json: dict[str, Any]
    status: str
    priority: int
    lease_owner: str | None = None
    lease_until: datetime | None = None
    attempts: int
    idempotency_key: str | None = None
    progress_json: dict[str, Any] | None = None
    result_json: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class ClaimResponse(BaseModel):
    items: list[WorkItemOut]


class HeartbeatRequest(BaseModel):
    worker_id: str | None = None
    lease_seconds: int | None = Field(default=None, ge=30, le=3600)


class ProgressRequest(BaseModel):
    worker_id: str | None = None
    progress: dict[str, Any] = Field(default_factory=dict)


class CompleteRequest(BaseModel):
    worker_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class FailRequest(BaseModel):
    worker_id: str | None = None
    error: str = Field(..., min_length=1)
    retry: bool = False


def _to_out(item: WorkItem) -> WorkItemOut:
    return WorkItemOut.model_validate(item)


@router.post("/claim", response_model=ClaimResponse)
async def claim_work(
    body: ClaimRequest,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    items = await wi.claim_work_items(
        db,
        worker_id=body.worker_id,
        types=body.types,
        limit=body.limit,
        lease_seconds=body.lease_seconds,
    )
    await db.commit()
    return ClaimResponse(items=[_to_out(i) for i in items])


@router.post("/{work_id}/heartbeat", response_model=WorkItemOut)
async def heartbeat(
    work_id: uuid.UUID,
    body: HeartbeatRequest,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        item = await wi.heartbeat_work_item(
            db,
            work_id,
            worker_id=body.worker_id,
            lease_seconds=body.lease_seconds or settings.work_lease_seconds,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="work item not found") from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await db.commit()
    return _to_out(item)


@router.post("/{work_id}/progress", response_model=WorkItemOut)
async def progress(
    work_id: uuid.UUID,
    body: ProgressRequest,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        item = await wi.progress_work_item(
            db,
            work_id,
            progress=body.progress,
            worker_id=body.worker_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="work item not found") from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await db.commit()
    return _to_out(item)


@router.post("/{work_id}/complete", response_model=WorkItemOut)
async def complete(
    work_id: uuid.UUID,
    body: CompleteRequest,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        item = await wi.complete_work_item(
            db,
            work_id,
            result=body.result,
            worker_id=body.worker_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="work item not found") from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await db.commit()
    return _to_out(item)


@router.post("/{work_id}/fail", response_model=WorkItemOut)
async def fail(
    work_id: uuid.UUID,
    body: FailRequest,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        item = await wi.fail_work_item(
            db,
            work_id,
            error=body.error,
            worker_id=body.worker_id,
            retry=body.retry,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="work item not found") from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await db.commit()
    return _to_out(item)
