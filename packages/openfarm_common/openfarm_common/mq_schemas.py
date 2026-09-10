"""CloudAMQP task/result message schemas (outer scheduling bus)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskMessage(BaseModel):
    """Inbound task published to CLOUDAMQP_TASK_QUEUE."""

    task_id: str
    type: str = "satellite_analysis"
    # OpenFarm field UUID (preferred when known)
    field_id: str | None = None
    # agri.land_parcels.land_id — user sample field_id often means this
    parcel_id: str | None = None
    land_id: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class ResultMessage(BaseModel):
    """Outbound result published to CLOUDAMQP_RESULT_QUEUE (OSS URLs only)."""

    task_id: str
    status: Literal["success", "failed"]
    oss_urls: dict[str, str] = Field(default_factory=dict)
    error: str | None = None
    field_id: str | None = None
    land_id: str | None = None
    finished_at: datetime = Field(default_factory=_utcnow)
    extras: dict[str, Any] = Field(default_factory=dict)
