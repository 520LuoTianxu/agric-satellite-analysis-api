"""Timed HTTP Range I/O for windowed COG reads, outside GDAL/libcurl.

Windowed Sentinel-2/1 reads previously went through ``rasterio.open`` on a
``/vsicurl/`` URL. GDAL then used libcurl for Range GETs. In production those
calls often ignored ``GDAL_HTTP_TIMEOUT``, and a Python thread watchdog could
not kill the hung C stack, so later 1-2s reads never got a slot.

This module owns the HTTP Range traffic with ``httpx``, whose timeouts actually
cancel. After the needed bytes are in hand, rasterio/GDAL only decode from the
Python file-like via rasterio's ``opener=`` VSI plugin (header + required
tiles, never the whole object).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit

import httpx

_log = logging.getLogger("openfarm.ingest.cog_http")

_DEFAULT_TIMEOUT_SEC = 20.0
_DEFAULT_CONNECT_SEC = 10.0
_DEFAULT_HEDGE_AFTER_SEC = 5.0
_DEFAULT_CHUNK = 262144
_HEADER_PREFETCH = 32768
_HEDGE_CIRCUIT_WINDOW_SEC = 60.0
_HEDGE_CIRCUIT_MIN_SAMPLES = 6
_HEDGE_CIRCUIT_TIMEOUT_RATE = 0.5

_VSI_PREFIXES = ("/vsicurl/", "/vsis3/", "/vsiaz/", "/vsis/")

_attempt = threading.local()
_tls = threading.local()
_size_lock = threading.Lock()
_size_cache: dict[str, int] = {}
_header_lock = threading.Lock()
_header_cache: dict[str, tuple[int, bytes]] = {}
_circuit_lock = threading.Lock()
_circuit_events: list[tuple[float, bool]] = []  # (monotonic, timed_out)


class BandReadTimeout(TimeoutError):
    """A Range request or parent wall clock exceeded the configured timeout."""


class GdalSlotWaitTimeout(TimeoutError):
    """No band-read slot became free within ``GDAL_SLOT_WAIT_SEC``."""


class SceneFailFastError(RuntimeError):
    """Remaining bands of a scene were skipped after too many timeouts or a budget hit."""


def _cog_log(event: str, *, level: str = "info", **kwargs: Any) -> None:
    try:
        import structlog
    except ImportError:
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
        getattr(_log, level, _log.info)("%s %s", event, extra)
        return
    logger = structlog.get_logger()
    getattr(logger, level, logger.info)(event, **kwargs)


def _float_env(name: str, default: float, *, minimum: float | None = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        return max(minimum, value)
    return value


def band_read_timeout_sec() -> float:
    """Per-request read/total timeout and parent wall clock for one band attempt.

    Default 20s. ``<= 0`` disables the parent watchdog (HTTP still uses a
    positive fallback only when a request is made; see ``effective_http_timeout``).
    """
    raw = os.environ.get("BAND_READ_TIMEOUT_SEC", "20")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SEC
    if value <= 0:
        return 0.0
    return value


def band_read_connect_timeout_sec() -> float:
    raw = os.environ.get("BAND_READ_CONNECT_TIMEOUT_SEC")
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get("GDAL_HTTP_CONNECTTIMEOUT", "10")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CONNECT_SEC
    return max(0.1, value)


def band_read_hedge_after_sec() -> float:
    """Seconds to wait for first-byte success before starting a second connection.

    ``<= 0`` disables hedging.
    """
    raw = os.environ.get("BAND_READ_HEDGE_AFTER_SEC", "5")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_HEDGE_AFTER_SEC
    if value <= 0:
        return 0.0
    return value


def gdal_slot_wait_sec() -> float:
    """Seconds a band may wait for a free global slot before failing fast."""
    raw = os.environ.get("GDAL_SLOT_WAIT_SEC", "20")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 20.0
    if value < 0:
        return 0.0
    return value


def band_scene_max_timeouts() -> int:
    """Skip remaining bands after this many timeouts in the same scene. ``<=0`` disables."""
    raw = os.environ.get("BAND_SCENE_MAX_TIMEOUTS", "2")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2
    return value


def band_scene_budget_sec() -> float:
    """Scene wall budget after which remaining bands are skipped. ``<=0`` disables."""
    raw = os.environ.get("BAND_SCENE_BUDGET_SEC", "60")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 60.0
    if value <= 0:
        return 0.0
    return value


def _chunk_size() -> int:
    raw = os.environ.get("WINDOWED_GDAL_CURL_CHUNK_SIZE") or os.environ.get(
        "GDAL_CURL_CHUNK_SIZE"
    )
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_CHUNK
    try:
        return max(16 * 1024, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_CHUNK


def unwrap_raster_href(href: str) -> str:
    """Strip GDAL VSI prefixes and rewrite ``s3://`` / ``/vsis3/`` to HTTPS."""
    raw = str(href).strip()
    prefix_used = ""
    for prefix in _VSI_PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            prefix_used = prefix
            break
    if raw.startswith("s3://"):
        rest = raw[5:]
        bucket, _, key = rest.partition("/")
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    if prefix_used == "/vsis3/" and not raw.startswith(("http://", "https://", "s3://")):
        bucket, _, key = raw.partition("/")
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    if prefix_used == "/vsiaz/" and not raw.startswith(("http://", "https://")):
        return f"https://{raw.lstrip('/')}"
    return raw


