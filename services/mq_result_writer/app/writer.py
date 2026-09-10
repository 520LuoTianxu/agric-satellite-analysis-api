"""Persist ResultMessage payloads into agri.mq_task_results."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import text

from openfarm_common.database_sync import SyncSession
from openfarm_common.mq_schemas import ResultMessage
from openfarm_common.storage import get_storage
from openfarm_common.settings import settings

logger = logging.getLogger(__name__)


def _download_json(url: str) -> Any | None:
    """HTTP GET JSON from a public OSS URL, or storage.get for our bucket keys."""
    try:
        parsed = urlparse(url)
        # Prefer authenticated storage read when URL maps to our bucket
        bucket = settings.oss_bucket
        if bucket and parsed.netloc.startswith(f"{bucket}."):
            key = parsed.path.lstrip("/")
            if key:
                raw = get_storage().get_bytes(key)
                return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning("storage_get_failed url=%s err=%s", url[:120], exc)

    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("http_get_failed url=%s err=%s", url[:120], exc)
        return None


def upsert_mq_task_result(msg: ResultMessage, payloads: dict[str, Any]) -> None:
    session = SyncSession()
    try:
        session.execute(
            text(
                """
                INSERT INTO agri.mq_task_results (
                    task_id, status, oss_urls, payload, error,
                    field_id, land_id, finished_at, updated_at
                ) VALUES (
                    :task_id, :status, CAST(:oss_urls AS jsonb), CAST(:payload AS jsonb),
                    :error, :field_id, :land_id, :finished_at, now()
                )
                ON CONFLICT (task_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    oss_urls = EXCLUDED.oss_urls,
                    payload = EXCLUDED.payload,
                    error = EXCLUDED.error,
                    field_id = COALESCE(EXCLUDED.field_id, agri.mq_task_results.field_id),
                    land_id = COALESCE(EXCLUDED.land_id, agri.mq_task_results.land_id),
                    finished_at = EXCLUDED.finished_at,
                    updated_at = now()
                """
            ),
            {
                "task_id": msg.task_id,
                "status": msg.status,
                "oss_urls": json.dumps(msg.oss_urls, ensure_ascii=False),
                "payload": json.dumps(payloads, default=str, ensure_ascii=False),
                "error": msg.error,
                "field_id": msg.field_id,
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

    downloaded: dict[str, Any] = {}
    for label, url in (msg.oss_urls or {}).items():
        if not url or not str(url).startswith("http"):
            downloaded[label] = {"skipped": True, "raw": url}
            continue
        data = _download_json(str(url))
        downloaded[label] = data if data is not None else {"download_failed": True, "url": url}

    upsert_mq_task_result(msg, downloaded)
    logger.info(
        "mq_result_written task_id=%s status=%s urls=%s retry=%s",
        msg.task_id,
        msg.status,
        list(msg.oss_urls.keys()),
        meta.get("retry_count"),
    )
