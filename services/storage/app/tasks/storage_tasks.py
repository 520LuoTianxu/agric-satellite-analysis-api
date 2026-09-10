"""Celery tasks for object-storage uploads (storage queue only).

Task names stay ``app.tasks.storage.*`` for API/ingest send_task compatibility.
"""

from __future__ import annotations

import base64
import os

from openfarm_common.logging import logger
from openfarm_common.storage import get_storage

from app.worker import celery_app


def _result_payload(key: str) -> dict:
    storage = get_storage()
    return {
        "key": key,
        "public_url": storage.public_url(key),
        "backend": storage.backend,
        "uri": storage.uri_for(key),
    }


@celery_app.task(name="app.tasks.storage.upload_file", bind=True, max_retries=2)
def upload_file(
    self,
    key: str,
    path: str,
    content_type: str | None = None,
) -> dict:
    """Upload a local (shared-scratch) file to object storage."""
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"upload path missing or not a file: {path!r}")
    storage = get_storage()
    try:
        storage.upload_file(key, path, content_type=content_type)
    finally:
        # Own cleanup of shared-scratch staging dirs so ingest can leave files
        # until upload succeeds (avoids race when ingest workers die mid-wait).
        scratch_root = os.environ.get("OPENFARM_SCRATCH_DIR", "/data/scratch")
        try:
            if path.startswith(scratch_root.rstrip("/") + "/") and os.path.isfile(path):
                parent = os.path.dirname(path)
                os.unlink(path)
                try:
                    os.rmdir(parent)
                except OSError:
                    pass
        except OSError:
            pass
    logger.info(
        "storage_task_upload_file",
        key=key,
        path=path,
        backend=storage.backend,
    )
    return _result_payload(key)


@celery_app.task(name="app.tasks.storage.put_bytes", bind=True, max_retries=2)
def put_bytes(
    self,
    key: str,
    data_b64: str,
    content_type: str | None = None,
) -> dict:
    """Upload raw bytes (base64) — use only for small payloads, not COGs."""
    data = base64.b64decode(data_b64)
    storage = get_storage()
    storage.put_bytes(key, data, content_type=content_type)
    logger.info(
        "storage_task_put_bytes",
        key=key,
        bytes=len(data),
        backend=storage.backend,
    )
    return _result_payload(key)


@celery_app.task(name="app.tasks.storage.exists")
def exists(key: str) -> bool:
    return bool(get_storage().exists(key))


@celery_app.task(name="app.tasks.storage.public_url")
def public_url(key: str) -> str:
    return get_storage().public_url(key)


__all__ = ["upload_file", "put_bytes", "exists", "public_url"]
