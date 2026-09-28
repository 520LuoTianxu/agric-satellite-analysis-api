"""Thin CloudAMQP task enqueue API (smoke / external producers)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agric_satellite_analysis_common.task_priority import (
    INTERACTIVE_REPORT_PRIORITY,
    MANUAL_TASK_PRIORITY,
)
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
        "land_bootstrap",
        "assessment_report",
        "season_growth_report",
    }
)


class MqTaskEnqueue(BaseModel):
    type: str = "satellite_analysis"
    land_id: str
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
    from app.mq_publish import publish_api_task
    from agric_satellite_analysis_common.settings import settings as common_settings

    extras = body.extras or {}
    needs_satellite_batch = body.type == "satellite_analysis" or (
        body.type == "land_bootstrap" and not extras.get("skip_indices")
    )
    if needs_satellite_batch:
        # 兼容旧通用入口：指数回填统一转成 10km 共享窗口任务，再跳过逐地块扇出。
        from app.core.config import settings
        from app.services.smart_land_backfill import run_smart_land_backfill

        default_months = (
            24 if body.type == "satellite_analysis" else settings.index_backfill_months
        )
        try:
            date_to = date.fromisoformat(
                str(extras.get("date_to") or date.today())[:10]
            )
            date_from = (
                date.fromisoformat(str(extras["date_from"])[:10])
                if extras.get("date_from")
                else date_to
                - timedelta(
                    days=max(int(extras.get("months") or default_months), 1) * 30
                )
            )
            sensors = extras.get("sensors") or ["S1", "S2"]
            if isinstance(sensors, str):
                sensors = [sensors]
            sensors = list(dict.fromkeys(str(sensor).upper() for sensor in sensors))
            if not sensors or any(sensor not in {"S1", "S2"} for sensor in sensors):
                raise ValueError("sensors只允许S1或S2")
            if date_from > date_to:
                raise ValueError("date_from必须不晚于date_to")
            if (date_to - date_from).days > 3660:
                raise ValueError("回填时间范围最多10年")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            requested_parent_id = uuid.UUID(body.task_id) if body.task_id else uuid.uuid4()
        except ValueError:
            # 旧调用方可能传入非 UUID 消息 ID；遥感分组任务树使用独立 UUID 主任务。
            requested_parent_id = uuid.uuid4()

        from app.services.smart_land_backfill import LandSelectionError

        try:
            result = await run_smart_land_backfill(
                land_ids=[body.land_id],
                date_from=date_from,
                date_to=date_to,
                sensors=sensors,
                force=bool(extras.get("force", False)),
                parent_job_id=requested_parent_id,
            )
        except LandSelectionError as exc:
            detail: object = (
                {"missing_land_ids": exc.missing_land_ids}
                if exc.missing_land_ids
                else str(exc)
            )
            raise HTTPException(status_code=exc.status_code, detail=detail) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if body.type == "land_bootstrap":
            # bootstrap 保留天气/土壤及报告 follow-up，遥感由独立 10km Job 负责。
            task_id = body.task_id or str(uuid.uuid4())
            publish_api_task(
                type="land_bootstrap",
                land_id=body.land_id,
                task_id=task_id,
                priority=MANUAL_TASK_PRIORITY,
                extras={
                    **extras,
                    "skip_indices": True,
                    "satellite_batch_parent_job_id": result["parent_job_id"],
                    "satellite_job_ids": result["queued_job_ids"],
                    **({"org_id": str(ctx.org_id)} if ctx.org_id else {}),
                    "enqueued_by": str(ctx.user.id),
                },
            )
            return MqTaskEnqueued(
                task_id=task_id,
                type=body.type,
                queue=common_settings.cloudamqp_download_queue,
                created_at=datetime.now(timezone.utc),
            )

        # satellite_analysis 旧语义还会补齐相同日期范围的天气；遥感由上方 10km 子任务负责。
        if body.land_id in result["selected_land_ids"]:
            try:
                publish_api_task(
                    type="weather_backfill",
                    land_id=body.land_id,
                    task_id=str(uuid.uuid5(requested_parent_id, "weather-backfill")),
                    extras={
                        "date_from": date_from.isoformat(),
                        "date_to": date_to.isoformat(),
                        **({"org_id": str(ctx.org_id)} if ctx.org_id else {}),
                        "enqueued_by": str(ctx.user.id),
                    },
                )
            except Exception:
                logger.exception("mq_satellite_batch_weather_dispatch_failed", land_id=body.land_id)
        if extras.get("with_bridge") or extras.get("bridge_job_id"):
            try:
                publish_api_task(
                    type="agri_bridge",
                    land_id=body.land_id,
                    task_id=str(uuid.uuid5(requested_parent_id, "agri-bridge")),
                    extras={
                        **(
                            {"bridge_job_id": str(extras["bridge_job_id"])}
                            if extras.get("bridge_job_id")
                            else {}
                        ),
                        "dispatch_alerts": bool(extras.get("dispatch_alerts")),
                        **({"org_id": str(ctx.org_id)} if ctx.org_id else {}),
                        "enqueued_by": str(ctx.user.id),
                    },
                )
            except Exception:
                logger.exception("mq_satellite_batch_bridge_dispatch_failed", land_id=body.land_id)
        return MqTaskEnqueued(
            task_id=str(requested_parent_id),
            type=body.type,
            queue=common_settings.cloudamqp_download_queue,
            created_at=datetime.now(timezone.utc),
        )

    task_id = body.task_id or str(uuid.uuid4())
    priority = (
        INTERACTIVE_REPORT_PRIORITY
        if body.type in {"assessment_report", "season_growth_report"}
        else MANUAL_TASK_PRIORITY
    )
    publish_api_task(
        type=body.type,
        land_id=body.land_id,
        task_id=task_id,
        priority=priority,
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
        land_id=body.land_id,
    )
    return MqTaskEnqueued(
        task_id=task_id,
        type=body.type,
        queue=common_settings.cloudamqp_download_queue,
        created_at=datetime.now(timezone.utc),
    )
