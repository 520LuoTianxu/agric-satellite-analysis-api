"""Thin CloudAMQP task enqueue API (smoke / external producers)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.middleware.auth import OrgContext, require_roles
from app.core.logging import logger

router = APIRouter()
_writer = require_roles("owner", "admin", "member")


class MqTaskEnqueue(BaseModel):
    type: str = "satellite_analysis"
    field_id: str | None = None
    parcel_id: str | None = None
    land_id: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None


class MqTaskEnqueued(BaseModel):
    task_id: str
    type: str
    queue: str
    created_at: datetime


@router.post("/mq/tasks", response_model=MqTaskEnqueued, status_code=202)
async def enqueue_mq_task(
    body: MqTaskEnqueue,
    ctx: Annotated[OrgContext, Depends(_writer)],
):
    """Publish a TaskMessage to CloudAMQP task queue (does not run work inline)."""
    if not body.field_id and not body.parcel_id and not body.land_id:
        raise HTTPException(
            status_code=400,
            detail="Provide field_id and/or parcel_id (land_id)",
        )
    try:
        from openfarm_common.mq import publish_task
        from openfarm_common.mq_schemas import TaskMessage
        from openfarm_common.settings import settings as common_settings
    except Exception as e:
        raise HTTPException(
            status_code=503, detail=f"MQ helpers unavailable: {e}"
        ) from e

    if not common_settings.cloudamqp_url:
        raise HTTPException(status_code=503, detail="CLOUDAMQP_URL not configured")

    task_id = body.task_id or str(uuid.uuid4())
    msg = TaskMessage(
        task_id=task_id,
        type=body.type,
        field_id=body.field_id,
        parcel_id=body.parcel_id,
        land_id=body.land_id,
        extras={
            **(body.extras or {}),
            "org_id": str(ctx.org_id),
            "enqueued_by": str(ctx.user.id),
        },
    )
    try:
        publish_task(msg)
    except Exception as e:
        logger.error("mq_enqueue_failed", task_id=task_id, error=str(e))
        raise HTTPException(
            status_code=502, detail=f"Failed to publish task: {e}"
        ) from e

    logger.info(
        "mq_task_enqueued",
        task_id=task_id,
        type=body.type,
        field_id=body.field_id,
        parcel_id=body.parcel_id,
        org_id=str(ctx.org_id),
    )
    return MqTaskEnqueued(
        task_id=task_id,
        type=body.type,
        queue=common_settings.cloudamqp_task_queue,
        created_at=datetime.now(timezone.utc),
    )
