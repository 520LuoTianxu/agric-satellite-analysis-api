"""Map CloudAMQP TaskMessage to direct canonical-land Celery tasks.

A message contains one identity only: land_id. The consumer never looks up a
UUID field, parses tags, or translates between parcel identifiers.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from openfarm_common.celery_app import celery_client
from openfarm_common.mq_results import publish_task_result
from openfarm_common.mq_schemas import TaskMessage
from openfarm_common.trace import (
    bind_trace_from_mapping,
    clear_trace_id,
    get_or_create_trace_id,
)

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = {
    "satellite_analysis",
    "agri_bridge",
    "weather_backfill",
    "soil_fetch",
    "land_bootstrap",
    "assessment_report",
    "season_growth_report",
}


def _dispatch_satellite_analysis(
    task: TaskMessage,
    land_id: str,
) -> dict[str, Any]:
    """Dispatch the remote-sensing wave for one canonical land parcel."""
    extras = dict(task.extras or {})
    months = int(extras.get("months") or 24)
    force = bool(extras.get("force") or False)
    mode = str(extras.get("mode") or "full")
    with_bridge = bool(extras.get("with_bridge") or extras.get("bridge_job_id"))
    if mode == "full" and "with_bridge" not in extras and "bridge_job_id" not in extras:
        with_bridge = True

    sentinel_job_id = extras.get("sentinel_job_id")
    bridge_job_id = extras.get("bridge_job_id")
    dispatch_alerts = bool(extras.get("dispatch_alerts") or False)

    if mode == "bridge_only":
        async_result = celery_client.send_task(
            "app.tasks.agri_bridge.bridge_land_stac_to_agri",
            args=[land_id],
            kwargs={"mq_task_id": task.task_id},
            queue="ingest",
        )
        return {
            "dispatched": ["app.tasks.agri_bridge.bridge_land_stac_to_agri"],
            "celery_ids": [async_result.id],
            "mode": mode,
        }

    backfill_kwargs: dict[str, Any] = {
        "months": months,
        "force": force,
        "mq_task_id": task.task_id,
    }
    for key in ("date_from", "date_to"):
        if extras.get(key):
            backfill_kwargs[key] = str(extras[key])[:10]
    if sentinel_job_id:
        backfill_kwargs["sentinel_job_id"] = str(sentinel_job_id)

    backfill = celery_client.send_task(
        "app.tasks.backfill.backfill_indices_for_land",
        args=[land_id],
        kwargs=backfill_kwargs,
        queue="ingest",
    )
    dispatched = ["app.tasks.backfill.backfill_indices_for_land"]
    celery_ids = [backfill.id]

    if with_bridge:
        bridge_kwargs: dict[str, Any] = {"mq_task_id": task.task_id}
        if bridge_job_id:
            bridge_kwargs["bridge_job_id"] = str(bridge_job_id)
        bridge = celery_client.send_task(
            "app.tasks.agri_bridge.bridge_after_backfill",
            args=[land_id],
            kwargs=bridge_kwargs,
            queue="ingest",
        )
        dispatched.append("app.tasks.agri_bridge.bridge_after_backfill")
        celery_ids.append(bridge.id)
        if dispatch_alerts:
            celery_client.send_task(
                "app.tasks.agri_alerts.evaluate_agri_alerts_for_land",
                args=[land_id],
                kwargs={"replace_open": True},
                queue="ingest",
            )
            dispatched.append("app.tasks.agri_alerts.evaluate_agri_alerts_for_land")
    else:
        publish_task_result(
            task_id=task.task_id,
            status="success",
            land_id=land_id,
            extras={
                "phase": "dispatched",
                "dispatched": dispatched,
                "months": months,
            },
            upload_summary_if_empty=True,
        )

    return {
        "dispatched": dispatched,
        "celery_ids": celery_ids,
        "mode": mode,
        "months": months,
        "with_bridge": with_bridge,
    }


def _dispatch_weather_backfill(
    task: TaskMessage,
    land_id: str,
) -> dict[str, Any]:
    extras = dict(task.extras or {})
    kwargs: dict[str, Any] = {"mq_task_id": task.task_id}
    if extras.get("days") is not None:
        kwargs["days"] = int(extras["days"])
    async_result = celery_client.send_task(
        "app.tasks.weather.backfill_weather_for_land",
        args=[land_id],
        kwargs=kwargs,
        queue="ingest",
    )
    return {
        "dispatched": ["app.tasks.weather.backfill_weather_for_land"],
        "celery_ids": [async_result.id],
        "days": kwargs.get("days"),
    }


def _dispatch_soil_fetch(
    task: TaskMessage,
    land_id: str,
) -> dict[str, Any]:
    extras = dict(task.extras or {})
    kwargs: dict[str, Any] = {"mq_task_id": task.task_id}
    args = [land_id]
    if extras.get("job_id"):
        args.append(str(extras["job_id"]))
    async_result = celery_client.send_task(
        "app.tasks.soil.fetch_soil_for_land",
        args=args,
        kwargs=kwargs,
        queue="ingest",
    )
    return {
        "dispatched": ["app.tasks.soil.fetch_soil_for_land"],
        "celery_ids": [async_result.id],
        "job_id": extras.get("job_id"),
    }


def _dispatch_land_bootstrap(
    task: TaskMessage,
    land_id: str,
) -> dict[str, Any]:
    """Fan out weather, soil, and optional remote-sensing work for one parcel."""
    extras = dict(task.extras or {})
    skip_indices = bool(extras.get("skip_indices") or False)
    sentinel_job_id = extras.get("sentinel_job_id")
    dispatched: list[str] = []
    celery_ids: list[str] = []

    weather_kwargs: dict[str, Any] = {}
    days = extras.get("days") or extras.get("weather_days")
    if days is None and extras.get("date_from"):
        try:
            from datetime import date as date_cls

            start = date_cls.fromisoformat(str(extras["date_from"])[:10])
            end_raw = extras.get("date_to")
            end = (
                date_cls.fromisoformat(str(end_raw)[:10])
                if end_raw
                else date_cls.today()
            )
            days = max(1, (end - start).days)
        except ValueError:
            days = None
    if days is not None:
        weather_kwargs["days"] = int(days)

    weather = celery_client.send_task(
        "app.tasks.weather.backfill_weather_for_land",
        args=[land_id],
        kwargs=weather_kwargs,
        queue="ingest",
    )
    dispatched.append("app.tasks.weather.backfill_weather_for_land")
    celery_ids.append(weather.id)

    soil = celery_client.send_task(
        "app.tasks.soil.fetch_soil_for_land",
        args=[land_id],
        kwargs={},
        queue="ingest",
    )
    dispatched.append("app.tasks.soil.fetch_soil_for_land")
    celery_ids.append(soil.id)

    if not skip_indices:
        index_kwargs: dict[str, Any] = {}
        if sentinel_job_id:
            index_kwargs["sentinel_job_id"] = str(sentinel_job_id)
        for key in ("date_from", "date_to"):
            if extras.get(key):
                index_kwargs[key] = str(extras[key])[:10]
        if extras.get("months") is not None:
            index_kwargs["months"] = int(extras["months"])
        if extras.get("force") is not None:
            index_kwargs["force"] = bool(extras["force"])
        indices = celery_client.send_task(
            "app.tasks.backfill.backfill_indices_for_land",
            args=[land_id],
            kwargs=index_kwargs,
            queue="ingest",
        )
        dispatched.append("app.tasks.backfill.backfill_indices_for_land")
        celery_ids.append(indices.id)

        with_bridge = bool(extras.get("with_bridge", True))
        if with_bridge:
            bridge_kwargs: dict[str, Any] = {}
            if extras.get("bridge_job_id"):
                bridge_kwargs["bridge_job_id"] = str(extras["bridge_job_id"])
            bridge = celery_client.send_task(
                "app.tasks.agri_bridge.bridge_after_backfill",
                args=[land_id],
                kwargs=bridge_kwargs,
                queue="ingest",
            )
            dispatched.append("app.tasks.agri_bridge.bridge_after_backfill")
            celery_ids.append(bridge.id)

    followup = extras.get("followup_assessment")
    if isinstance(followup, dict) and followup.get("job_id"):
        assess_kwargs: dict[str, Any] = {
            "job_id": str(followup["job_id"]),
            "land_id": land_id,
            "pull_data": True,
            "wait_celery_ids": list(celery_ids),
        }
        for key in ("crop_type", "crop_name_zh", "date_from", "date_to", "years"):
            if followup.get(key) is not None:
                assess_kwargs[key] = followup[key]
            elif extras.get(key) is not None:
                assess_kwargs[key] = extras[key]
        if followup.get("mq_task_id"):
            assess_kwargs["mq_task_id"] = str(followup["mq_task_id"])
        result = celery_client.send_task(
            "app.tasks.assessment_report.generate_assessment_report",
            kwargs=assess_kwargs,
            queue="ingest",
        )
        dispatched.append("app.tasks.assessment_report.generate_assessment_report")
        celery_ids.append(result.id)

    followup_sg = extras.get("followup_season_growth")
    if isinstance(followup_sg, dict) and followup_sg.get("job_id"):
        season_kwargs: dict[str, Any] = {
            "job_id": str(followup_sg["job_id"]),
            "land_id": land_id,
            "pull_data": True,
            "wait_celery_ids": list(celery_ids),
        }
        for key in ("start_date", "end_date", "crops", "label", "material_keys"):
            if followup_sg.get(key) is not None:
                season_kwargs[key] = followup_sg[key]
            elif extras.get(key) is not None:
                season_kwargs[key] = extras[key]
        result = celery_client.send_task(
            "app.tasks.season_growth_report.generate_season_growth_report",
            kwargs=season_kwargs,
            queue="ingest",
        )
        dispatched.append(
            "app.tasks.season_growth_report.generate_season_growth_report"
        )
        celery_ids.append(result.id)

    publish_task_result(
        task_id=task.task_id,
        status="success",
        land_id=land_id,
        extras={
            "phase": "bootstrap_dispatched",
            "dispatched": dispatched,
            "skip_indices": skip_indices,
            "date_from": extras.get("date_from"),
            "date_to": extras.get("date_to"),
            "days": weather_kwargs.get("days"),
        },
        upload_summary_if_empty=True,
    )
    return {
        "dispatched": dispatched,
        "celery_ids": celery_ids,
        "skip_indices": skip_indices,
        "date_from": extras.get("date_from"),
        "date_to": extras.get("date_to"),
        "days": weather_kwargs.get("days"),
    }


def _dispatch_assessment_report(
    task: TaskMessage,
    land_id: str,
) -> dict[str, Any]:
    """Dispatch land-assessment PDF generation using the same land_id."""
    extras = dict(task.extras or {})
    job_id = extras.get("job_id")
    kwargs: dict[str, Any] = {
        "mq_task_id": task.task_id,
        "land_id": land_id,
    }
    if job_id:
        kwargs["job_id"] = str(job_id)
    for key in ("crop_type", "crop_name_zh", "date_from", "date_to", "years"):
        if extras.get(key) is not None:
            kwargs[key] = extras[key]
    if extras.get("pull_data") is not None:
        kwargs["pull_data"] = bool(extras["pull_data"])
    if extras.get("wait_celery_ids"):
        kwargs["wait_celery_ids"] = list(extras["wait_celery_ids"])
    async_result = celery_client.send_task(
        "app.tasks.assessment_report.generate_assessment_report",
        kwargs=kwargs,
        queue="ingest",
    )
    return {
        "dispatched": ["app.tasks.assessment_report.generate_assessment_report"],
        "celery_ids": [async_result.id],
        "job_id": str(job_id) if job_id else None,
    }


def _dispatch_season_growth_report(
    task: TaskMessage,
    land_id: str,
) -> dict[str, Any]:
    """Dispatch season-growth PDF generation using the same land_id."""
    extras = dict(task.extras or {})
    job_id = extras.get("job_id")
    kwargs: dict[str, Any] = {
        "mq_task_id": task.task_id,
        "land_id": land_id,
    }
    if job_id:
        kwargs["job_id"] = str(job_id)
    for key in ("start_date", "end_date", "crops", "label", "material_keys"):
        if extras.get(key) is not None:
            kwargs[key] = extras[key]
    if extras.get("pull_data") is not None:
        kwargs["pull_data"] = bool(extras["pull_data"])
    if extras.get("wait_celery_ids"):
        kwargs["wait_celery_ids"] = list(extras["wait_celery_ids"])
    async_result = celery_client.send_task(
        "app.tasks.season_growth_report.generate_season_growth_report",
        kwargs=kwargs,
        queue="ingest",
    )
    return {
        "dispatched": ["app.tasks.season_growth_report.generate_season_growth_report"],
        "celery_ids": [async_result.id],
        "job_id": str(job_id) if job_id else None,
    }


def handle_task_message(payload: dict[str, Any], meta: dict[str, Any]) -> None:
    """Validate and dispatch one canonical-land task message."""
    bind_trace_from_mapping(payload)
    get_or_create_trace_id()
    try:
        _handle_task_message(payload, meta)
    finally:
        clear_trace_id()


def _handle_task_message(payload: dict[str, Any], meta: dict[str, Any]) -> None:
    try:
        task = TaskMessage.model_validate(payload)
    except Exception as exc:
        logger.error("invalid_task_message err=%s payload_keys=%s", exc, list(payload))
        tid = str(payload.get("task_id") or uuid.uuid4())
        publish_task_result(
            task_id=tid,
            status="failed",
            error=f"invalid TaskMessage: {exc}",
            upload_summary_if_empty=False,
        )
        return

    if task.type not in SUPPORTED_TYPES:
        publish_task_result(
            task_id=task.task_id,
            status="failed",
            error=f"unsupported type: {task.type}",
            land_id=task.land_id,
            upload_summary_if_empty=False,
        )
        return

    if not task.land_id:
        publish_task_result(
            task_id=task.task_id,
            status="failed",
            error="land_id is required",
            upload_summary_if_empty=False,
        )
        return

    land_id = str(task.land_id)
    try:
        if task.type in ("satellite_analysis", "agri_bridge"):
            if task.type == "agri_bridge":
                task.extras = {**(task.extras or {}), "mode": "bridge_only"}
            info = _dispatch_satellite_analysis(task, land_id)
        elif task.type == "weather_backfill":
            info = _dispatch_weather_backfill(task, land_id)
        elif task.type == "soil_fetch":
            info = _dispatch_soil_fetch(task, land_id)
        elif task.type == "land_bootstrap":
            info = _dispatch_land_bootstrap(task, land_id)
        elif task.type == "assessment_report":
            info = _dispatch_assessment_report(task, land_id)
        elif task.type == "season_growth_report":
            info = _dispatch_season_growth_report(task, land_id)
        else:
            raise ValueError(f"unhandled type: {task.type}")

        logger.info(
            "mq_task_dispatched task_id=%s type=%s land_id=%s info=%s meta=%s",
            task.task_id,
            task.type,
            land_id,
            info,
            {k: meta.get(k) for k in ("retry_count", "redelivered")},
        )
    except Exception as exc:
        logger.exception(
            "mq_dispatch_failed task_id=%s land_id=%s", task.task_id, land_id
        )
        if int(meta.get("retry_count") or 0) >= 2:
            publish_task_result(
                task_id=task.task_id,
                status="failed",
                error=f"dispatch failed: {exc}",
                land_id=land_id,
                upload_summary_if_empty=False,
            )
            return
        raise


__all__ = ["SUPPORTED_TYPES", "handle_task_message"]
