"""Aliyun OSS object storage implementation."""

from __future__ import annotations

import os
import random
import ssl
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import urlparse

import oss2
import structlog

from agric_satellite_analysis_common.settings import settings

logger = structlog.get_logger(__name__)

T = TypeVar("T")

_TRANSIENT_EXC_NAMES = frozenset(
    {
        "SSLError",
        "SSLEOFError",
        "ConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "ReadError",
        "RemoteProtocolError",
        "RequestError",
        "Timeout",
        "TimeoutError",
        "ProtocolError",
        "ChunkedEncodingError",
        "IncompleteRead",
        "ServerDisconnectedError",
        "WriteError",
        "PoolTimeout",
        "ClientError",
    }
)

_TRANSIENT_MSG_FRAGMENTS = (
    "UNEXPECTED_EOF",
    "Server disconnected",
    "Connection reset",
    "Connection aborted",
    "timed out",
    "EOF occurred",
    "Temporary failure",
)


class ObjectTooLargeError(ValueError):
    """对象流超过调用方上限时抛出，避免把整份大对象读入内存。"""


def signed_get_expire_sec(*, years: float = 20.0) -> int:
    """OSS signed GET TTL in seconds (default 20 years)."""
    return max(int(years * 365.25 * 24 * 3600), 24 * 3600)


def resolve_oss_is_cname(
    endpoint: str,
    bucket: str,
    configured: bool | None = None,
) -> bool:
    """Resolve ``is_cname`` for ``oss2.Bucket``.

    Explicit ``configured`` wins. Otherwise treat the endpoint as CNAME /
    virtual-hosted when its host starts with ``{bucket}.`` (e.g.
    ``https://agric-prod.oss-accelerate.aliyuncs.com``).
    """
    if configured is not None:
        return bool(configured)
    parsed = urlparse((endpoint or "").strip())
    host = (parsed.netloc or parsed.path or "").split("@")[-1].lower()
    bucket_l = (bucket or "").strip().lower()
    if not host or not bucket_l:
        return False
    return host.startswith(f"{bucket_l}.")


def oss_multipart_threshold_bytes() -> int:
    """Byte threshold at/above which uploads use resumable multipart."""
    mb = max(float(settings.oss_multipart_threshold_mb), 0.0)
    return max(int(mb * 1024 * 1024), 1)


def oss_part_size_bytes() -> int:
    """Multipart part size in bytes (oss2 minimum is 100 KiB; keep >= 1 MiB)."""
    mb = max(float(settings.oss_part_size_mb), 0.1)
    return max(int(mb * 1024 * 1024), 100 * 1024)


def oss_resumable_store_root() -> str:
    """Directory root for ``oss2.ResumableStore`` checkpoints."""
    configured = (settings.oss_resumable_store_dir or "").strip()
    if configured:
        return configured
    scratch = (settings.openfarm_scratch_dir or "/data/scratch").rstrip("/")
    return f"{scratch}/oss_resume"