def is_http_raster_href(href: str | None) -> bool:
    if not href:
        return False
    raw = unwrap_raster_href(href)
    return raw.startswith("http://") or raw.startswith("https://")


def _safe_host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except Exception:
        return None


@dataclass
class AttemptContext:
    """Per-attempt deadline and cancel flag, shared with Range I/O."""

    deadline: float
    cancel: threading.Event
    _closers: list[Callable[[], None]]
    _lock: threading.Lock

    def remaining(self) -> float:
        if self.deadline == float("inf"):
            return float("inf")
        return max(0.0, self.deadline - time.monotonic())

    def add_closer(self, closer: Callable[[], None]) -> None:
        with self._lock:
            self._closers.append(closer)

    def remove_closer(self, closer: Callable[[], None]) -> None:
        with self._lock:
            try:
                self._closers.remove(closer)
            except ValueError:
                return

    def close_inflight(self) -> None:
        with self._lock:
            closers = list(self._closers)
            self._closers.clear()
        for closer in closers:
            try:
                closer()
            except Exception:
                continue


def current_attempt() -> AttemptContext | None:
    return getattr(_attempt, "ctx", None)


@contextmanager
def use_attempt_context(ctx: AttemptContext | None) -> Iterator[AttemptContext | None]:
    """Bind an attempt context to this thread (does not create or close it).

    硬超时计时在父线程，Range I/O 在 watchdog 子线程。thread-local 上下文
    不会自动跟上，必须在子线程显式绑定同一个 ``AttemptContext``，取消和
    closer 才能作用到实际读取。
    """
    previous = getattr(_attempt, "ctx", None)
    _attempt.ctx = ctx
    try:
        yield ctx
    finally:
        _attempt.ctx = previous


@contextmanager
def band_attempt_context(timeout_sec: float) -> Iterator[AttemptContext]:
    deadline = (
        float("inf")
        if timeout_sec <= 0
        else time.monotonic() + float(timeout_sec)
    )
    ctx = AttemptContext(
        deadline=deadline,
        cancel=threading.Event(),
        _closers=[],
        _lock=threading.Lock(),
    )
    with use_attempt_context(ctx):
        try:
            yield ctx
        finally:
            ctx.cancel.set()
            ctx.close_inflight()


