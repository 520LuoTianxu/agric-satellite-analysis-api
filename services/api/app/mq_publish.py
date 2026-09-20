"""Publish canonical-land tasks to CloudAMQP.

Every task carries one parcel identity: land_id. No UUID-field or parcel-id
resolution is performed here.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import HTTPException

from agric_satellite_analysis_common.task_priority import (
    BACKGROUND_TASK_PRIORITY,
    MANUAL_TASK_PRIORITY,
    normalize_task_priority,
)
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
    land_id: str,
    extras: dict[str, Any],
) -> None:
    """Best-effort direct Celery dispatch for local/dev fallback."""
    from app.celery_client import send_task

    # fallback 也必须复用同一优先级，否则本地直连 Celery 会绕过 MQ consumer
    # 的优先级适配，导致报告申请和普通任务出现两套排序行为。
    task_priority = normalize_task_priority(extras.get("priority"))

    def dispatch(task_name: str, *args: Any, **kwargs: Any) -> Any:
        return send_task(task_name, *args, priority=task_priority, **kwargs)

    if type == "satellite_batch":
        dispatch(
            "app.tasks.satellite_batch.process_satellite_batch",
            kwargs={"job_id": str(extras["job_id"])},
            queue="ingest",
        )
    elif type == "weather_backfill":
        days = extras.get("days") or extras.get("weather_days")
        kwargs = {"days": int(days)} if days is not None else {}
        dispatch(
            "app.tasks.weather.backfill_weather_for_land", args=[land_id], kwargs=kwargs
        )
    elif type == "soil_fetch":
        args = [land_id]
        if extras.get("job_id"):
            args.append(str(extras["job_id"]))
        dispatch("app.tasks.soil.fetch_soil_for_land", args=args)
    elif type == "satellite_analysis":
        kwargs: dict[str, Any] = {
            "months": int(extras.get("months") or 24),
            "force": bool(extras.get("force") or False),
        }
        for key in (
            "sentinel_job_id",
            "date_from",
            "date_to",
            "growing_seasons",
            "season_months",
            "processing_window_km",
        ):
            if extras.get(key) is not None:
                kwargs[key] = extras[key]
        dispatch(
            "app.tasks.backfill.backfill_indices_for_land",
            args=[land_id],
            kwargs=kwargs,
        )
        if extras.get("with_bridge") or extras.get("bridge_job_id"):
            bridge_kwargs: dict[str, Any] = {}
            if extras.get("bridge_job_id"):
                bridge_kwargs["bridge_job_id"] = str(extras["bridge_job_id"])
            dispatch(
                "app.tasks.agri_bridge.bridge_land_stac_to_agri",
                args=[land_id],
                kwargs=bridge_kwargs,
            )
            if extras.get("dispatch_alerts"):
                dispatch(
                    "app.tasks.agri_alerts.evaluate_agri_alerts_for_land",
                    args=[land_id],
                    kwargs={"replace_open": True},
                )
    elif type == "land_bootstrap":
        weather_kwargs: dict[str, Any] = {}
        days = extras.get("days") or extras.get("weather_days")
        if days is not None:
            weather_kwargs["days"] = int(days)
        weather_result = dispatch(
            "app.tasks.weather.backfill_weather_for_land",
            args=[land_id],
            kwargs=weather_kwargs,
        )
        soil_result = dispatch("app.tasks.soil.fetch_soil_for_land", args=[land_id])
        if not extras.get("skip_indices"):
            index_kwargs: dict[str, Any] = {}
            for key in ("sentinel_job_id", "date_from", "date_to"):
                if extras.get(key) is not None:
                    index_kwargs[key] = extras[key]
            index_result = dispatch(
                "app.tasks.backfill.backfill_indices_for_land",
                args=[land_id],
                kwargs=index_kwargs,
            )
        else:
            index_result = None

        # 即使本次复用共享遥感批次而跳过单地块指数下载，也必须继续派发报告；
        # 报告任务会通过 wait_celery_ids 等待天气/土壤，并自行校验共享遥感覆盖。
        followup = extras.get("followup_assessment")
        if isinstance(followup, dict) and followup.get("job_id"):
            kwargs = {
                "job_id": str(followup["job_id"]),
                "land_id": land_id,
                "pull_data": True,
                "wait_celery_ids": [
                    result.id
                    for result in (weather_result, soil_result, index_result)
                    if result is not None and getattr(result, "id", None)
                ],
            }
            for key in (
                "crop_type",
                "crop_name_zh",
                "date_from",
                "date_to",
                "years",
                "mq_task_id",
            ):
                if followup.get(key) is not None:
                    kwargs[key] = followup[key]
            dispatch(
                "app.tasks.assessment_report.generate_assessment_report",
                kwargs=kwargs,
                queue="ingest",
            )
        followup_sg = extras.get("followup_season_growth")
        if isinstance(followup_sg, dict) and followup_sg.get("job_id"):
            kwargs = {
                "job_id": str(followup_sg["job_id"]),
                "land_id": land_id,
                "pull_data": True,
                "wait_celery_ids": [
                    result.id
                    for result in (weather_result, soil_result, index_result)
                    if result is not None and getattr(result, "id", None)
                ],
            }
            for key in (
                "start_date",
                "end_date",
                "crops",
                "label",
                "material_keys",
                "mq_task_id",
            ):
                if followup_sg.get(key) is not None:
                    kwargs[key] = followup_sg[key]
            dispatch(
                "app.tasks.season_growth_report.generate_season_growth_report",
                kwargs=kwargs,
                queue="ingest",
            )
    elif type == "agri_bridge":
        dispatch(
            "app.tasks.agri_bridge.bridge_land_stac_to_agri",
            args=[land_id],
        )
    elif type == "assessment_report":
        job_id = extras.get("job_id")
        if not job_id:
            raise HTTPException(
                status_code=503,
                detail="MQ_FALLBACK_CELERY assessment_report requires extras.job_id",
            )
        dispatch(
            "app.tasks.assessment_report.generate_assessment_report",
            kwargs={"job_id": str(job_id), "land_id": land_id},
            queue="ingest",
        )
    elif type == "season_growth_report":
        job_id = extras.get("job_id")
        if not job_id:
            raise HTTPException(
                status_code=503,
                detail="MQ_FALLBACK_CELERY season_growth_report requires extras.job_id",
            )
        kwargs = {"job_id": str(job_id), "land_id": land_id}
        for key in ("start_date", "end_date", "crops", "label", "material_keys"):
            if extras.get(key) is not None:
                kwargs[key] = extras[key]
        if extras.get("pull_data") is not None:
            kwargs["pull_data"] = bool(extras["pull_data"])
        if extras.get("wait_celery_ids"):
            kwargs["wait_celery_ids"] = list(extras["wait_celery_ids"])
        dispatch(
            "app.tasks.season_growth_report.generate_season_growth_report",
            kwargs=kwargs,
            queue="ingest",
        )
    else:
        raise HTTPException(
            status_code=503,
            detail=f"MQ_FALLBACK_CELERY unsupported type: {type}",
        )


def publish_api_task(
    *,
    type: str,
    land_id: str | None = None,
    extras: dict[str, Any] | None = None,
    task_id: str | None = None,
    priority: int | None = None,
) -> str:
    """Publish a task with the canonical parcel land_id."""
    if not land_id:
        raise HTTPException(status_code=400, detail="land_id is required")
    extras = dict(extras or {})
    if priority is None:
        # 报告/建档类默认属于普通人工任务；真正的一键选地报告入口会显式
        # 传入最高优先级，避免通用 MQ API 的任意调用方直接占用最高队列。
        priority = (
            MANUAL_TASK_PRIORITY
            if type in ("assessment_report", "season_growth_report", "land_bootstrap")
            else BACKGROUND_TASK_PRIORITY
        )
    resolved_priority = normalize_task_priority(priority)
    # 由服务端覆盖 payload 中同名字段，防止通用接口调用方伪造最高优先级。
    extras["priority"] = resolved_priority
    tid = task_id or str(uuid.uuid4())
    from agric_satellite_analysis_common.trace import get_or_create_trace_id, stamp_trace_on_payload

    trace_id = get_or_create_trace_id()

    try:
        from app.services.work_items import (
            CLAIMABLE_TYPES,
            enqueue_work_item_sync,
            should_enqueue_work_items,
            should_publish_mq,
            work_item_idempotency_key,
        )
    except Exception:
        should_publish_mq = lambda: True  # noqa: E731
        should_enqueue_work_items = lambda: False  # noqa: E731
        CLAIMABLE_TYPES = frozenset()
        enqueue_work_item_sync = None
        work_item_idempotency_key = None

    if (
        should_enqueue_work_items()
        and type in CLAIMABLE_TYPES
        and enqueue_work_item_sync
    ):
        try:
            idem = work_item_idempotency_key(type, task_id=tid, extras=extras)
            work_id = enqueue_work_item_sync(
                type=type,
                payload=stamp_trace_on_payload(
                    {
                        "land_id": land_id,
                        "extras": extras,
                        "task_id": tid,
                    }
                ),
                priority=resolved_priority,
                idempotency_key=idem,
            )
            if work_id:
                logger.info(
                    "work_item_enqueued_from_publish",
                    work_id=work_id,
                    task_id=tid,
                    type=type,
                    land_id=land_id,
                )
        except Exception as exc:
            logger.error(
                "work_item_enqueue_from_publish_failed",
                task_id=tid,
                type=type,
                error=str(exc),
            )
            if not should_publish_mq():
                raise HTTPException(
                    status_code=503, detail=f"work_items enqueue failed: {exc}"
                ) from exc

    if not should_publish_mq():
        logger.info(
            "mq_publish_skipped_claim_mode",
            task_id=tid,
            type=type,
            land_id=land_id,
        )
        return tid

    try:
        from agric_satellite_analysis_common.mq import publish_task
        from agric_satellite_analysis_common.mq_schemas import TaskMessage
        from agric_satellite_analysis_common.settings import settings as common_settings
    except Exception as exc:
        logger.error("mq_helpers_unavailable", error=str(exc))
        if _mq_fallback_enabled():
            _fallback_celery(type=type, land_id=land_id, extras=extras)
            return tid
        raise HTTPException(
            status_code=503, detail=f"MQ helpers unavailable: {exc}"
        ) from exc

    if not common_settings.cloudamqp_url:
        logger.error("cloudamqp_url_missing", type=type, land_id=land_id)
        if _mq_fallback_enabled():
            _fallback_celery(type=type, land_id=land_id, extras=extras)
            return tid
        raise HTTPException(
            status_code=503,
            detail="CLOUDAMQP_URL not configured (set MQ_FALLBACK_CELERY=1 for local Celery fallback)",
        )

    msg = TaskMessage(
        task_id=tid,
        type=type,
        land_id=land_id,
        extras=extras,
        priority=resolved_priority,
        trace_id=trace_id,
    )
    try:
        publish_task(msg)
    except Exception as exc:
        logger.error("mq_publish_failed", task_id=tid, type=type, error=str(exc))
        raise HTTPException(
            status_code=502, detail=f"Failed to publish task: {exc}"
        ) from exc

    logger.info(
        "mq_task_published_from_api",
        task_id=tid,
        type=type,
        land_id=land_id,
        trace_id=trace_id,
    )
    return tid


__all__ = ["publish_api_task"]
