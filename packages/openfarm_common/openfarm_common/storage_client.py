"""Client helpers to dispatch uploads onto the storage Celery queue.

When invoked from inside an ingest (or any) Celery task, upload via the local
storage backend instead of ``send_task(...).get()`` — Celery forbids joining
another task's result from within a task (``Never call result.get() within a
task!``). Outside a task context (e.g. API request handlers), keep the
storage-worker path so uploads stay on the storage queue.
"""

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


def _cleanup_staged(staged: Path | None, cleanup_dir: Path | None) -> None:
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


def _running_in_celery_task() -> bool:
    """True when called from inside a Celery worker task body."""
    try:
        from celery import current_task

        return (
            current_task is not None
            and getattr(current_task, "request", None) is not None
            and current_task.request.id is not None
        )
    except Exception:
        return False


def _result_payload(key: str) -> dict:
    from openfarm_common.storage import get_storage

    storage = get_storage()
    return {
        "key": key,
        "public_url": storage.public_url(key),
        "backend": storage.backend,
        "uri": storage.uri_for(key),
    }


def _upload_file_direct(
    key: str,
    local_path: str,
    content_type: str | None = None,
) -> dict:
    """Upload via the local storage backend (safe inside Celery tasks)."""
    from openfarm_common.storage import get_storage

    if not local_path or not os.path.isfile(local_path):
        raise FileNotFoundError(f"upload path missing or not a file: {local_path!r}")
    storage = get_storage()
    storage.upload_file(key, local_path, content_type=content_type)
    return _result_payload(key)


def _put_bytes_direct(
    key: str,
    data: bytes,
    content_type: str | None = None,
) -> dict:
    """Put bytes via the local storage backend (safe inside Celery tasks)."""
    from openfarm_common.storage import get_storage

    storage = get_storage()
    storage.put_bytes(key, data, content_type=content_type)
    return _result_payload(key)


def upload_file_via_storage(
    key: str,
    local_path: str,
    content_type: str | None = None,
    *,
    timeout: float = DEFAULT_UPLOAD_TIMEOUT,
    already_on_scratch: bool = False,
) -> dict:
    """Stage ``local_path`` on scratch (if needed) and wait for storage upload.

    Inside a Celery task: upload directly via ``get_storage()`` (no cross-worker
    ``AsyncResult.get()``). Outside a task: dispatch to the storage queue and
    join.

    Scratch files are removed only after a successful upload. On timeout /
    worker death, leave the staged file for the storage worker (or later GC)
    so we do not race ``FileNotFoundError`` on the storage queue.
    """
    if _running_in_celery_task():
        # Direct path — no staging needed; caller owns local_path lifecycle.
        return _upload_file_direct(key, local_path, content_type)

    staged: Path | None = None
    cleanup_dir: Path | None = None
    if already_on_scratch:
        path_for_worker = local_path
    else:
        cleanup_dir = scratch_workdir("upload")
        staged = cleanup_dir / Path(local_path).name
        shutil.copy2(local_path, staged)
        path_for_worker = str(staged)

    async_result = celery_client.send_task(
        "app.tasks.storage.upload_file",
        args=[key, path_for_worker, content_type],
        queue="storage",
    )
    try:
        result = async_result.get(timeout=timeout)
    except Exception:
        # Do not delete staged path — storage may still be running / retrying.
        raise
    else:
        # Storage task also unlinks scratch paths; this is best-effort local GC.
        _cleanup_staged(staged, cleanup_dir)
        return result


def put_bytes_via_storage(
    key: str,
    data: bytes,
    content_type: str | None = None,
    *,
    timeout: float = DEFAULT_UPLOAD_TIMEOUT,
    max_inline_bytes: int = 512_000,
) -> dict:
    """Upload bytes via the storage queue (or directly when inside a task)."""
    if _running_in_celery_task():
        return _put_bytes_direct(key, data, content_type)

    if len(data) <= max_inline_bytes:
        async_result = celery_client.send_task(
            "app.tasks.storage.put_bytes",
            args=[key, base64.b64encode(data).decode("ascii"), content_type],
            queue="storage",
        )
        return async_result.get(timeout=timeout)

    work = scratch_workdir("put")
    path = work / Path(key).name
    path.write_bytes(data)
    async_result = celery_client.send_task(
        "app.tasks.storage.upload_file",
        args=[key, str(path), content_type],
        queue="storage",
    )
    try:
        result = async_result.get(timeout=timeout)
    except Exception:
        raise
    else:
        _cleanup_staged(path, work)
        return result


__all__ = [
    "scratch_workdir",
    "upload_file_via_storage",
    "put_bytes_via_storage",
    "SCRATCH_DIR",
    "DEFAULT_UPLOAD_TIMEOUT",
]
