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
from openfarm_common.settings import settings
from openfarm_common.storage import get_storage

logger = logging.getLogger(__name__)

# CloudAMQP practical soft limit for inline ResultMessage JSON body.
INLINE_PAYLOAD_MAX_BYTES = 100_000


def collect_parcel_oss_urls(land_id: str, *, limit: int = 50) -> dict[str, str]:
    """Build public URLs from agri.parcel_scene_products.json_oss_key."""
    if not land_id:
        return {}
    urls: dict[str, str] = {}
    session = SyncSession()
    try:
        rows = (
            session.execute(
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
            )
            .mappings()
            .all()
        )
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


def payload_byte_size(payload: dict[str, Any] | None) -> int:
    if not payload:
        return 0
    return len(json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"))


def fit_inline_payload(
    *,
    task_id: str,
    payload: dict[str, Any],
    max_bytes: int = INLINE_PAYLOAD_MAX_BYTES,
) -> tuple[dict[str, Any] | None, dict[str, str], str | None]:
    """Keep payload inline if under limit; else upload to OSS.

    Returns (inline_payload_or_none, oss_urls, error_or_none).
    """
    size = payload_byte_size(payload)
    if size <= max_bytes:
        return payload, {}, None

    kind = str(payload.get("kind") or "oversized")
    try:
        storage = get_storage()
        key = f"mq_results/{kind}/{task_id}.json"
        body = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        storage.put_bytes(key, body, content_type="application/json")
        url = storage.public_url(key)
        stub = {
            "kind": kind,
            "oss_fallback": True,
            "field_id": payload.get("field_id"),
            "rows_count": payload.get("rows_count")
            or (
                len(payload["rows"]) if isinstance(payload.get("rows"), list) else None
            ),
            "json_oss_key": key,
            "note": f"inline payload {size}B exceeded {max_bytes}B; stored on OSS",
        }
        return stub, {kind: url}, None
    except Exception as exc:
        logger.warning(
            "fit_inline_payload_oss_fallback_failed task_id=%s err=%s", task_id, exc
        )
        return (
            {
                "kind": kind,
                "truncated": True,
                "field_id": payload.get("field_id"),
                "rows_count": payload.get("rows_count"),
                "error": f"payload {size}B over {max_bytes}B and OSS fallback failed",
            },
            {},
            f"payload oversized ({size}B) and OSS fallback failed: {exc}",
        )


def scene_json_oss_key(land_id: str, date_str: str, sensor: str = "S2") -> str:
    """Stable OSS key for a lonlat_v1 parcel scene product JSON."""
    prefix = (settings.oss_prefix or "s1s2_parcel/json/").rstrip("/") + "/"
    return f"{prefix}{land_id}/{date_str}_{sensor}.json"


def upload_scene_product_json(
    *,
    land_id: str,
    date_str: str,
    sensor: str,
    product: dict[str, Any],
) -> tuple[str, str]:
    """Upload DB-ready scene product JSON; return (json_oss_key, public_url)."""
    storage = get_storage()
    key = scene_json_oss_key(land_id, date_str, sensor)
    body = json.dumps(product, default=str, ensure_ascii=False, separators=(",", ":"))
    storage.put_bytes(key, body.encode("utf-8"), content_type="application/json")
    return key, storage.public_url(key)


def publish_task_result(
    *,
    task_id: str,
    status: str,
    land_id: str | None = None,
    field_id: str | None = None,
    error: str | None = None,
    extras: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    oss_urls: dict[str, str] | None = None,
    collect_parcel_urls: bool = True,
    upload_summary_if_empty: bool = True,
) -> ResultMessage:
    """Publish ResultMessage with optional inline payload and/or OSS URLs.

    Weather/soil: pass ``payload`` (inline preferred; oversized → OSS fallback).
    Remote sensing: pass ``oss_urls`` (or enable collect_parcel_urls after
    json_oss_key is set on parcel_scene_products).
    """
    urls: dict[str, str] = dict(oss_urls or {})
    inline: dict[str, Any] | None = payload
    publish_error = error

    if inline is not None:
        inline, fallback_urls, fit_err = fit_inline_payload(
            task_id=task_id, payload=inline
        )
        urls.update(fallback_urls)
        if fit_err and status == "success":
            # Keep success if we still have a stub; attach note in extras.
            extras = {**(extras or {}), "payload_fit_error": fit_err}
            if inline and inline.get("truncated") and not fallback_urls:
                status = "failed"
                publish_error = fit_err

    if collect_parcel_urls and land_id:
        # Merge DB-keyed scene URLs (does not overwrite explicit labels).
        for label, url in collect_parcel_oss_urls(land_id).items():
            urls.setdefault(label, url)

    summary = {
        "task_id": task_id,
        "status": status,
        "field_id": field_id,
        "land_id": land_id,
        "error": publish_error,
        "extras": extras or {},
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "oss_url_keys": list(urls.keys()),
        "has_inline_payload": inline is not None,
    }

    if not urls and inline is None and upload_summary_if_empty and status == "success":
        try:
            urls.update(upload_result_summary(task_id, summary))
        except Exception as exc:
            logger.warning(
                "upload_result_summary_failed task_id=%s err=%s", task_id, exc
            )

    msg = ResultMessage(
        task_id=task_id,
        status="success" if status == "success" else "failed",
        oss_urls=urls,
        error=publish_error,
        field_id=field_id,
        land_id=land_id,
        extras=extras or {},
        payload=inline,
    )
    try:
        publish_result(msg)
    except Exception as exc:
        logger.error("publish_result_failed task_id=%s err=%s", task_id, exc)
        raise
    return msg


__all__ = [
    "INLINE_PAYLOAD_MAX_BYTES",
    "collect_parcel_oss_urls",
    "fit_inline_payload",
    "payload_byte_size",
    "publish_task_result",
    "scene_json_oss_key",
    "upload_result_summary",
    "upload_scene_product_json",
]