def reset_cog_http_state() -> None:
    """Drop cached sizes, headers, hedge-circuit samples, and thread-local clients."""
    global _circuit_events, _size_cache, _header_cache
    with _circuit_lock:
        _circuit_events = []
    with _size_lock:
        _size_cache = {}
    with _header_lock:
        _header_cache = {}
    _attempt.ctx = None
    client = getattr(_tls, "client", None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
        _tls.client = None


def hedge_circuit_open() -> bool:
    """True when a recent window already has a high timeout rate (skip hedging)."""
    now = time.monotonic()
    window = _float_env(
        "BAND_READ_HEDGE_CIRCUIT_WINDOW_SEC", _HEDGE_CIRCUIT_WINDOW_SEC, minimum=1.0
    )
    min_samples = int(
        _float_env(
            "BAND_READ_HEDGE_CIRCUIT_MIN_SAMPLES",
            float(_HEDGE_CIRCUIT_MIN_SAMPLES),
            minimum=1.0,
        )
    )
    rate_limit = _float_env(
        "BAND_READ_HEDGE_CIRCUIT_TIMEOUT_RATE",
        _HEDGE_CIRCUIT_TIMEOUT_RATE,
        minimum=0.0,
    )
    with _circuit_lock:
        cutoff = now - window
        live = [(ts, flag) for ts, flag in _circuit_events if ts >= cutoff]
        _circuit_events[:] = live
        if len(live) < min_samples:
            return False
        timeouts = sum(1 for _, flag in live if flag)
        return (timeouts / len(live)) >= rate_limit


def record_range_outcome(*, timed_out: bool) -> None:
    now = time.monotonic()
    with _circuit_lock:
        _circuit_events.append((now, timed_out))


def _primary_client() -> httpx.Client:
    client = getattr(_tls, "client", None)
    if client is None or client.is_closed:
        client = httpx.Client(
            http2=False,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=8,
                max_keepalive_connections=4,
                keepalive_expiry=30.0,
            ),
            headers={"User-Agent": "openfarm-ingest-cog-http/1.0"},
        )
        _tls.client = client
    return client


def _http_timeout(remaining: float | None = None) -> httpx.Timeout:
    total = band_read_timeout_sec()
    if total <= 0:
        total = _DEFAULT_TIMEOUT_SEC
    if remaining is not None:
        total = max(0.05, min(total, remaining))
    connect = min(band_read_connect_timeout_sec(), total)
    return httpx.Timeout(total, connect=connect)


def _parse_content_range_total(value: str | None) -> int | None:
    if not value:
        return None
    text = value.strip()
    if "/" not in text:
        return None
    total = text.rsplit("/", 1)[-1].strip()
    if not total or total == "*":
        return None
    try:
        parsed = int(total)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _slice_full_body(payload: bytes, start: int, end: int, status_code: int) -> bytes:
    if status_code == 206:
        expected = end - start + 1
        return payload[:expected]
    # Server ignored Range and sent the object from byte 0.
    return payload[start : end + 1]