def is_transient_oss_error(exc: BaseException) -> bool:
    """SSL / connection / timeout / 5xx / 429 are retryable; other 4xx are not."""
    if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError, OSError)):
        return True
    status = getattr(exc, "status", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
    if isinstance(status, int) and status > 0:
        if status >= 500 or status == 429:
            return True
        if 400 <= status < 500:
            return False
    names = {cls.__name__ for cls in type(exc).__mro__}
    if names & _TRANSIENT_EXC_NAMES:
        return True
    if status == -2:  # oss2 RequestError wrapping requests failures
        return True
    msg = str(exc)
    return any(fragment in msg for fragment in _TRANSIENT_MSG_FRAGMENTS)


def retry_oss_call(
    fn: Callable[[], T],
    *,
    what: str,
    attempts: int | None = None,
    base: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **log_ctx: object,
) -> T:
    """Exponential backoff with jitter; non-transient errors raise immediately."""
    attempts = max(1, int(attempts if attempts is not None else settings.oss_upload_max_retries))
    base = float(settings.oss_upload_retry_backoff_s if base is None else base)
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — classify then re-raise
            last_exc = exc
            if attempt >= attempts or not is_transient_oss_error(exc):
                raise
            delay = min(60.0, base * (2 ** (attempt - 1))) * (0.75 + 0.5 * random.random())
            logger.warning(
                "oss_upload_retry",
                what=what,
                attempt=attempt,
                delay_s=round(delay, 1),
                exc_type=type(exc).__name__,
                error=str(exc)[:300],
                **log_ctx,
            )
            sleep(delay)
    assert last_exc is not None
    raise last_exc


class ObjectStorage(ABC):
    """Backend-agnostic object storage interface."""

    @property
    @abstractmethod
    def backend(self) -> str:
        """Backend name."""

    @property
    @abstractmethod
    def bucket(self) -> str:
        """Bucket / container name."""

    @abstractmethod
    def upload_file(
        self,
        key: str,
        file_path: str,
        content_type: str | None = None,
    ) -> str:
        """Upload a local file. Returns the object key."""

    @abstractmethod
    def download_file(self, key: str, file_path: str) -> None:
        """Download object to a local path."""

    @abstractmethod
    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
    ) -> str:
        """Upload raw bytes. Returns the object key."""

    @abstractmethod
    def get_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        """下载对象内容；提供上限时在流读取阶段限制传输和内存占用。"""

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """按块读取对象；后端必须原生实现以避免整对象驻留内存。"""
        raise NotImplementedError("storage backend must implement streaming reads")

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return True if the object exists."""

    @abstractmethod
    def list_keys(
        self,
        prefix: str,
        suffix: str = "",
        limit: int = 0,
    ) -> list[str]:
        """List object keys under ``prefix``, optionally filtered by ``suffix``.

        ``limit`` 0 means no limit (return all matching keys found).
        """

    @abstractmethod
    def presigned_put(
        self,
        key: str,
        expires: timedelta = timedelta(minutes=15),
        content_type: str | None = None,
    ) -> str:
        """Return a presigned URL for HTTP PUT upload."""

    @abstractmethod
    def public_url(self, key: str) -> str:
        """Best-effort public (or endpoint) URL for the object."""

    def presigned_get(
        self,
        key: str,
        expires: timedelta | None = None,
    ) -> str:
        """Return a browser-usable signed GET URL for a private bucket object.

        Default expiry is long-lived (through 2099-12-31 UTC) to match the
        offline parcel pipeline's ``forever_expire_sec`` convention.
        """
        raise NotImplementedError

    @abstractmethod
    def uri_for(self, key: str) -> str:
        """Canonical storage URI (``s3://`` or ``oss://``)."""


class OssStorage(ObjectStorage):
    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not settings.oss_access_key_id or not settings.oss_access_key_secret:
            raise ValueError(
                "OSS backend requires OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET"
            )
        auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
        self._bucket_name = settings.oss_bucket
        self._endpoint = settings.oss_endpoint
        self._is_cname = resolve_oss_is_cname(
            self._endpoint,
            self._bucket_name,
            settings.oss_is_cname,
        )
        self._multipart_threshold = oss_multipart_threshold_bytes()
        self._part_size = oss_part_size_bytes()
        self._num_threads = max(1, int(settings.oss_multipart_num_threads))
        self._max_retries = max(1, int(settings.oss_upload_max_retries))
        self._backoff = float(settings.oss_upload_retry_backoff_s)
        self._store_root = oss_resumable_store_root()
        self._sleep = sleep
        connect = max(float(settings.oss_connect_timeout_s), 1.0)
        read = max(float(settings.oss_read_timeout_s), 1.0)
        self._bucket = oss2.Bucket(
            auth,
            self._endpoint,
            self._bucket_name,
            is_cname=self._is_cname,
            session=oss2.Session(),
            connect_timeout=(connect, read),
        )

    @property
    def backend(self) -> str:
        return "oss"

    @property
    def bucket(self) -> str:
        return self._bucket_name

    def _headers(self, content_type: str | None) -> dict[str, str] | None:
        return {"Content-Type": content_type} if content_type else None

    def _resumable_store(self) -> oss2.ResumableStore:
        root = Path(self._store_root)
        root.mkdir(parents=True, exist_ok=True)
        # Keep checkpoints under ``{root}/.py-oss-upload`` (oss2 default dir name).
        return oss2.ResumableStore(root=str(root))

    def upload_file(
        self,
        key: str,
        file_path: str,
        content_type: str | None = None,
    ) -> str:
        if not file_path or not os.path.isfile(file_path):
            raise FileNotFoundError(f"upload path missing or not a file: {file_path!r}")
        size = os.path.getsize(file_path)
        mode = "multipart" if size >= self._multipart_threshold else "put"
        headers = self._headers(content_type)
        started = time.monotonic()
        attempt_box = {"n": 0}

        def _once() -> str:
            attempt_box["n"] += 1
            if mode == "multipart":
                oss2.resumable_upload(
                    self._bucket,
                    key,
                    file_path,
                    store=self._resumable_store(),
                    headers=headers,
                    multipart_threshold=self._multipart_threshold,
                    part_size=self._part_size,
                    num_threads=self._num_threads,
                )
            else:
                self._bucket.put_object_from_file(key, file_path, headers=headers)
            return key

        try:
            result = retry_oss_call(
                _once,
                what="upload_file",
                attempts=self._max_retries,
                base=self._backoff,
                sleep=self._sleep,
                key=key,
                size=size,
                mode=mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "oss_upload_failed",
                key=key,
                size=size,
                mode=mode,
                attempt=attempt_box["n"],
                elapsed_s=round(time.monotonic() - started, 3),
                exc_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            raise
        logger.info(
            "oss_upload_ok",
            key=key,
            size=size,
            mode=mode,
            attempt=attempt_box["n"],
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return result

    def download_file(self, key: str, file_path: str) -> None:
        def _once() -> None:
            self._bucket.get_object_to_file(key, file_path)

        retry_oss_call(
            _once,
            what="download_file",
            attempts=self._max_retries,
            base=self._backoff,
            sleep=self._sleep,
            key=key,
        )

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
    ) -> str:
        size = len(data)
        if size >= self._multipart_threshold:
            root = Path(self._store_root)
            root.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(prefix="oss-put-", suffix=".bin", dir=str(root))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                return self.upload_file(key, tmp_path, content_type=content_type)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        headers = self._headers(content_type)
        started = time.monotonic()
        attempt_box = {"n": 0}

        def _once() -> str:
            attempt_box["n"] += 1
            self._bucket.put_object(key, data, headers=headers)
            return key

        try:
            result = retry_oss_call(
                _once,
                what="put_bytes",
                attempts=self._max_retries,
                base=self._backoff,
                sleep=self._sleep,
                key=key,
                size=size,
                mode="put",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "oss_upload_failed",
                key=key,
                size=size,
                mode="put",
                attempt=attempt_box["n"],
                elapsed_s=round(time.monotonic() - started, 3),
                exc_type=type(exc).__name__,
                error=str(exc)[:300],
            )
            raise
        logger.info(
            "oss_upload_ok",
            key=key,
            size=size,
            mode="put",
            attempt=attempt_box["n"],
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return result

    def get_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        def _once() -> bytes:
            result = self._bucket.get_object(key)
            try:
                if max_bytes is None:
                    return result.read()
                if max_bytes < 0:
                    raise ValueError("max_bytes must be non-negative")
                # 多读1字节仅用于识别超限对象；OSS流随即关闭，不会把剩余大对象拉入内存。
                raw = result.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise ObjectTooLargeError(
                        f"object {key!r} exceeds the {max_bytes}-byte read limit"
                    )
                return raw
            finally:
                result.close()

        return retry_oss_call(
            _once,
            what="get_bytes",
            attempts=self._max_retries,
            base=self._backoff,
            sleep=self._sleep,
            key=key,
        )

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """从OSS响应流逐块返回对象，并在客户端断开时关闭底层连接。"""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        result = self._bucket.get_object(key)
        try:
            while chunk := result.read(chunk_size):
                yield chunk
        finally:
            result.close()

    def exists(self, key: str) -> bool:
        def _once() -> bool:
            return bool(self._bucket.object_exists(key))

        return retry_oss_call(
            _once,
            what="exists",
            attempts=self._max_retries,
            base=self._backoff,
            sleep=self._sleep,
            key=key,
        )

    def list_keys(
        self,
        prefix: str,
        suffix: str = "",
        limit: int = 0,
    ) -> list[str]:
        keys: list[str] = []
        for obj in oss2.ObjectIterator(self._bucket, prefix=prefix):
            key = obj.key
            if suffix and not key.endswith(suffix):
                continue
            keys.append(key)
            if limit and len(keys) >= limit:
                break
        return keys

    def presigned_put(
        self,
        key: str,
        expires: timedelta = timedelta(minutes=15),
        content_type: str | None = None,
    ) -> str:
        headers = self._headers(content_type)
        return self._bucket.sign_url(
            "PUT", key, int(expires.total_seconds()), headers=headers
        )

    def public_url(self, key: str) -> str:
        # Virtual-hosted-style URL derived from endpoint host.
        parsed = urlparse(settings.oss_endpoint)
        host = parsed.netloc or parsed.path
        scheme = parsed.scheme or "https"
        if self._is_cname:
            # Endpoint already includes the bucket hostname (accelerate CNAME).
            return f"{scheme}://{host}/{key}"
        return f"{scheme}://{self._bucket_name}.{host}/{key}"

    def presigned_get(
        self,
        key: str,
        expires: timedelta | None = None,
    ) -> str:
        # Aliyun OSS sign_url expects seconds; private buckets need GET signatures
        # for browser <img> (public_url alone returns AccessDenied).
        secs = (
            int(expires.total_seconds())
            if expires is not None
            else signed_get_expire_sec(years=20.0)
        )
        return self._bucket.sign_url("GET", key, secs)

    def uri_for(self, key: str) -> str:
        return f"oss://{self._bucket_name}/{key}"


def parcel_product_prefix() -> str:
    """Return the configured S1/S2 parcel product prefix with a trailing slash."""
    prefix = settings.oss_prefix or ""
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"
    return prefix


def configure_gdal_vsis3(storage: ObjectStorage | None = None) -> dict[str, str]:
    """Point rasterio/GDAL ``/vsis3/`` at the active object store.

    Returns the previous env values for keys we touch (caller may restore).
    Uses assignment (not setdefault) so stale process environment cannot stick
    to a previous storage configuration.
    """
    import os as _os

    storage = storage or get_storage()
    keys = (
        "AWS_S3_ENDPOINT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_VIRTUAL_HOSTING",
        "AWS_HTTPS",
        "AWS_NO_SIGN_REQUEST",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )
    previous = {k: _os.environ[k] for k in keys if k in _os.environ}

    parsed = urlparse(settings.oss_endpoint)
    endpoint = parsed.netloc or parsed.path or "oss-cn-beijing.aliyuncs.com"
    # Strip scheme leftovers
    endpoint = endpoint.replace("https://", "").replace("http://", "")
    # When using bucket-prefixed accelerate CNAME, GDAL still wants the bare
    # accelerate / regional host for virtual hosting.
    bucket = (settings.oss_bucket or "").strip().lower()
    if bucket and endpoint.lower().startswith(f"{bucket}."):
        endpoint = endpoint[len(bucket) + 1 :]
    _os.environ["AWS_S3_ENDPOINT"] = endpoint
    _os.environ["AWS_ACCESS_KEY_ID"] = settings.oss_access_key_id
    _os.environ["AWS_SECRET_ACCESS_KEY"] = settings.oss_access_key_secret
    _os.environ["AWS_VIRTUAL_HOSTING"] = "TRUE"
    _os.environ["AWS_HTTPS"] = "YES"
    _os.environ["AWS_NO_SIGN_REQUEST"] = "NO"
    _os.environ["AWS_REGION"] = settings.oss_region or "oss-cn-beijing"
    _os.environ["AWS_DEFAULT_REGION"] = settings.oss_region or "oss-cn-beijing"
    return previous


def restore_gdal_env(
    previous: dict[str, str], touched: tuple[str, ...] | None = None
) -> None:
    """Restore env keys after ``configure_gdal_vsis3`` / public S3 reads."""
    import os as _os

    keys = touched or (
        "AWS_S3_ENDPOINT",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_VIRTUAL_HOSTING",
        "AWS_HTTPS",
        "AWS_NO_SIGN_REQUEST",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )
    for k in keys:
        if k in previous:
            _os.environ[k] = previous[k]
        else:
            _os.environ.pop(k, None)


@lru_cache(maxsize=1)
def get_storage() -> ObjectStorage:
    """Return a cached storage backend instance for the configured backend."""
    backend = (settings.storage_backend or "oss").strip().lower()
    if backend != "oss":
        raise ValueError(
            f"Unsupported STORAGE_BACKEND={settings.storage_backend!r}; "
            "only 'oss' is supported"
        )
    return OssStorage()


@lru_cache(maxsize=1)
def get_parcel_product_storage() -> ObjectStorage:
    """Storage used to read S1/S2 parcel product JSON (`json_oss_key`).

    Parcel products live on Aliyun OSS (`agric-dev`). Prefer OSS whenever
    credentials are configured; otherwise fall back to ``get_storage()``.
    """
    if settings.oss_access_key_id and settings.oss_access_key_secret:
        return OssStorage()
    return get_storage()


def clear_storage_cache() -> None:
    """Clear the cached storage instance (mainly for tests)."""
    get_storage.cache_clear()
    get_parcel_product_storage.cache_clear()


__all__ = [
    "ObjectStorage",
    "ObjectTooLargeError",
    "OssStorage",
    "get_storage",
    "get_parcel_product_storage",
    "parcel_product_prefix",
    "clear_storage_cache",
    "configure_gdal_vsis3",
    "restore_gdal_env",
    "signed_get_expire_sec",
    "resolve_oss_is_cname",
    "is_transient_oss_error",
    "retry_oss_call",
    "oss_multipart_threshold_bytes",
    "oss_part_size_bytes",
    "oss_resumable_store_root",
]
