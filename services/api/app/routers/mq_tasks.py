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

ALLOWED_MQ_TYPES = frozenset(
    {
        "satellite_analysis",
        "agri_bridge",
        "weather_backfill",
        "soil_fetch",
        "field_bootstrap",
        "assessment_report",
        "season_growth_report",
    }
)


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
    body: MqTaskEnqueue, ctx: Annotated[OrgContext, Depends(_writer)]
):
    """Publish a TaskMessage to CloudAMQP download queue (does not run work inline)."""
    if body.type not in ALLOWED_MQ_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported type. Allowed: {', '.join(sorted(ALLOWED_MQ_TYPES))}",
        )
    if not body.field_id and not body.parcel_id and not body.land_id:
        raise HTTPException(
            status_code=400, detail="Provide field_id and/or parcel_id (land_id)"
        )

    from app.mq_publish import publish_api_task
    from openfarm_common.settings import settings as common_settings

    task_id = body.task_id or str(uuid.uuid4())
    publish_api_task(
        type=body.type,
        field_id=body.field_id,
        parcel_id=body.parcel_id,
        land_id=body.land_id,
        task_id=task_id,
        extras={
            **(body.extras or {}),
            **({"org_id": str(ctx.org_id)} if ctx.org_id else {}),
            "enqueued_by": str(ctx.user.id),
        },
    )

    logger.info(
        "mq_task_enqueued",
        task_id=task_id,
        type=body.type,
        field_id=body.field_id,
        parcel_id=body.parcel_id,
    )
    return MqTaskEnqueued(
        task_id=task_id,
        type=body.type,
        queue=common_settings.cloudamqp_download_queue,
        created_at=datetime.now(timezone.utc),
    )
