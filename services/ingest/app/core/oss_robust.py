"""跨境访问 OSS 的健壮封装：连接复用、更长超时、指数退避重试、大文件分片断点续传。

下载机 → oss-cn-beijing 只有 ~0.4 MB/s 且偶发 SSL UNEXPECTED_EOF / Server disconnected，
oss2 默认 60s 超时、无重试。这里包装 ``ObjectStorage``（OssStorage 时另建带 Session 与
(connect, read) 超时的 Bucket），其它后端（LocalDirStorage / 测试桩）只加重试。
"""

from __future__ import annotations

import os
import random
import ssl
import time
from typing import Any, Callable, TypeVar

import structlog

logger = structlog.get_logger()
T = TypeVar("T")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


CONNECT_TIMEOUT = _env_float("PROJECT_OSS_CONNECT_TIMEOUT_S", 10.0)
READ_TIMEOUT = _env_float("PROJECT_OSS_READ_TIMEOUT_S", 180.0)
ATTEMPTS = int(_env_float("PROJECT_OSS_ATTEMPTS", 5))
BACKOFF_BASE = _env_float("PROJECT_OSS_BACKOFF_S", 2.0)
MULTIPART_THRESHOLD = int(_env_float("PROJECT_OSS_MULTIPART_THRESHOLD_MB", 8) * 1024 * 1024)
PART_SIZE = int(_env_float("PROJECT_OSS_PART_SIZE_MB", 4) * 1024 * 1024)

_TRANSIENT_NAMES = (
    "SSLError", "SSLEOFError", "ConnectionError", "ConnectError", "ConnectTimeout", "ReadTimeout",
    "ReadError", "RemoteProtocolError", "RequestError", "Timeout", "TimeoutError", "ProtocolError",
    "ChunkedEncodingError", "IncompleteRead", "ServerDisconnectedError", "WriteError", "PoolTimeout",
)


def is_transient(exc: BaseException) -> bool:
    """SSL/连接/超时/5xx/429 视为可重试；4xx（NoSuchKey、鉴权）不重试。"""
    if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)):
        return True
    status = getattr(exc, "status", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    if isinstance(status, int) and status > 0:
        if status >= 500 or status == 429:
            return True
        if 400 <= status < 500:
            return False
    names = {c.__name__ for c in type(exc).__mro__}
    if names & set(_TRANSIENT_NAMES):
        return True
    # oss2.exceptions.RequestError（status=-2）包装底层 requests 异常
    if "RequestError" in names or status == -2:
        return True
    msg = str(exc)
    return any(s in msg for s in ("UNEXPECTED_EOF", "Server disconnected", "Connection reset",
                                  "Connection aborted", "timed out", "EOF occurred"))


def retry_call(
    fn: Callable[[], T],
    *,
    what: str,
    attempts: int | None = None,
    base: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **log_ctx: Any,
) -> T:
    """带指数退避（含抖动）的重试；非瞬时错误立即抛出。"""
    attempts = max(1, attempts or ATTEMPTS)
    base = BACKOFF_BASE if base is None else base
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if i >= attempts or not is_transient(exc):
                raise
            delay = min(60.0, base * (2 ** (i - 1))) * (0.75 + 0.5 * random.random())
            logger.warning("oss_retry", what=what, attempt=i, delay_s=round(delay, 1),
                           exc_type=type(exc).__name__, error=str(exc)[:300], **log_ctx)
            sleep(delay)
    raise AssertionError("unreachable")


class RobustStorage:
    """``ObjectStorage`` 的重试包装；未覆盖的属性透传给原对象。"""

    def __init__(self, inner: Any, *, sleep: Callable[[float], None] = time.sleep) -> None:
        self._inner = inner
        self._sleep = sleep
        self._bucket = None
        if getattr(inner, "backend", None) == "oss" and getattr(inner, "_bucket", None) is not None:
            try:
                import oss2

                b = inner._bucket
                self._bucket = oss2.Bucket(b.auth, b.endpoint, b.bucket_name, session=oss2.Session(),
                                           connect_timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            except Exception as exc:  # 退回原 bucket，仅保留重试
                logger.warning("oss_robust_bucket_init_failed", error=str(exc)[:200])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _retry(self, what: str, fn: Callable[[], T], key: str) -> T:
        return retry_call(fn, what=what, sleep=self._sleep, key=key)

    def exists(self, key: str) -> bool:
        if self._bucket is not None:
            return self._retry("exists", lambda: bool(self._bucket.object_exists(key)), key)
        return self._retry("exists", lambda: self._inner.exists(key), key)

    def upload_file(self, key: str, file_path: str, content_type: str | None = None) -> str:
        if self._bucket is not None:
            import oss2

            headers = {"Content-Type": content_type} if content_type else None
            size = os.path.getsize(file_path)

            def _up() -> str:
                if size >= MULTIPART_THRESHOLD:
                    oss2.resumable_upload(self._bucket, key, file_path, headers=headers,
                                          multipart_threshold=MULTIPART_THRESHOLD, part_size=PART_SIZE,
                                          num_threads=4)
                else:
                    self._bucket.put_object_from_file(key, file_path, headers=headers)
                return key

            return self._retry("upload_file", _up, key)
        return self._retry("upload_file", lambda: self._inner.upload_file(key, file_path, content_type=content_type), key)

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        if self._bucket is not None:
            headers = {"Content-Type": content_type} if content_type else None
            return self._retry("put_bytes", lambda: (self._bucket.put_object(key, data, headers=headers), key)[1], key)
        return self._retry("put_bytes", lambda: self._inner.put_bytes(key, data, content_type=content_type), key)

    def get_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        if max_bytes is None:
            return self._retry("get_bytes", lambda: self._inner.get_bytes(key), key)
        return self._retry("get_bytes", lambda: self._inner.get_bytes(key, max_bytes=max_bytes), key)

    def download_file(self, key: str, file_path: str) -> None:
        if self._bucket is not None:
            return self._retry("download_file", lambda: self._bucket.get_object_to_file(key, file_path) and None, key)
        return self._retry("download_file", lambda: self._inner.download_file(key, file_path), key)


def robust(storage: Any) -> Any:
    return storage if isinstance(storage, RobustStorage) else RobustStorage(storage)


def retrying_report(report: Callable[[dict], Any] | None, *, attempts: int | None = None,
                    sleep: Callable[[float], None] = time.sleep) -> Callable[[dict], Any] | None:
    """回执 API 的重试包装（results/apply 为幂等 upsert，可安全重放）。"""
    if report is None or getattr(report, "_retrying", False):
        return report

    def _r(payload: dict) -> Any:
        return retry_call(lambda: report(payload), what="report", attempts=attempts, sleep=sleep,
                          kind=payload.get("kind"), key=payload.get("oss_key") or payload.get("run_id"))

    _r._retrying = True  # type: ignore[attr-defined]
    return _r
