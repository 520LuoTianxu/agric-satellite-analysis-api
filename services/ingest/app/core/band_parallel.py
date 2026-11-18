"""Band-level download concurrency nested under the scene ThreadPool.

``INGEST_SCENE_MAX_WORKERS`` overlaps dates inside one Celery task.
``INGEST_BAND_MAX_WORKERS`` (default 8) is the process-wide cap on
concurrent windowed COG reads. Each scene still uses a small ThreadPool
so S2 multi-band jobs are not strictly serial.

Nested math: per-scene pool size is
``min(n_bands, cap, max(1, (cap * 2) // scene_workers))``.
With the default cap of 8 and 8 scene workers that is 2 band threads
per scene. Peak in-flight reads are still ``cap`` via a process-wide
FIFO slot gate. A lone scene uses ``min(cap, n_bands)``.

Slot acquire has its own wait timeout (``GDAL_SLOT_WAIT_SEC``). Timed-out
Range/HTTP work is cancelled and the slot is released immediately so a
later 1-2s read can start. A scene that already has two band timeouts
(or that exhausts ``BAND_SCENE_BUDGET_SEC``) skips remaining bands.
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

from app.core.cog_http import (
    BandReadTimeout,
    GdalSlotWaitTimeout,
    SceneFailFastError,
    band_attempt_context,
    band_read_timeout_sec,
    band_scene_budget_sec,
    band_scene_max_timeouts,
    gdal_slot_wait_sec,
)
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
_gdal_limit: "_GdalSlotGate | None" = None
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
    return not isinstance(
        exc,
        (AssertionError, KeyError, TypeError, ValueError, SceneFailFastError),
    )


def _is_timeout_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return isinstance(exc, TimeoutError) or "timeout" in text or "timed out" in text


def _is_wall_timeout(exc: Exception) -> bool:
    """Hard I/O timeout of a started read; does not include slot-wait timeouts."""
    return isinstance(exc, (BandReadTimeout, TimeoutError)) and not isinstance(
        exc, GdalSlotWaitTimeout
    )


def _unwrap_thread_result(box: dict[str, object]) -> object:
    if "exc" in box:
        raise box["exc"]  # type: ignore[misc]
    return box["value"]


def _spawn_timed_call(
    fn: Callable[[], object], timeout_sec: float, cancel: threading.Event | None = None
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
    if timeout_sec <= 0:
        done.wait()
        thread.join(timeout=1)
        return thread, box, False
    if not done.wait(timeout_sec):
        if cancel is not None:
            cancel.set()
        return thread, box, True
    thread.join(timeout=1)
    return thread, box, False


def _run_fn_holding_slot(fn: Callable[[], object], timeout_sec: float) -> tuple[object, int]:
    """Acquire a slot (with wait timeout), run ``fn``, and return ``(value, wait_ms)``.

    The slot is released as soon as the parent declares a timeout or the call
    returns. Hung work is cancelled via the attempt context; a brief over-cap
    is possible only if a C-level decode ignores cancel, which is no longer the
    HTTP path.
    """
    wait_timeout = gdal_slot_wait_sec()
    gate = _gdal_slot_gate()
    t_wait = time.perf_counter()

    def _wait_ms() -> int:
        return int((time.perf_counter() - t_wait) * 1000)

    if getattr(_read_local, "active", False):
        wait_ms = _wait_ms()
        if timeout_sec <= 0:
            return fn(), wait_ms
        with band_attempt_context(timeout_sec) as ctx:
            thread, box, timed_out = _spawn_timed_call(fn, timeout_sec, ctx.cancel)
            if timed_out:
                ctx.cancel.set()
                ctx.close_inflight()
                _band_log(
                    "band_read_killed",
                    level="warning",
                    timeout_sec=timeout_sec,
                    wait_ms=wait_ms,
                    io_ms=int(timeout_sec * 1000),
                )
                raise BandReadTimeout(f"band read exceeded {timeout_sec:g}s")
            return _unwrap_thread_result(box), wait_ms

    if not gate.acquire_live(wait_timeout):
        wait_ms = _wait_ms()
        cap, live, waiting = gate.snapshot()
        _band_log(
            "gdal_slot_wait_timeout",
            level="warning",
            wait_ms=wait_ms,
            cap=cap,
            live=live,
            waiting=waiting,
            timeout_sec=wait_timeout,
        )
        raise GdalSlotWaitTimeout(
            f"no band-read slot free within {wait_timeout:g}s "
            f"(cap={cap} live={live} waiting={waiting})"
        )
    wait_ms = _wait_ms()
    _read_local.active = True
    released = False

    def _release_live() -> None:
        nonlocal released
        if released:
            return
        released = True
        _read_local.active = False
        gate.release_live()

    try:
        if timeout_sec <= 0:
            try:
                return fn(), wait_ms
            finally:
                _release_live()

        with band_attempt_context(timeout_sec) as ctx:
            thread, box, timed_out = _spawn_timed_call(fn, timeout_sec, ctx.cancel)
            if timed_out:
                ctx.cancel.set()
                ctx.close_inflight()
                _release_live()
                _band_log(
                    "band_read_killed",
                    level="warning",
                    timeout_sec=timeout_sec,
                    wait_ms=wait_ms,
                    io_ms=int(timeout_sec * 1000),
                )
                # Do not join here: the slot is already free for the next waiter.
                raise BandReadTimeout(f"band read exceeded {timeout_sec:g}s")
            try:
                return _unwrap_thread_result(box), wait_ms
            finally:
                _release_live()
    except Exception:
        if not released:
            _release_live()
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
    # 场景线程进入计算或发布阶段时允许少量线程超配，真正打开数仍受信号量约束。
    thread_budget = cap * 2
    nested = max(1, thread_budget // scene_workers)
    return max(1, min(per_scene, nested))


def reset_band_gdal_limit() -> None:
    """Drop the cached slot gate (tests that change the env cap)."""
    global _gdal_limit, _gdal_limit_n
    with _gdal_limit_lock:
        _gdal_limit = None
        _gdal_limit_n = 0


class _GdalSlotGate:
    """Process-wide FIFO cap on concurrent windowed band reads.

    Waiters are served in condition-queue order. Acquire fails if no slot
    is free within ``GDAL_SLOT_WAIT_SEC``, so a later job is not stuck
    behind a stalled queue forever.
    """

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._cond = threading.Condition()
        self.live = 0
        self.waiting = 0

    def acquire_live(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            self.waiting += 1
            try:
                while self.live >= self.cap:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(remaining)
                self.live += 1
                return True
            finally:
                self.waiting -= 1

    def release_live(self) -> None:
        with self._cond:
            self.live -= 1
            self._cond.notify_all()

    def snapshot(self) -> tuple[int, int, int]:
        with self._cond:
            return self.cap, self.live, self.waiting


def _gdal_slot_gate() -> _GdalSlotGate:
    global _gdal_limit, _gdal_limit_n
    n = band_max_workers()
    with _gdal_limit_lock:
        if _gdal_limit is None or _gdal_limit_n != n:
            _gdal_limit = _GdalSlotGate(n)
            _gdal_limit_n = n
        return _gdal_limit


def gdal_slot_snapshot() -> tuple[int, int, int]:
    """Return ``(cap, live, waiting)`` for tests and logs."""
    return _gdal_slot_gate().snapshot()


@contextmanager
def gdal_read_slot(timeout: float | None = None):
    """统一限制远程栅格读取；同线程嵌套复用名额，避免 cap=1 时死锁。

    Waiting for a free slot longer than ``timeout`` (default
    ``GDAL_SLOT_WAIT_SEC``) raises ``GdalSlotWaitTimeout``.
    """
    if getattr(_read_local, "active", False):
        yield
        return
    wait = gdal_slot_wait_sec() if timeout is None else max(0.0, float(timeout))
    gate = _gdal_slot_gate()
    t0 = time.perf_counter()
    if not gate.acquire_live(wait):
        wait_ms = int((time.perf_counter() - t0) * 1000)
        cap, live, waiting = gate.snapshot()
        _band_log(
            "gdal_slot_wait_timeout",
            level="warning",
            wait_ms=wait_ms,
            cap=cap,
            live=live,
            waiting=waiting,
            timeout_sec=wait,
        )
        raise GdalSlotWaitTimeout(
            f"no band-read slot free within {wait:g}s "
            f"(cap={cap} live={live} waiting={waiting})"
        )
    _read_local.active = True
    try:
        yield
    finally:
        _read_local.active = False
        gate.release_live()


class _SceneGuard:
    """Fail-fast remaining bands after N timeouts or a scene wall budget."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = time.monotonic()
        self.timeouts = 0
        self.skipped = 0
        self.reason: str | None = None

    def note_timeout(self) -> None:
        with self._lock:
            self.timeouts += 1
            max_timeouts = band_scene_max_timeouts()
            if max_timeouts > 0 and self.timeouts >= max_timeouts and self.reason is None:
                self.reason = "timeouts"

    def should_skip(self) -> str | None:
        with self._lock:
            if self.reason is not None:
                return self.reason
            max_timeouts = band_scene_max_timeouts()
            if max_timeouts > 0 and self.timeouts >= max_timeouts:
                self.reason = "timeouts"
                return self.reason
            budget = band_scene_budget_sec()
            if budget > 0 and (time.monotonic() - self.started) >= budget:
                self.reason = "budget"
                return self.reason
            return None

    def note_skip(self) -> None:
        with self._lock:
            self.skipped += 1


