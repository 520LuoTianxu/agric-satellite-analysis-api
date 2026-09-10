"""Map CloudAMQP TaskMessage → existing Celery ingest tasks."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from openfarm_common.celery_app import celery_client
from openfarm_common.database_sync import SyncSession
from openfarm_common.mq_results import publish_task_result
from openfarm_common.mq_schemas import TaskMessage
from sqlalchemy import text

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = {
    "satellite_analysis",
    "agri_bridge",
    "weather_backfill",
    "soil_fetch",
    "field_bootstrap",
    "assessment_report",
}


def _resolve_field_and_land(
    field_id: str | None,
    parcel_id: str | None,
    land_id: str | None,
) -> tuple[str | None, str | None]:
    """Map field_id / parcel_id(land_id) using agri: tags on public.fields."""
    lid = land_id or parcel_id
    fid = field_id
    session = SyncSession()
    try:
        if fid and not lid:
            row = session.execute(
                text("SELECT tags_json FROM fields WHERE id = CAST(:fid AS uuid)"),
                {"fid": fid},
            ).first()
            if row and row[0]:
                tags = row[0]
                if isinstance(tags, list):
                    for tag in tags:
                        if isinstance(tag, str) and tag.startswith("agri:"):
                            lid = tag[5:].strip() or lid
                            break
        if lid and not fid:
            # parcel_id / land_id → OpenFarm field tagged agri:<land_id>
            row = session.execute(
                text(
                    """
                    SELECT id::text
                    FROM fields
                    WHERE deleted_at IS NULL
                      AND tags_json::text LIKE :pat
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT 1
                    """
                ),
                {"pat": f"%agri:{lid}%"},
            ).first()
            if row:
                fid = row[0]
    finally:
        session.close()
    return fid, lid


def _dispatch_satellite_analysis(
    task: TaskMessage,
    field_id: str,
    land_id: str | None,
) -> dict[str, Any]:
    """Fire-and-forget: index backfill + optional agri bridge; bridge publishes MQ result."""
    extras = dict(task.extras or {})
    months = int(extras.get("months") or 60)
    force = bool(extras.get("force") or False)
    mode = str(extras.get("mode") or "full")
    allow_agri = bool(extras.get("allow_agri") or False)
    # External producers default to agri-aware full path (historical MVP behavior).
    if "allow_agri" not in extras and mode != "bridge_only":
        allow_agri = True
    with_bridge = bool(extras.get("with_bridge") or extras.get("bridge_job_id"))
    if mode == "full" and "with_bridge" not in extras and "bridge_job_id" not in extras:
        # Default full path includes bridge when land_id known or allow_agri
        with_bridge = bool(land_id) or allow_agri
    sentinel_job_id = extras.get("sentinel_job_id")
    bridge_job_id = extras.get("bridge_job_id")
    dispatch_alerts = bool(extras.get("dispatch_alerts") or False)
    lid = land_id or extras.get("land_id")

    if mode == "bridge_only":
        async_result = celery_client.send_task(
            "app.tasks.agri_bridge.bridge_field_stac_to_agri",
            args=[field_id],
            kwargs={
                "mq_task_id": task.task_id,
                "land_id": lid,
            },
            queue="ingest",
        )
        return {
            "dispatched": ["app.tasks.agri_bridge.bridge_field_stac_to_agri"],
            "celery_ids": [async_result.id],
            "mode": mode,
        }

    backfill_kwargs: dict[str, Any] = {
        "months": months,
        "allow_agri": allow_agri,
        "force": force,
    }
    if sentinel_job_id:
        backfill_kwargs["sentinel_job_id"] = str(sentinel_job_id)

    celery_client.send_task(
        "app.tasks.backfill.backfill_indices_for_field",
        args=[field_id],
        kwargs=backfill_kwargs,
        queue="ingest",
    )
    dispatched = ["app.tasks.backfill.backfill_indices_for_field"]
    celery_ids: list[str] = []

    if with_bridge:
        bridge_kwargs: dict[str, Any] = {
            "land_id": lid,
            "mq_task_id": task.task_id,
        }
        if bridge_job_id:
            bridge_kwargs["bridge_job_id"] = str(bridge_job_id)
        bridge = celery_client.send_task(
            "app.tasks.agri_bridge.bridge_after_backfill",
            args=[field_id],
            kwargs=bridge_kwargs,
            queue="ingest",
        )
        dispatched.append("app.tasks.agri_bridge.bridge_after_backfill")
        celery_ids.append(bridge.id)

        if dispatch_alerts:
            try:
                celery_client.send_task(
                    "app.tasks.agri_alerts.evaluate_agri_alerts_for_field",
                    args=[field_id],
                    kwargs={"land_id": lid, "replace_open": True},
                    queue="ingest",
                )
                dispatched.append(
                    "app.tasks.agri_alerts.evaluate_agri_alerts_for_field"
                )
            except Exception:
                logger.warning(
                    "mq_alert_dispatch_failed task_id=%s field_id=%s",
                    task.task_id,
                    field_id,
                )
    else:
        # Non-agri / no bridge: publish lightweight accepted result (orchestration only).
        publish_task_result(
            task_id=task.task_id,
            status="success",
            field_id=field_id,
            land_id=lid,
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
    field_id: str,
) -> dict[str, Any]:
    extras = dict(task.extras or {})
    kwargs: dict[str, Any] = {"mq_task_id": task.task_id}
    if extras.get("days") is not None:
        kwargs["days"] = int(extras["days"])
    async_result = celery_client.send_task(
        "app.tasks.weather.backfill_weather_for_field",
        args=[field_id],
        kwargs=kwargs,
        queue="ingest",
    )
    return {
        "dispatched": ["app.tasks.weather.backfill_weather_for_field"],
        "celery_ids": [async_result.id],
        "days": kwargs.get("days"),
    }


def _dispatch_soil_fetch(
    task: TaskMessage,
    field_id: str,
) -> dict[str, Any]:
    extras = dict(task.extras or {})
    kwargs: dict[str, Any] = {"mq_task_id": task.task_id}
    job_id = extras.get("job_id")
    args: list[str] = [field_id]
    if job_id:
        args.append(str(job_id))
    async_result = celery_client.send_task(
        "app.tasks.soil.fetch_soil_for_field",
        args=args,
        kwargs=kwargs,
        queue="ingest",
    )
    return {
        "dispatched": ["app.tasks.soil.fetch_soil_for_field"],
        "celery_ids": [async_result.id],
        "job_id": job_id,
    }


def _dispatch_field_bootstrap(
    task: TaskMessage,
    field_id: str,
    land_id: str | None,
) -> dict[str, Any]:
    """One MQ message → fan-out weather + soil + optional satellite indices.

    Publishes a single lightweight ResultMessage after Celery enqueue
    (accepted/dispatched). Standalone weather_backfill / soil_fetch /
    satellite_analysis carry full end-of-task results via mq_task_id hooks.
    """
    extras = dict(task.extras or {})
    skip_indices = bool(extras.get("skip_indices") or False)
    sentinel_job_id = extras.get("sentinel_job_id")
    dispatched: list[str] = []
    celery_ids: list[str] = []

    w = celery_client.send_task(
        "app.tasks.weather.backfill_weather_for_field",
        args=[field_id],
        kwargs={},
        queue="ingest",
    )
    dispatched.append("app.tasks.weather.backfill_weather_for_field")
    celery_ids.append(w.id)

    s = celery_client.send_task(
        "app.tasks.soil.fetch_soil_for_field",
        args=[field_id],
        kwargs={},
        queue="ingest",
    )
    dispatched.append("app.tasks.soil.fetch_soil_for_field")
    celery_ids.append(s.id)

    if not skip_indices:
        bk: dict[str, Any] = {}
        if sentinel_job_id:
            bk["sentinel_job_id"] = str(sentinel_job_id)
        b = celery_client.send_task(
            "app.tasks.backfill.backfill_indices_for_field",
            args=[field_id],
            kwargs=bk,
            queue="ingest",
        )
        dispatched.append("app.tasks.backfill.backfill_indices_for_field")
        celery_ids.append(b.id)

    publish_task_result(
        task_id=task.task_id,
        status="success",
        field_id=field_id,
        land_id=land_id,
        extras={
            "phase": "bootstrap_dispatched",
            "dispatched": dispatched,
            "skip_indices": skip_indices,
        },
        upload_summary_if_empty=True,
    )
    return {
        "dispatched": dispatched,
        "celery_ids": celery_ids,
        "skip_indices": skip_indices,
    }



def _dispatch_assessment_report(
    task: TaskMessage,
    field_id: str,
) -> dict[str, Any]:
    """Dispatch land-assessment PDF Celery task; result published by ingest.

    ``field_id`` is required (resolved by handle_task_message). ``job_id`` is
    optional and forwarded so ingest can update a *local* Job when present, and
    so ResultMessage carries job_id for the process-host writer / API DB.
    """
    extras = dict(task.extras or {})
    if not field_id:
        raise ValueError("assessment_report requires field_id")
    job_id = extras.get("job_id")
    kwargs: dict[str, Any] = {
        "mq_task_id": task.task_id,
        "field_id": str(field_id),
    }
    if job_id:
        kwargs["job_id"] = str(job_id)
    for key in ("crop_type", "crop_name_zh"):
        if extras.get(key) is not None:
            kwargs[key] = extras[key]
    async_result = celery_client.send_task(
        "app.tasks.assessment_report.generate_assessment_report",
        kwargs=kwargs,
        queue="ingest",
    )
    return {
        "dispatched": ["app.tasks.assessment_report.generate_assessment_report"],
        "celery_ids": [async_result.id],
        "job_id": str(job_id) if job_id else None,
        "field_id": field_id,
    }

def handle_task_message(payload: dict[str, Any], meta: dict[str, Any]) -> None:
    """Process one TaskMessage. Permanent failures publish failed result (no raise)."""
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
            field_id=task.field_id,
            land_id=task.land_id or task.parcel_id,
            upload_summary_if_empty=False,
        )
        return

    field_id, land_id = _resolve_field_and_land(
        task.field_id, task.parcel_id, task.land_id
    )
    if not field_id:
        publish_task_result(
            task_id=task.task_id,
            status="failed",
            error="field_id could not be resolved (provide field_id or agri-tagged parcel_id)",
            land_id=land_id,
            upload_summary_if_empty=False,
        )
        return

    try:
        if task.type in ("satellite_analysis", "agri_bridge"):
            if task.type == "agri_bridge":
                task.extras = {**(task.extras or {}), "mode": "bridge_only"}
            info = _dispatch_satellite_analysis(task, field_id, land_id)
        elif task.type == "weather_backfill":
            info = _dispatch_weather_backfill(task, field_id)
        elif task.type == "soil_fetch":
            info = _dispatch_soil_fetch(task, field_id)
        elif task.type == "field_bootstrap":
            info = _dispatch_field_bootstrap(task, field_id, land_id)
        elif task.type == "assessment_report":
            try:
                info = _dispatch_assessment_report(task, field_id)
            except ValueError as exc:
                publish_task_result(
                    task_id=task.task_id,
                    status="failed",
                    error=str(exc),
                    field_id=field_id,
                    land_id=land_id,
                    upload_summary_if_empty=False,
                )
                return
        else:
            publish_task_result(
                task_id=task.task_id,
                status="failed",
                error=f"unhandled type: {task.type}",
                field_id=field_id,
                land_id=land_id,
                upload_summary_if_empty=False,
            )
            return

        logger.info(
            "mq_task_dispatched task_id=%s type=%s field_id=%s land_id=%s info=%s meta=%s",
            task.task_id,
            task.type,
            field_id,
            land_id,
            info,
            {k: meta.get(k) for k in ("retry_count", "redelivered")},
        )
    except Exception as exc:
        # Transient broker/dispatch errors → requeue via raise
        logger.exception("mq_dispatch_failed task_id=%s", task.task_id)
        if int(meta.get("retry_count") or 0) >= 2:
            try:
                publish_task_result(
                    task_id=task.task_id,
                    status="failed",
                    error=f"dispatch failed: {exc}",
                    field_id=field_id,
                    land_id=land_id,
                    upload_summary_if_empty=False,
                )
            except Exception:
                pass
            return
        raise
