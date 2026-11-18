"""Band-level download concurrency nested under the scene ThreadPool.

``INGEST_SCENE_MAX_WORKERS`` overlaps dates inside one Celery task.
``INGEST_BAND_MAX_WORKERS`` (default 8) is the process-wide cap on
concurrent windowed GDAL/rasterio reads. Each scene still uses a small
ThreadPool so S2 multi-band jobs are not strictly serial.

Nested math: per-scene pool size is
``min(n_bands, cap, max(1, (cap * 2) // scene_workers))``.
With the default cap of 8 and 8 scene workers that is 2 band threads
per scene. Peak GDAL opens are still ``cap`` via a process-wide
semaphore. A lone scene uses ``min(cap, n_bands)``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Generic, Mapping, TypeVar
from urllib.parse import urlsplit

from app.core.windowed_gdal import log_timing

_log = logging.getLogger("openfarm.ingest.band_parallel")


def _band_log(event: str, *, level: str = "info", **kwargs) -> None:
    """Prefer structlog in the ingest worker; stdlib fallback for unit tests."""
    try:
        import structlog
    except ImportError:
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
        getattr(_log, level, _log.info)("%s %s", event, extra)
        return
    logger = structlog.get_logger()
    getattr(logger, level, logger.info)(event, **kwargs)


_DEFAULT_BAND_WORKERS = 8
_gdal_limit_lock = threading.Lock()
_gdal_limit: threading.BoundedSemaphore | None = None
_gdal_limit_n = 0
_read_local = threading.local()

K = TypeVar("K")
V = TypeVar("V")
R = TypeVar("R")


@dataclass(frozen=True)
class BandReadResult(Generic[R]):
    """波段读取结果及阶段耗时，供日志拆分远端 I/O 与本地重投影。"""

    value: R
    io_ms: int
    reproject_ms: int


def band_max_workers() -> int:
    """Process-wide cap on concurrent windowed band reads."""
    raw = os.environ.get("INGEST_BAND_MAX_WORKERS")
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_BAND_WORKERS
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_BAND_WORKERS


def band_read_max_attempts() -> int:
    """应用层波段读取总尝试次数；默认三次并限制异常配置的影响范围。"""
    raw = os.environ.get("BAND_READ_MAX_ATTEMPTS", "3")
    try:
        return min(10, max(1, int(raw)))
    except (TypeError, ValueError):
        return 3


def _retry_delays() -> tuple[float, ...]:
    raw = os.environ.get("BAND_READ_RETRY_DELAYS_SEC", "1,3")
    values: list[float] = []
    for part in str(raw).split(","):
        try:
            values.append(max(0.0, float(part.strip())))
        except (TypeError, ValueError):
            continue
    return tuple(values) or (1.0, 3.0)


def _slow_read_ms() -> int:
    raw = os.environ.get("BAND_READ_SLOW_MS", "10000")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 10000


def _host_from_value(value: object) -> str | None:
    """只提取主机名写日志，避免将 SAS 查询串等敏感信息写入日志。"""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("/vsis3/"):
        return value.removeprefix("/vsis3/").split("/", 1)[0] or None
    parsed = urlsplit(value)
    if parsed.hostname:
        return parsed.hostname
    return "local"


def _is_retryable_band_error(exc: Exception) -> bool:
    """参数或代码错误立即暴露；远端 I/O 类异常交给应用层有限重试。"""
    return not isinstance(exc, (AssertionError, KeyError, TypeError, ValueError))


def _is_timeout_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text


def band_read_timeout_sec() -> float:
    """Wall-clock seconds for one band-read attempt. Default 20; ``<= 0`` disables the watchdog."""
    raw = os.environ.get("BAND_READ_TIMEOUT_SEC", "20")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 20.0
    if value <= 0:
        return 0.0
    return value


def _unwrap_thread_result(box: dict[str, object]) -> object:
    if "exc" in box:
        raise box["exc"]  # type: ignore[misc]
    return box["value"]


def _spawn_timed_call(
    fn: Callable[[], object], timeout_sec: float
) -> tuple[threading.Thread, dict[str, object], bool]:
    """Run ``fn`` in a daemon thread; return (thread, box, timed_out)."""
    box: dict[str, object] = {}
    done = threading.Event()

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["exc"] = exc
        finally:
            done.set()

    thread = threading.Thread(
        target=_target, daemon=True, name="ingest-band-watchdog"
    )
    thread.start()
    if not done.wait(timeout_sec):
        return thread, box, True
    thread.join(timeout=1)
    return thread, box, False


def _run_fn_holding_slot(fn: Callable[[], object], timeout_sec: float) -> object:
    """Hold a GDAL slot while ``fn`` runs.

    On wall-clock timeout the blocked GDAL/curl call cannot be killed, so the
    slot stays reserved until that thread finishes and a reaper releases it.
    That avoids letting a new read overlap an in-flight GDAL call.
    """
    if getattr(_read_local, "active", False):
        if timeout_sec <= 0:
            return fn()
        thread, box, timed_out = _spawn_timed_call(fn, timeout_sec)
        if timed_out:
            raise TimeoutError(f"band read exceeded {timeout_sec:g}s")
        return _unwrap_thread_result(box)

    if timeout_sec <= 0:
        with gdal_read_slot():
            return fn()

    slot = _gdal_band_limit()
    slot.acquire()
    _read_local.active = True
    released = False

    def _release() -> None:
        nonlocal released
        if released:
            return
        released = True
        _read_local.active = False
        slot.release()

    try:
        thread, box, timed_out = _spawn_timed_call(fn, timeout_sec)
        if timed_out:
            def _reap() -> None:
                thread.join()
                slot.release()

            threading.Thread(
                target=_reap, daemon=True, name="ingest-band-slot-reap"
            ).start()
            released = True
            _read_local.active = False
            raise TimeoutError(f"band read exceeded {timeout_sec:g}s")
        try:
            return _unwrap_thread_result(box)
        finally:
            _release()
    except Exception:
        if not released:
            _release()
        raise


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


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
    # 场景线程进入计算或发布阶段时允许少量线程超配，真正 GDAL 打开数仍受信号量约束。
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


@contextmanager
def gdal_read_slot():
    """统一限制远程栅格读取；同线程嵌套复用名额，避免 cap=1 时死锁。"""
    if getattr(_read_local, "active", False):
        yield
        return
    with _gdal_band_limit():
        _read_local.active = True
        try:
            yield
        finally:
            _read_local.active = False


def run_parallel_band_jobs(
    items: dict[K, V],
    fn: Callable[[K, V], R | BandReadResult[R]],
    *,
    scene_workers: int = 1,
    log_context: Mapping[str, object] | None = None,
    host_resolver: Callable[[V], str | None] | None = None,
    retry_if: Callable[[Exception], bool] | None = None,
) -> dict[K, R]:
    """并行执行同景波段读取，并在全局 GDAL cap 内完成有限应用层重试。

    每次尝试离开 ``gdal_read_slot`` 后才退避，因此失败波段不会在睡眠时占住
    进程级读取名额。返回字典保持 ``items`` 的原始顺序。
    """
    if not items:
        return {}

    n = len(items)
    workers = effective_band_workers(n, scene_workers)
    cap = band_max_workers()
    context = {
        "job_id": None,
        "scene_id": None,
        "date": None,
        "sensor": None,
        **dict(log_context or {}),
    }
    max_attempts = band_read_max_attempts()
    timeout_sec = band_read_timeout_sec()
    retry_delays = _retry_delays()
    resolve_host = host_resolver or _host_from_value
    attempt_stats: list[dict[str, object]] = []
    stats_lock = threading.Lock()
    _band_log(
        "band_parallel_start",
        **context,
        bands=n,
        workers=workers,
        scene_workers=max(1, int(scene_workers)),
        gdal_cap=cap,
        max_attempts=max_attempts,
    )

    def _call(key: K, value: V) -> R:
        thread = threading.current_thread().name
        host = resolve_host(value)
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            wait_ms = 0
            read_ms = 0
            io_ms = 0
            reproject_ms = 0
            t0 = time.perf_counter()
            t_wait = time.perf_counter()
            try:
                # 退避位于槽位之外，避免失败请求睡眠时占住全局 GDAL 名额。
                # 墙钟超时后无法打断已在跑的 GDAL/HTTP，名额由收割线程延后释放。
                wait_ms = 0
                read_ms = 0
                timing: dict[str, int] = {}

                def _read() -> R | BandReadResult[R]:
                    timing["wait_ms"] = int((time.perf_counter() - t_wait) * 1000)
                    t_read = time.perf_counter()
                    try:
                        return fn(key, value)
                    finally:
                        timing["read_ms"] = int((time.perf_counter() - t_read) * 1000)

                raw_result = _run_fn_holding_slot(_read, timeout_sec)
                wait_ms = timing.get("wait_ms", int((time.perf_counter() - t_wait) * 1000))
                read_ms = timing.get("read_ms", 0)
                if isinstance(raw_result, BandReadResult):
                    result = raw_result.value
                    io_ms = max(0, int(raw_result.io_ms))
                    reproject_ms = max(0, int(raw_result.reproject_ms))
                else:
                    result = raw_result
                    # 兼容尚未拆分阶段耗时的读取器，至少保留完整读取耗时。
                    io_ms = read_ms
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                row = {
                    "band": str(key),
                    "attempt": attempt,
                    "outcome": "success",
                    "wait_ms": wait_ms,
                    "io_ms": io_ms,
                    "reproject_ms": reproject_ms,
                    "read_ms": read_ms,
                    "elapsed_ms": elapsed_ms,
                }
                with stats_lock:
                    attempt_stats.append(row)
                _band_log(
                    "band_read_attempt_done",
                    level="warning" if read_ms >= _slow_read_ms() else "info",
                    **context,
                    band=str(key),
                    host=host,
                    attempt=attempt,
                    outcome="success",
                    thread=thread,
                    elapsed_ms=elapsed_ms,
                    wait_ms=wait_ms,
                    io_ms=io_ms,
                    reproject_ms=reproject_ms,
                    read_ms=read_ms,
                )
                return result
            except Exception as exc:
                last_exc = exc
                read_ms = read_ms or int((time.perf_counter() - t_wait) * 1000)
                io_ms = read_ms
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                retryable = (retry_if or _is_retryable_band_error)(exc)
                timed_out = _is_timeout_error(exc)
                wall_timeout = isinstance(exc, TimeoutError)
                # Wall-clock timeout leaves a blocked GDAL thread behind; do not
                # retry and stack more hung reads. Finished curl timeouts still retry.
                will_retry = retryable and attempt < max_attempts and not wall_timeout
                outcome = "retry" if will_retry else "failed"
                row = {
                    "band": str(key),
                    "attempt": attempt,
                    "outcome": outcome,
                    "wait_ms": wait_ms,
                    "io_ms": io_ms,
                    "reproject_ms": 0,
                    "read_ms": read_ms,
                    "elapsed_ms": elapsed_ms,
                    "timed_out": timed_out,
                }
                with stats_lock:
                    attempt_stats.append(row)
                _band_log(
                    "band_read_attempt_done",
                    level="warning",
                    **context,
                    band=str(key),
                    host=host,
                    attempt=attempt,
                    outcome=outcome,
                    timed_out=timed_out,
                    thread=thread,
                    elapsed_ms=elapsed_ms,
                    wait_ms=wait_ms,
                    io_ms=io_ms,
                    reproject_ms=0,
                    read_ms=read_ms,
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                    timeout_sec=timeout_sec,
                )
                if not will_retry:
                    break
                delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
                if delay:
                    time.sleep(delay)

        assert last_exc is not None
        raise last_exc

    def _log_summary(wall_ms: int, outcome: str) -> None:
        with stats_lock:
            rows = list(attempt_stats)
        reads = [int(row["read_ms"]) for row in rows]
        timeout_retries = sum(
            1
            for row in rows
            if row.get("timed_out") and row.get("outcome") == "retry"
        )
        slow_bands = {
            str(row["band"])
            for row in rows
            if int(row["read_ms"]) >= _slow_read_ms()
        }
        _band_log(
            "band_parallel_done",
            level="warning" if outcome == "failed" or slow_bands else "info",
            **context,
            outcome=outcome,
            bands=n,
            workers=workers,
            wall_ms=wall_ms,
            band_read_p50_ms=_percentile(reads, 0.50),
            band_read_p95_ms=_percentile(reads, 0.95),
            band_read_max_ms=max(reads, default=0),
            timeout_retries=timeout_retries,
            slow_band_count=len(slow_bands),
        )
        log_timing(
            "read_window",
            sensor=context.get("sensor"),
            job_id=context.get("job_id"),
            scene_id=context.get("scene_id"),
            date=context.get("date"),
            wall_ms=wall_ms,
            bands=n,
            workers=workers,
            outcome=outcome,
            band_read_p50_ms=_percentile(reads, 0.50),
            band_read_p95_ms=_percentile(reads, 0.95),
            band_read_max_ms=max(reads, default=0),
            timeout_retries=timeout_retries,
        )

    t0 = time.perf_counter()
    results: dict[K, R] = {}
    try:
        if n == 1 or workers == 1:
            for key, value in items.items():
                try:
                    results[key] = _call(key, value)
                except Exception as exc:
                    raise RuntimeError(f"band read failed: {key}") from exc
        else:
            errors: list[tuple[K, Exception]] = []
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="ingest-band"
            ) as pool:
                futs = {
                    pool.submit(_call, key, value): key for key, value in items.items()
                }
                for fut in as_completed(futs):
                    key = futs[fut]
                    try:
                        results[key] = fut.result()
                    except Exception as exc:
                        errors.append((key, exc))
            if errors:
                key, exc = errors[0]
                raise RuntimeError(f"band read failed: {key}") from exc
    except Exception:
        _log_summary(int((time.perf_counter() - t0) * 1000), "failed")
        raise

    wall_ms = int((time.perf_counter() - t0) * 1000)
    _log_summary(wall_ms, "success")
    return {key: results[key] for key in items}


__all__ = [
    "BandReadResult",
    "band_max_workers",
    "band_read_max_attempts",
    "band_read_timeout_sec",
    "effective_band_workers",
    "gdal_read_slot",
    "reset_band_gdal_limit",
    "run_parallel_band_jobs",
]
