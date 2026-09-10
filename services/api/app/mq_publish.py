"""Publish CloudAMQP TaskMessage from API routers.

Fail closed when CLOUDAMQP_URL is missing unless MQ_FALLBACK_CELERY=1
(legacy direct Celery for local/dev only).
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import HTTPException

from app.core.logging import logger


def _mq_fallback_enabled() -> bool:
    return os.getenv("MQ_FALLBACK_CELERY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _fallback_celery(
    *,
    type: str,
    field_id: str | None,
    land_id: str | None,
    extras: dict[str, Any],
) -> None:
    """Best-effort mirror of mq_consumer dispatch (dev only)."""
    from app.celery_client import send_task

    fid = field_id
    if not fid:
        raise HTTPException(
            status_code=503,
            detail="MQ_FALLBACK_CELERY requires field_id",
        )
    if type == "weather_backfill":
        days = extras.get("days")
        kwargs: dict[str, Any] = {}
        if days is not None:
            kwargs["days"] = int(days)
        send_task(
            "app.tasks.weather.backfill_weather_for_field",
            args=[fid],
            kwargs=kwargs,
        )
    elif type == "soil_fetch":
        job_id = extras.get("job_id")
        args: list[str] = [fid]
        if job_id:
            args.append(str(job_id))
        send_task("app.tasks.soil.fetch_soil_for_field", args=args)
    elif type == "satellite_analysis":
        months = int(extras.get("months") or 60)
        force = bool(extras.get("force") or False)
        allow_agri = bool(extras.get("allow_agri") or False)
        sentinel_job_id = extras.get("sentinel_job_id")
        kwargs = {
            "months": months,
            "force": force,
            "allow_agri": allow_agri,
        }
        if sentinel_job_id:
            kwargs["sentinel_job_id"] = str(sentinel_job_id)
        send_task(
            "app.tasks.backfill.backfill_indices_for_field",
            args=[fid],
            kwargs=kwargs,
        )
        if extras.get("with_bridge") or extras.get("bridge_job_id"):
            bk: dict[str, Any] = {"land_id": land_id or extras.get("land_id")}
            if extras.get("bridge_job_id"):
                bk["bridge_job_id"] = str(extras["bridge_job_id"])
            send_task(
                "app.tasks.agri_bridge.bridge_after_backfill",
                args=[fid],
                kwargs=bk,
            )
            if extras.get("dispatch_alerts"):
                try:
                    send_task(
                        "app.tasks.agri_alerts.evaluate_agri_alerts_for_field",
                        args=[fid],
                        kwargs={
                            "land_id": land_id or extras.get("land_id"),
                            "replace_open": True,
                        },
                    )
                except Exception:
                    pass
    elif type == "field_bootstrap":
        send_task("app.tasks.weather.backfill_weather_for_field", args=[fid])
        send_task("app.tasks.soil.fetch_soil_for_field", args=[fid])
        if not extras.get("skip_indices"):
            kwargs = {}
            if extras.get("sentinel_job_id"):
                kwargs["sentinel_job_id"] = str(extras["sentinel_job_id"])
            send_task(
                "app.tasks.backfill.backfill_indices_for_field",
                args=[fid],
                kwargs=kwargs,
            )
    elif type == "agri_bridge":
        send_task(
            "app.tasks.agri_bridge.bridge_field_stac_to_agri",
            args=[fid],
            kwargs={"land_id": land_id or extras.get("land_id")},
        )
    else:
        raise HTTPException(
            status_code=503,
            detail=f"MQ_FALLBACK_CELERY unsupported type: {type}",
        )


def publish_api_task(
    *,
    type: str,
    field_id: str | None = None,
    parcel_id: str | None = None,
    land_id: str | None = None,
    extras: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> str:
    """Publish a TaskMessage; return task_id.

    Raises HTTPException 503 when CloudAMQP is not configured (unless
    MQ_FALLBACK_CELERY=1), or 502 on publish failure.
    """
    extras = dict(extras or {})
    tid = task_id or str(uuid.uuid4())

    try:
        from openfarm_common.mq import publish_task
        from openfarm_common.mq_schemas import TaskMessage
        from openfarm_common.settings import settings as common_settings
    except Exception as e:
        logger.error("mq_helpers_unavailable", error=str(e))
        if _mq_fallback_enabled():
            logger.warning(
                "mq_fallback_celery",
                type=type,
                field_id=field_id,
                reason="helpers_unavailable",
            )
            _fallback_celery(
                type=type, field_id=field_id, land_id=land_id, extras=extras
            )
            return tid
        raise HTTPException(
            status_code=503, detail=f"MQ helpers unavailable: {e}"
        ) from e

    if not common_settings.cloudamqp_url:
        logger.error("cloudamqp_url_missing", type=type, field_id=field_id)
        if _mq_fallback_enabled():
            logger.warning(
                "mq_fallback_celery",
                type=type,
                field_id=field_id,
                reason="CLOUDAMQP_URL_missing",
            )
            _fallback_celery(
                type=type, field_id=field_id, land_id=land_id, extras=extras
            )
            return tid
        raise HTTPException(
            status_code=503,
            detail="CLOUDAMQP_URL not configured (set MQ_FALLBACK_CELERY=1 for local Celery fallback)",
        )

    msg = TaskMessage(
        task_id=tid,
        type=type,
        field_id=field_id,
        parcel_id=parcel_id,
        land_id=land_id,
        extras=extras,
    )
    try:
        publish_task(msg)
    except Exception as e:
        logger.error("mq_publish_failed", task_id=tid, type=type, error=str(e))
        raise HTTPException(
            status_code=502, detail=f"Failed to publish task: {e}"
        ) from e

    logger.info(
        "mq_task_published_from_api",
        task_id=tid,
        type=type,
        field_id=field_id,
        land_id=land_id or parcel_id,
    )
    return tid


__all__ = ["publish_api_task"]
