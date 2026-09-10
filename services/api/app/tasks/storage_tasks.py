"""Celery tasks for object-storage uploads (storage queue only).

Ingest / API workers must not call ``get_storage().upload_file`` / ``put_bytes``
for pipeline artifacts. Stage large files under the shared scratch volume
(``OPENFARM_SCRATCH_DIR``, default ``/data/scratch``) and dispatch these tasks.
"""

from __future__ import annotations

import base64
import os
import shutil
import uuid
from pathlib import Path

from app.core.logging import logger
from app.core.storage import get_storage
from app.worker import celery_app

SCRATCH_DIR = Path(os.environ.get("OPENFARM_SCRATCH_DIR", "/data/scratch"))
DEFAULT_UPLOAD_TIMEOUT = float(os.environ.get("OPENFARM_STORAGE_UPLOAD_TIMEOUT", "900"))


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
    storage.upload_file(key, path, content_type=content_type)
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


def scratch_workdir(prefix: str = "job") -> Path:
    """Create a unique directory on the shared scratch volume."""
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = SCRATCH_DIR / f"{prefix}-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def upload_file_via_storage(
    key: str,
    local_path: str,
    content_type: str | None = None,
    *,
    timeout: float = DEFAULT_UPLOAD_TIMEOUT,
    already_on_scratch: bool = False,
) -> dict:
    """Stage ``local_path`` on scratch (if needed) and wait for storage upload.

    Returns ``{key, public_url, backend, uri}``.
    """
    staged: Path | None = None
    cleanup_dir: Path | None = None
    if already_on_scratch:
        path_for_worker = local_path
    else:
        cleanup_dir = scratch_workdir("upload")
        staged = cleanup_dir / Path(local_path).name
        shutil.copy2(local_path, staged)
        path_for_worker = str(staged)

    try:
        async_result = celery_app.send_task(
            "app.tasks.storage.upload_file",
            args=[key, path_for_worker, content_type],
            queue="storage",
        )
        return async_result.get(timeout=timeout)
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        if cleanup_dir is not None:
            try:
                cleanup_dir.rmdir()
            except OSError:
                pass


def put_bytes_via_storage(
    key: str,
    data: bytes,
    content_type: str | None = None,
    *,
    timeout: float = DEFAULT_UPLOAD_TIMEOUT,
    max_inline_bytes: int = 512_000,
) -> dict:
    """Upload bytes via the storage queue.

    Small payloads go as base64 on the broker; larger ones are written to
    scratch and uploaded as a file.
    """
    if len(data) <= max_inline_bytes:
        async_result = celery_app.send_task(
            "app.tasks.storage.put_bytes",
            args=[key, base64.b64encode(data).decode("ascii"), content_type],
            queue="storage",
        )
        return async_result.get(timeout=timeout)

    work = scratch_workdir("put")
    path = work / Path(key).name
    try:
        path.write_bytes(data)
        async_result = celery_app.send_task(
            "app.tasks.storage.upload_file",
            args=[key, str(path), content_type],
            queue="storage",
        )
        return async_result.get(timeout=timeout)
    finally:
        try:
            path.unlink(missing_ok=True)
            work.rmdir()
        except OSError:
            pass


__all__ = [
    "upload_file",
    "put_bytes",
    "exists",
    "public_url",
    "scratch_workdir",
    "upload_file_via_storage",
    "put_bytes_via_storage",
    "SCRATCH_DIR",
]
