"""Band-level download concurrency nested under the scene ThreadPool.

``INGEST_SCENE_MAX_WORKERS`` overlaps dates inside one Celery task.
``INGEST_BAND_MAX_WORKERS`` (default 16) is the process-wide cap on
concurrent windowed GDAL/rasterio reads. Each scene still uses a small
ThreadPool so S2 multi-band jobs (agri optical is typically 7 unique
bands) are not strictly serial.

Nested math: per-scene pool size is
``min(n_bands, cap, max(1, (cap * 2) // scene_workers))``.
With the default cap of 16 and 8 scene workers that is 4 band threads
per scene. Peak GDAL opens are still ``cap`` via a process-wide
semaphore, so 8 scenes x 7 bands does not become 56 simultaneous
reads. A lone scene (scene_workers=1) uses ``min(cap, n_bands)``.

Each worker must open its own ``rasterio.Env()`` (see ``read_band_windowed``).
Keep ``GDAL_NUM_THREADS=1`` so GDAL does not spawn another pool under these
threads.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, TypeVar

_log = logging.getLogger("openfarm.ingest.band_parallel")


def _band_log(event: str, **kwargs) -> None:
    """Prefer structlog in the ingest worker; stdlib fallback for unit tests."""
    try:
        import structlog
    except ImportError:
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
        _log.info("%s %s", event, extra)
        return
    structlog.get_logger().info(event, **kwargs)


_DEFAULT_BAND_WORKERS = 16
_gdal_limit_lock = threading.Lock()
_gdal_limit: threading.BoundedSemaphore | None = None
_gdal_limit_n = 0

K = TypeVar("K")
V = TypeVar("V")
R = TypeVar("R")


def band_max_workers() -> int:
    """Process-wide cap on concurrent windowed band reads."""
    raw = os.environ.get("INGEST_BAND_MAX_WORKERS")
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_BAND_WORKERS
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_BAND_WORKERS


def effective_band_workers(n_bands: int, scene_workers: int = 1) -> int:
    """Per-scene ThreadPool size nested under the scene pool.

    Does not raise the process-wide GDAL cap; that is ``band_max_workers()``.
    """
    n_bands = max(1, int(n_bands))
    cap = band_max_workers()
    try:
        scene_workers = max(1, int(scene_workers))
    except (TypeError, ValueError):
        scene_workers = 1
    if n_bands == 1:
        return 1
    per_scene = min(n_bands, cap)
    if scene_workers <= 1:
        return per_scene
    # Modest thread oversubscribe (2x the GDAL cap) so scenes that have
    # moved on to compute/upsert do not starve remaining band reads.
    thread_budget = cap * 2
    nested = max(1, thread_budget // scene_workers)
    return max(1, min(per_scene, nested))


def reset_band_gdal_limit() -> None:
    """Drop the cached semaphore (tests that change the env cap)."""
    global _gdal_limit, _gdal_limit_n
    with _gdal_limit_lock:
        _gdal_limit = None
        _gdal_limit_n = 0


def _gdal_band_limit() -> threading.BoundedSemaphore:
    global _gdal_limit, _gdal_limit_n
    n = band_max_workers()
    with _gdal_limit_lock:
        if _gdal_limit is None or _gdal_limit_n != n:
            _gdal_limit = threading.BoundedSemaphore(n)
            _gdal_limit_n = n
        return _gdal_limit


def run_parallel_band_jobs(
    items: dict[K, V],
    fn: Callable[[K, V], R],
    *,
    scene_workers: int = 1,
) -> dict[K, R]:
    """Run ``fn(key, value)`` for each item, overlapping I/O when useful.

    Acquires the process-wide GDAL semaphore around every ``fn`` call, serial
    or pooled. Returns a dict with the same key order as ``items``.
    """
    if not items:
        return {}

    n = len(items)
    workers = effective_band_workers(n, scene_workers)
    cap = band_max_workers()
    _band_log(
        "band_parallel_start",
        bands=n,
        workers=workers,
        scene_workers=max(1, int(scene_workers)),
        gdal_cap=cap,
    )

    def _call(key: K, value: V) -> R:
        thread = threading.current_thread().name
        _band_log("band_read_start", band=str(key), thread=thread)
        t0 = time.perf_counter()
        wait_ms = 0
        read_ms = 0
        try:
            sem = _gdal_band_limit()
            t_wait = time.perf_counter()
            sem.acquire()
            wait_ms = int((time.perf_counter() - t_wait) * 1000)
            try:
                t_read = time.perf_counter()
                return fn(key, value)
            finally:
                read_ms = int((time.perf_counter() - t_read) * 1000)
                sem.release()
        finally:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            _band_log(
                "band_read_done",
                band=str(key),
                thread=thread,
                elapsed_ms=elapsed_ms,
                wait_ms=wait_ms,
                read_ms=read_ms,
            )

    t0 = time.perf_counter()
    results: dict[K, R] = {}
    if n == 1 or workers == 1:
        for key, value in items.items():
            results[key] = _call(key, value)
    else:
        errors: list[tuple[K, Exception]] = []
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="ingest-band"
        ) as pool:
            futs = {pool.submit(_call, key, value): key for key, value in items.items()}
            for fut in as_completed(futs):
                key = futs[fut]
                try:
                    results[key] = fut.result()
                except Exception as exc:
                    errors.append((key, exc))
        if errors:
            key, exc = errors[0]
            raise RuntimeError(f"band read failed: {key}") from exc

    wall_ms = int((time.perf_counter() - t0) * 1000)
    _band_log(
        "band_parallel_done",
        bands=n,
        workers=workers,
        wall_ms=wall_ms,
    )
    return {key: results[key] for key in items}
