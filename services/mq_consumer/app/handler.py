"""Map CloudAMQP TaskMessage → existing Celery ingest tasks."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from openfarm_common.celery_app import celery_client
from openfarm_common.database_sync import SyncSession
from openfarm_common.mq_results import publish_task_result
from openfarm_common.mq_schemas import TaskMessage

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = {"satellite_analysis", "agri_bridge"}


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
    """Fire-and-forget: index backfill + agri bridge; bridge publishes MQ result."""
    extras = dict(task.extras or {})
    months = int(extras.get("months") or 6)
    force = bool(extras.get("force") or False)
    mode = str(extras.get("mode") or "full")

    mq_kwargs = {
        "mq_task_id": task.task_id,
        "land_id": land_id,
    }

    if mode == "bridge_only":
        async_result = celery_client.send_task(
            "app.tasks.agri_bridge.bridge_field_stac_to_agri",
            args=[field_id],
            kwargs={**mq_kwargs, "land_id": land_id},
            queue="ingest",
        )
        return {
            "dispatched": ["app.tasks.agri_bridge.bridge_field_stac_to_agri"],
            "celery_ids": [async_result.id],
            "mode": mode,
        }

    # Full path mirrors API backfill_field_indices (agri-aware)
    celery_client.send_task(
        "app.tasks.backfill.backfill_indices_for_field",
        args=[field_id],
        kwargs={
            "months": months,
            "allow_agri": True,
            "force": force,
        },
        queue="ingest",
    )
    bridge = celery_client.send_task(
        "app.tasks.agri_bridge.bridge_after_backfill",
        args=[field_id],
        kwargs={
            "land_id": land_id,
            "mq_task_id": task.task_id,
        },
        queue="ingest",
    )
    return {
        "dispatched": [
            "app.tasks.backfill.backfill_indices_for_field",
            "app.tasks.agri_bridge.bridge_after_backfill",
        ],
        "celery_ids": [bridge.id],
        "mode": mode,
        "months": months,
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
            # agri_bridge forces bridge_only
            if task.type == "agri_bridge":
                task.extras = {**(task.extras or {}), "mode": "bridge_only"}
            info = _dispatch_satellite_analysis(task, field_id, land_id)
            logger.info(
                "mq_task_dispatched task_id=%s field_id=%s land_id=%s info=%s meta=%s",
                task.task_id,
                field_id,
                land_id,
                info,
                {k: meta.get(k) for k in ("retry_count", "redelivered")},
            )
            # Result is published by agri_bridge when work finishes.
            # For bridge_only we still rely on the Celery task hook.
        else:
            publish_task_result(
                task_id=task.task_id,
                status="failed",
                error=f"unhandled type: {task.type}",
                field_id=field_id,
                land_id=land_id,
                upload_summary_if_empty=False,
            )
    except Exception as exc:
        # Transient broker/dispatch errors → requeue via raise
        logger.exception("mq_dispatch_failed task_id=%s", task.task_id)
        # If already retried heavily, publish failed (caller may drop)
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
