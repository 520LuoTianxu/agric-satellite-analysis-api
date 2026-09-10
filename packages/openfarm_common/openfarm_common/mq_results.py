"""Helpers to collect OSS URLs and publish ResultMessage after Celery work."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from openfarm_common.database_sync import SyncSession
from openfarm_common.mq import publish_result
from openfarm_common.mq_schemas import ResultMessage
from openfarm_common.storage import get_storage

logger = logging.getLogger(__name__)


def collect_parcel_oss_urls(land_id: str, *, limit: int = 50) -> dict[str, str]:
    """Build public URLs from agri.parcel_scene_products.json_oss_key."""
    if not land_id:
        return {}
    urls: dict[str, str] = {}
    session = SyncSession()
    try:
        rows = session.execute(
            text(
                """
                SELECT date::text AS d, sensor, json_oss_key
                FROM agri.parcel_scene_products
                WHERE land_id = :land_id
                  AND json_oss_key IS NOT NULL
                  AND json_oss_key <> ''
                ORDER BY date DESC
                LIMIT :lim
                """
            ),
            {"land_id": land_id, "lim": limit},
        ).mappings().all()
        storage = get_storage()
        for row in rows:
            key = row["json_oss_key"]
            label = f"{row['d']}_{row['sensor']}"
            try:
                urls[label] = storage.public_url(key)
            except Exception as exc:
                logger.warning("oss_url_build_failed key=%s err=%s", key, exc)
                urls[label] = key
    except Exception as exc:
        logger.warning("collect_parcel_oss_urls_failed land_id=%s err=%s", land_id, exc)
    finally:
        session.close()
    return urls


def upload_result_summary(
    task_id: str,
    summary: dict[str, Any],
) -> dict[str, str]:
    """Upload a small JSON summary to OSS; return {summary: url}."""
    storage = get_storage()
    key = f"mq_results/{task_id}.json"
    body = json.dumps(summary, default=str, ensure_ascii=False).encode("utf-8")
    storage.put_bytes(key, body, content_type="application/json")
    return {"summary": storage.public_url(key)}


def publish_task_result(
    *,
    task_id: str,
    status: str,
    land_id: str | None = None,
    field_id: str | None = None,
    error: str | None = None,
    extras: dict[str, Any] | None = None,
    upload_summary_if_empty: bool = True,
) -> ResultMessage:
    """Collect OSS URLs (or upload summary) and publish ResultMessage."""
    oss_urls: dict[str, str] = {}
    if land_id:
        oss_urls.update(collect_parcel_oss_urls(land_id))

    summary = {
        "task_id": task_id,
        "status": status,
        "field_id": field_id,
        "land_id": land_id,
        "error": error,
        "extras": extras or {},
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "oss_url_keys": list(oss_urls.keys()),
    }

    if not oss_urls and upload_summary_if_empty and status == "success":
        try:
            oss_urls.update(upload_result_summary(task_id, summary))
        except Exception as exc:
            logger.warning("upload_result_summary_failed task_id=%s err=%s", task_id, exc)

    msg = ResultMessage(
        task_id=task_id,
        status="success" if status == "success" else "failed",
        oss_urls=oss_urls,
        error=error,
        field_id=field_id,
        land_id=land_id,
        extras=extras or {},
    )
    try:
        publish_result(msg)
    except Exception as exc:
        logger.error("publish_result_failed task_id=%s err=%s", task_id, exc)
        raise
    return msg


__all__ = [
    "collect_parcel_oss_urls",
    "publish_task_result",
    "upload_result_summary",
]
