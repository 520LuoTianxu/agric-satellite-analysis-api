"""Client helpers to dispatch uploads onto the storage Celery queue."""

from __future__ import annotations

import base64
import os
import shutil
import uuid
from pathlib import Path

from openfarm_common.celery_app import celery_client
from openfarm_common.settings import settings

SCRATCH_DIR = Path(
    os.environ.get("OPENFARM_SCRATCH_DIR", settings.openfarm_scratch_dir)
)
DEFAULT_UPLOAD_TIMEOUT = float(
    os.environ.get(
        "OPENFARM_STORAGE_UPLOAD_TIMEOUT",
        str(settings.openfarm_storage_upload_timeout),
    )
)


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
    """Stage ``local_path`` on scratch (if needed) and wait for storage upload."""
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
        async_result = celery_client.send_task(
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
    """Upload bytes via the storage queue."""
    if len(data) <= max_inline_bytes:
        async_result = celery_client.send_task(
            "app.tasks.storage.put_bytes",
            args=[key, base64.b64encode(data).decode("ascii"), content_type],
            queue="storage",
        )
        return async_result.get(timeout=timeout)

    work = scratch_workdir("put")
    path = work / Path(key).name
    try:
        path.write_bytes(data)
        async_result = celery_client.send_task(
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
    "scratch_workdir",
    "upload_file_via_storage",
    "put_bytes_via_storage",
    "SCRATCH_DIR",
    "DEFAULT_UPLOAD_TIMEOUT",
]