def fetch_byte_range(
    url: str,
    start: int,
    end: int,
    *,
    allow_hedge: bool = True,
) -> tuple[bytes, int | None]:
    """GET ``bytes=start-end`` with a hard timeout and optional hedged retry.

    A hard timeout never retries on the same connection. If the first request
    has not succeeded after ``BAND_READ_HEDGE_AFTER_SEC``, a second request is
    started on a new connection; the first success wins and the loser is
    abandoned. When the hedge circuit is open, the second request is skipped.
    """
    if start < 0 or end < start:
        raise ValueError(f"invalid byte range: {start}-{end}")
    ctx = current_attempt()
    if ctx is not None and (ctx.cancel.is_set() or ctx.remaining() <= 0):
        raise BandReadTimeout("band read cancelled or exceeded wall timeout")

    hedge_after = band_read_hedge_after_sec()
    use_hedge = (
        allow_hedge
        and hedge_after > 0
        and not hedge_circuit_open()
    )
    cancel = threading.Event()
    winner: dict[str, Any] = {}
    done = threading.Event()
    errors: dict[str, BaseException] = {}
    lock = threading.Lock()
    started = time.perf_counter()
    local_closers: list[Callable[[], None]] = []

    def _abort_client(client: httpx.Client) -> None:
        try:
            client.close()
        except Exception:
            return

    def _close_losers() -> None:
        if ctx is not None:
            ctx.close_inflight()
        with lock:
            closers = list(local_closers)
            local_closers.clear()
        for closer in closers:
            try:
                closer()
            except Exception:
                continue

    def _attempt(name: str, client: httpx.Client, *, close_client: bool) -> None:
        if cancel.is_set() or done.is_set():
            if close_client:
                _abort_client(client)
            return
        closer = lambda: _abort_client(client)
        with lock:
            local_closers.append(closer)
        if ctx is not None:
            ctx.add_closer(closer)
        remaining = ctx.remaining() if ctx is not None else None
        timeout = _http_timeout(remaining)
        headers = {"Range": f"bytes={start}-{end}"}
        try:
            with client.stream("GET", url, headers=headers, timeout=timeout) as resp:
                if cancel.is_set() or done.is_set():
                    return
                resp.raise_for_status()
                total = _parse_content_range_total(resp.headers.get("Content-Range"))
                if total is None:
                    try:
                        total = int(resp.headers.get("Content-Length") or "")
                    except ValueError:
                        total = None
                    if resp.status_code == 200 and total is not None and start:
                        # Content-Length is the whole object, not the requested window.
                        pass
                body = bytearray()
                skip = start if resp.status_code == 200 else 0
                needed = end - start + 1
                skipped = 0
                for chunk in resp.iter_bytes(64 * 1024):
                    if cancel.is_set() or done.is_set():
                        return
                    if skip and skipped < skip:
                        take = min(len(chunk), skip - skipped)
                        skipped += take
                        chunk = chunk[take:]
                        if not chunk:
                            continue
                    remaining_body = needed - len(body)
                    if remaining_body <= 0:
                        break
                    body.extend(chunk[:remaining_body])
                    if len(body) >= needed:
                        break
                data = bytes(body)
                if resp.status_code == 206 and not data:
                    raise httpx.HTTPStatusError(
                        "empty 206 range", request=resp.request, response=resp
                    )
                with lock:
                    if "data" not in winner and not done.is_set():
                        winner["data"] = data
                        winner["total"] = total
                        winner["name"] = name
                        done.set()
        except BaseException as exc:
            with lock:
                errors[name] = exc
            if name == "hedge" or done.is_set() or cancel.is_set():
                return
        finally:
            if ctx is not None:
                ctx.remove_closer(closer)
            if close_client:
                _abort_client(client)

    primary = _primary_client()
    primary_thread = threading.Thread(
        target=_attempt,
        args=("primary", primary),
        kwargs={"close_client": False},
        daemon=True,
        name="cog-http-range",
    )
    primary_thread.start()

    hedged = False
    if use_hedge:
        if not done.wait(hedge_after):
            if not cancel.is_set() and (ctx is None or not ctx.cancel.is_set()):
                hedged = True
                _cog_log(
                    "cog_http_hedge_start",
                    level="info",
                    host=_safe_host(url),
                    start=start,
                    end=end,
                    after_sec=hedge_after,
                )
                hedge_client = httpx.Client(
                    http2=False,
                    follow_redirects=True,
                    headers={"User-Agent": "openfarm-ingest-cog-http/1.0"},
                )
                hedge_thread = threading.Thread(
                    target=_attempt,
                    args=("hedge", hedge_client),
                    kwargs={"close_client": True},
                    daemon=True,
                    name="cog-http-hedge",
                )
                hedge_thread.start()

    wall = band_read_timeout_sec()
    if ctx is not None:
        wait_for = ctx.remaining()
    else:
        wait_for = wall if wall > 0 else _DEFAULT_TIMEOUT_SEC
    finished = done.wait(max(0.05, wait_for) if wait_for != float("inf") else None)

    if not finished:
        cancel.set()
        _close_losers()
        record_range_outcome(timed_out=True)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        raise BandReadTimeout(
            f"byte range {start}-{end} exceeded {wall or wait_for:g}s "
            f"(elapsed_ms={elapsed_ms})"
        )

    cancel.set()
    # Keep the thread-local primary client (HTTP/1.1 keepalive) on a primary
    # win. If the hedge won or nobody finished, close in-flight connections.
    if winner.get("name") != "primary":
        _close_losers()
    data = winner.get("data")
    if data is None:
        exc = errors.get("primary") or errors.get("hedge")
        timed_out = isinstance(exc, (TimeoutError, httpx.TimeoutException)) or (
            exc is not None and "timeout" in f"{type(exc).__name__}: {exc}".lower()
        )
        record_range_outcome(timed_out=timed_out)
        if timed_out:
            raise BandReadTimeout(str(exc) if exc else "range request timed out") from exc
        if exc is not None:
            raise exc
        raise BandReadTimeout("range request failed without a result")

    record_range_outcome(timed_out=False)
    if hedged:
        _cog_log(
            "cog_http_hedge_win" if winner.get("name") == "hedge" else "cog_http_hedge_lose",
            level="info",
            host=_safe_host(url),
            start=start,
            end=end,
            winner=winner.get("name"),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    total = winner.get("total")
    if isinstance(total, int) and total >= 0:
        with _size_lock:
            _size_cache[url] = total
    return data, total if isinstance(total, int) else None


class TimedRangeFile:
    """Seekable HTTP Range file-like used as a rasterio ``opener`` result.

    Reads are satisfied from a block cache; missing blocks become one Range
    GET (optionally hedged). The object is never downloaded in full.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._pos = 0
        self._size: int | None
        with _size_lock:
            self._size = _size_cache.get(url)
        self._blocks: dict[int, bytes] = {}
        self._chunk = _chunk_size()
        self.closed = False
        header = None
        with _header_lock:
            cached = _header_cache.get(url)
        if cached is not None:
            self._size = cached[0]
            header = cached[1]
            self._store_bytes(0, header)

    def __enter__(self) -> "TimedRangeFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._check_open()
        if whence == os.SEEK_SET:
            pos = int(offset)
        elif whence == os.SEEK_CUR:
            pos = self._pos + int(offset)
        elif whence == os.SEEK_END:
            pos = self.size() + int(offset)
        else:
            raise ValueError(f"invalid whence: {whence}")
        if pos < 0:
            raise ValueError("negative seek position")
        self._pos = pos
        return self._pos

    def size(self) -> int:
        if self._size is not None:
            return self._size
        end = max(_HEADER_PREFETCH, self._chunk) - 1
        data, total = fetch_byte_range(self.url, 0, end)
        self._store_bytes(0, data)
        if total is None:
            # No Content-Range: if the payload was shorter than asked, it is the whole object.
            if len(data) <= end:
                total = len(data)
            else:
                raise OSError(f"could not determine size of {self.url}")
        self._size = total
        with _size_lock:
            _size_cache[self.url] = total
        with _header_lock:
            _header_cache[self.url] = (total, data[: max(_HEADER_PREFETCH, self._chunk)])
        return self._size

    def read(self, size: int = -1) -> bytes:
        self._check_open()
        if size == 0:
            return b""
        known = self._size
        if size is None or size < 0:
            if known is None:
                known = self.size()
            size = max(0, known - self._pos)
        if known is not None and self._pos >= known:
            return b""
        start = self._pos
        end = start + int(size) - 1
        if known is not None:
            end = min(end, known - 1)
        if end < start:
            return b""
        payload = self._read_span(start, end)
        self._pos = start + len(payload)
        return payload

    def readinto(self, b) -> int:  # type: ignore[no-untyped-def]
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def get_byte_ranges(self, offsets: list[int], sizes: list[int]) -> list[bytes]:
        """Serial Range assembly for GDAL multi-range callbacks."""
        out: list[bytes] = []
        for offset, length in zip(offsets, sizes):
            if length <= 0:
                out.append(b"")
                continue
            out.append(self._read_span(int(offset), int(offset) + int(length) - 1))
        return out

    def _check_open(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed TimedRangeFile")

    def _store_bytes(self, start: int, data: bytes) -> None:
        if not data:
            return
        chunk = self._chunk
        # Align stored views to chunk boundaries; leftovers stay as partial last blocks.
        offset = 0
        while offset < len(data):
            abs_pos = start + offset
            block = abs_pos // chunk
            inner = abs_pos % chunk
            take = min(chunk - inner, len(data) - offset)
            existing = self._blocks.get(block)
            if existing is None:
                buf = bytearray(chunk)
                present = False
            else:
                buf = bytearray(existing)
                present = True
            buf[inner : inner + take] = data[offset : offset + take]
            # Keep a short last block only when we already know it is the file tail
            # and this write starts at the beginning of the block.
            if (
                not present
                and inner == 0
                and take < chunk
                and self._size is not None
                and (block * chunk + take) >= self._size
            ):
                self._blocks[block] = bytes(buf[:take])
            else:
                self._blocks[block] = bytes(buf)
            offset += take

    def _block_bytes(self, block: int, start: int, end: int) -> bytes | None:
        data = self._blocks.get(block)
        if not data:
            return None
        chunk = self._chunk
        lo = start - block * chunk
        hi = end - block * chunk + 1
        if lo < 0 or hi > len(data):
            # Partial cache entry that does not cover the requested span.
            if lo >= 0 and hi <= self._chunk and hi <= len(data):
                return data[lo:hi]
            return None
        return data[lo:hi]

    def _read_span(self, start: int, end: int) -> bytes:
        if end < start:
            return b""
        chunk = self._chunk
        parts: list[bytes] = []
        pos = start
        while pos <= end:
            block = pos // chunk
            block_start = block * chunk
            block_end = block_start + chunk - 1
            want_end = min(end, block_end)
            cached = self._block_bytes(block, pos, want_end)
            if cached is not None and len(cached) == (want_end - pos + 1):
                parts.append(cached)
                pos = want_end + 1
                continue
            # Fetch a full chunk (or to known EOF) to amortize small GDAL reads.
            fetch_end = block_end
            if self._size is not None:
                fetch_end = min(fetch_end, self._size - 1)
            data, total = fetch_byte_range(self.url, block_start, fetch_end)
            if total is not None:
                self._size = total
            self._store_bytes(block_start, data)
            if block_start == 0:
                with _header_lock:
                    _header_cache[self.url] = (
                        self._size or (total or (block_start + len(data))),
                        data[: max(_HEADER_PREFETCH, self._chunk)],
                    )
            cached = self._block_bytes(block, pos, want_end)
            if cached is None:
                # Direct slice from the fetched payload if cache alignment failed.
                lo = pos - block_start
                hi = want_end - block_start + 1
                cached = data[lo:hi]
            parts.append(cached)
            pos = want_end + 1
        return b"".join(parts)


def timed_range_opener(url: str) -> Callable[..., TimedRangeFile]:
    """Build a rasterio ``opener`` that always Range-reads ``url``."""

    def opener(path: str, mode: str = "rb", **kwargs: Any) -> TimedRangeFile:
        text = str(path)
        # rasterio probes ``size("test/test")`` while registering the plugin.
        if text == "test/test" or text.endswith("/test/test"):
            raise FileNotFoundError(path)
        return TimedRangeFile(url)

    return opener


@contextmanager
def open_windowed_cog(href: str, *, env: dict[str, str] | None = None):
    """Open a windowed COG so HTTP Range traffic does not use GDAL vsicurl.

    Remote ``http(s)`` (including ``/vsicurl/`` and rewritten ``/vsis3/``) URLs
    are opened with :func:`timed_range_opener`. Local paths and ``/vsimem/``
    fall back to a normal ``rasterio.open``.
    """
    import rasterio

    from app.core.windowed_gdal import windowed_cog_gdal_env

    gdal_env = env if env is not None else windowed_cog_gdal_env()
    raw = str(href).strip()
    if raw.startswith("/vsimem/") or not is_http_raster_href(raw):
        with rasterio.Env(**gdal_env):
            with rasterio.open(raw) as src:
                yield src
        return

    url = unwrap_raster_href(raw)
    with rasterio.Env(**gdal_env):
        with rasterio.open(url, opener=timed_range_opener(url)) as src:
            yield src


__all__ = [
    "AttemptContext",
    "BandReadTimeout",
    "GdalSlotWaitTimeout",
    "SceneFailFastError",
    "TimedRangeFile",
    "band_attempt_context",
    "band_read_connect_timeout_sec",
    "band_read_hedge_after_sec",
    "band_read_timeout_sec",
    "band_scene_budget_sec",
    "band_scene_max_timeouts",
    "current_attempt",
    "fetch_byte_range",
    "gdal_slot_wait_sec",
    "hedge_circuit_open",
    "is_http_raster_href",
    "open_windowed_cog",
    "record_range_outcome",
    "reset_cog_http_state",
    "timed_range_opener",
    "unwrap_raster_href",
    "use_attempt_context",
]
