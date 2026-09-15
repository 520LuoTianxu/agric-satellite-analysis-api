"""Persist ResultMessage payloads into agric_satellite.mq_task_results (+ domain upserts).

Domain upserts live in ``openfarm_common.result_apply`` so API work-complete
can reuse the same logic (download-host isolation D3).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openfarm_common.database_sync import SyncSession
from openfarm_common.mq_schemas import ResultMessage
from openfarm_common.result_apply import (
    UPSERT_SCENE_SQL,
    UPSERT_WEATHER_SQL,
    apply_complete_result,
    apply_domain_from_payload,
    apply_parcel_scene_product,
    apply_result_envelope,
    apply_soil_payload,
    apply_weather_payload,
    handle_result_message_dict,
)
from openfarm_common.trace import (
    bind_trace_from_mapping,
    clear_trace_id,
    get_or_create_trace_id,
)
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Re-export for callers/tests that imported from this module.
__all__ = [
    "UPSERT_SCENE_SQL",
    "UPSERT_WEATHER_SQL",
    "apply_complete_result",
    "apply_domain_from_payload",
    "apply_parcel_scene_product",
    "apply_result_envelope",
    "apply_soil_payload",
    "apply_weather_payload",
    "handle_result_message",
    "handle_result_message_dict",
    "upsert_mq_task_result",
]


def upsert_mq_task_result(msg: ResultMessage, payloads: dict[str, Any]) -> None:
    session = SyncSession()
    try:
        stored = dict(payloads)
        if msg.payload and "inline" not in stored:
            stored["inline"] = msg.payload
        session.execute(
            text(
                """
                INSERT INTO agric_satellite.mq_task_results (
                    task_id, status, oss_urls, payload, error,
                    land_id, finished_at, updated_at
                ) VALUES (
                    :task_id, :status, CAST(:oss_urls AS jsonb), CAST(:payload AS jsonb),
                    :error, :land_id, :finished_at, now()
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    oss_urls = EXCLUDED.oss_urls,
                    payload = EXCLUDED.payload,
                    error = EXCLUDED.error,
                    land_id = COALESCE(EXCLUDED.land_id, agric_satellite.mq_task_results.land_id),
                    finished_at = EXCLUDED.finished_at,
                    updated_at = now()
                """
            ),
            {
                "task_id": msg.task_id,
                "status": msg.status,
                "oss_urls": json.dumps(msg.oss_urls, ensure_ascii=False),
                "payload": json.dumps(stored, default=str, ensure_ascii=False),
                "error": msg.error,
                "land_id": msg.land_id,
                "finished_at": msg.finished_at,
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def handle_result_message(payload: dict[str, Any], meta: dict[str, Any]) -> None:
    try:
        msg = ResultMessage.model_validate(payload)
    except Exception as exc:
        logger.error("invalid_result_message err=%s", exc)
        return

    bind_trace_from_mapping(msg.model_dump())
    get_or_create_trace_id()
    try:
        envelope = {
            "status": msg.status,
            "payload": msg.payload,
            "oss_urls": msg.oss_urls,
            "extras": msg.extras,
            "error": msg.error,
            "land_id": msg.land_id,
        }
        domain_stats = apply_result_envelope(envelope)
        upsert_mq_task_result(msg, {"domain": domain_stats})
        logger.info(
            "mq_result_written task_id=%s status=%s urls=%s domain=%s retry=%s",
            msg.task_id,
            msg.status,
            list(msg.oss_urls.keys()),
            domain_stats,
            meta.get("retry_count"),
        )
    finally:
        clear_trace_id()