def run_parallel_band_jobs(
    items: dict[K, V],
    fn: Callable[[K, V], R | BandReadResult[R]],
    *,
    scene_workers: int = 1,
    log_context: Mapping[str, object] | None = None,
    host_resolver: Callable[[V], str | None] | None = None,
    retry_if: Callable[[Exception], bool] | None = None,
) -> dict[K, R]:
    """并行执行同景波段读取，并在全局槽位 cap 内完成有限应用层重试。

    每次尝试离开槽位后才退避，因此失败波段不会在睡眠时占住进程级读取名额。
    返回字典保持 ``items`` 的原始顺序。
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
    guard = _SceneGuard()
    _band_log(
        "band_parallel_start",
        **context,
        bands=n,
        workers=workers,
        scene_workers=max(1, int(scene_workers)),
        gdal_cap=cap,
        max_attempts=max_attempts,
        timeout_sec=timeout_sec,
        slot_wait_sec=gdal_slot_wait_sec(),
        scene_max_timeouts=band_scene_max_timeouts(),
        scene_budget_sec=band_scene_budget_sec(),
    )

    def _call(key: K, value: V) -> R:
        thread = threading.current_thread().name
        host = resolve_host(value)
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            skip_reason = guard.should_skip()
            if skip_reason is not None:
                guard.note_skip()
                _band_log(
                    "scene_fail_fast",
                    level="warning",
                    **context,
                    band=str(key),
                    reason=skip_reason,
                    timeouts=guard.timeouts,
                    skipped=guard.skipped,
                    budget_sec=band_scene_budget_sec(),
                    attempt=attempt,
                )
                raise SceneFailFastError(
                    f"skipping band {key}: scene fail-fast ({skip_reason}, "
                    f"timeouts={guard.timeouts})"
                )

            wait_ms = 0
            read_ms = 0
            io_ms = 0
            reproject_ms = 0
            t0 = time.perf_counter()
            try:
                timing: dict[str, int] = {}

                def _read() -> R | BandReadResult[R]:
                    t_read = time.perf_counter()
                    try:
                        return fn(key, value)
                    finally:
                        timing["read_ms"] = int((time.perf_counter() - t_read) * 1000)

                raw_result, wait_ms = _run_fn_holding_slot(_read, timeout_sec)
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
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                wall_timeout = _is_wall_timeout(exc)
                slot_wait_timeout = isinstance(exc, GdalSlotWaitTimeout)
                fail_fast = isinstance(exc, SceneFailFastError)
                if wall_timeout:
                    guard.note_timeout()
                    # Parent-observed time when the timeout was declared.
                    declared = int(timeout_sec * 1000) if timeout_sec > 0 else elapsed_ms
                    io_ms = declared
                    read_ms = declared
                    elapsed_ms = wait_ms + declared
                elif slot_wait_timeout:
                    io_ms = 0
                    read_ms = 0
                else:
                    read_ms = read_ms or max(0, elapsed_ms - wait_ms)
                    io_ms = read_ms
                timed_out = _is_timeout_error(exc)
                retryable = (retry_if or _is_retryable_band_error)(exc)
                # Hard I/O timeout does not retry the same connection; hedging
                # already raced a second connection. Slot-wait may retry.
                will_retry = (
                    retryable
                    and attempt < max_attempts
                    and not wall_timeout
                    and not fail_fast
                )
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
                    "slot_wait_timeout": slot_wait_timeout,
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
                    slot_wait_timeout=slot_wait_timeout,
                    thread=thread,
                    elapsed_ms=elapsed_ms,
                    wait_ms=wait_ms,
                    io_ms=io_ms,
                    reproject_ms=0,
                    read_ms=read_ms,
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                    timeout_sec=timeout_sec if not slot_wait_timeout else gdal_slot_wait_sec(),
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
            scene_timeouts=guard.timeouts,
            scene_skipped=guard.skipped,
            fail_fast=guard.reason,
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
                # Prefer the original timeout/I/O error over a fail-fast skip.
                preferred = next(
                    (item for item in errors if not isinstance(item[1], SceneFailFastError)),
                    errors[0],
                )
                key, exc = preferred
                raise RuntimeError(f"band read failed: {key}") from exc
    except Exception:
        _log_summary(int((time.perf_counter() - t0) * 1000), "failed")
        raise

    wall_ms = int((time.perf_counter() - t0) * 1000)
    _log_summary(wall_ms, "success")
    return {key: results[key] for key in items}


__all__ = [
    "BandReadResult",
    "BandReadTimeout",
    "GdalSlotWaitTimeout",
    "SceneFailFastError",
    "band_max_workers",
    "band_read_max_attempts",
    "band_read_timeout_sec",
    "effective_band_workers",
    "gdal_read_slot",
    "gdal_slot_snapshot",
    "gdal_slot_wait_sec",
    "reset_band_gdal_limit",
    "run_parallel_band_jobs",
]
