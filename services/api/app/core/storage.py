"""Switchable object storage: MinIO (S3) or Aliyun OSS.

Selected by ``settings.storage_backend`` (``minio`` | ``oss``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from functools import lru_cache
from io import BytesIO
from urllib.parse import urlparse

import oss2
from minio import Minio

from app.core.config import settings


class ObjectStorage(ABC):
    """Backend-agnostic object storage interface."""

    @property
    @abstractmethod
    def backend(self) -> str:
        """Backend name: ``minio`` or ``oss``."""

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
    def get_bytes(self, key: str) -> bytes:
        """Download object contents as bytes."""

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

    @abstractmethod
    def uri_for(self, key: str) -> str:
        """Canonical storage URI (``s3://`` or ``oss://``)."""


class MinioStorage(ObjectStorage):
    def __init__(self) -> None:
        self._bucket = settings.minio_bucket
        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        # Browser-reachable client for presigned URL signing (host in signature).
        signing_endpoint = settings.minio_public_endpoint or settings.minio_endpoint
        self._signing_client = Minio(
            signing_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
            region="us-east-1",
        )

    @property
    def backend(self) -> str:
        return "minio"

    @property
    def bucket(self) -> str:
        return self._bucket

    def upload_file(
        self,
        key: str,
        file_path: str,
        content_type: str | None = None,
    ) -> str:
        self._client.fput_object(
            self._bucket,
            key,
            file_path,
            content_type=content_type or "application/octet-stream",
        )
        return key

    def download_file(self, key: str, file_path: str) -> None:
        self._client.fget_object(self._bucket, key, file_path)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
    ) -> str:
        self._client.put_object(
            self._bucket,
            key,
            BytesIO(data),
            length=len(data),
            content_type=content_type or "application/octet-stream",
        )
        return key

    def get_bytes(self, key: str) -> bytes:
        response = self._client.get_object(self._bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except Exception:
            return False

    def list_keys(
        self,
        prefix: str,
        suffix: str = "",
        limit: int = 0,
    ) -> list[str]:
        keys: list[str] = []
        for obj in self._client.list_objects(
            self._bucket, prefix=prefix, recursive=True
        ):
            key = obj.object_name
            if key is None:
                continue
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
        # MinIO Python SDK does not bind Content-Type into the signature here;
        # callers may still set it on the PUT request when allowed by CORS.
        _ = content_type
        return self._signing_client.presigned_put_object(
            self._bucket, key, expires=expires
        )

    def public_url(self, key: str) -> str:
        endpoint = settings.minio_public_endpoint or settings.minio_endpoint
        scheme = "https" if settings.minio_secure else "http"
        return f"{scheme}://{endpoint}/{self._bucket}/{key}"

    def uri_for(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"


class OssStorage(ObjectStorage):
    def __init__(self) -> None:
        if not settings.oss_access_key_id or not settings.oss_access_key_secret:
            raise ValueError(
                "OSS backend requires OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET"
            )
        auth = oss2.Auth(settings.oss_access_key_id, settings.oss_access_key_secret)
        self._bucket_name = settings.oss_bucket
        self._bucket = oss2.Bucket(auth, settings.oss_endpoint, self._bucket_name)

    @property
    def backend(self) -> str:
        return "oss"

    @property
    def bucket(self) -> str:
        return self._bucket_name

    def upload_file(
        self,
        key: str,
        file_path: str,
        content_type: str | None = None,
    ) -> str:
        headers = {"Content-Type": content_type} if content_type else None
        self._bucket.put_object_from_file(key, file_path, headers=headers)
        return key

    def download_file(self, key: str, file_path: str) -> None:
        self._bucket.get_object_to_file(key, file_path)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str | None = None,
    ) -> str:
        headers = {"Content-Type": content_type} if content_type else None
        self._bucket.put_object(key, data, headers=headers)
        return key

    def get_bytes(self, key: str) -> bytes:
        result = self._bucket.get_object(key)
        return result.read()

    def exists(self, key: str) -> bool:
        return bool(self._bucket.object_exists(key))

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
        headers = {"Content-Type": content_type} if content_type else None
        return self._bucket.sign_url(
            "PUT", key, int(expires.total_seconds()), headers=headers
        )

    def public_url(self, key: str) -> str:
        # Virtual-hosted-style URL derived from endpoint host.
        parsed = urlparse(settings.oss_endpoint)
        host = parsed.netloc or parsed.path
        scheme = parsed.scheme or "https"
        return f"{scheme}://{self._bucket_name}.{host}/{key}"

    def uri_for(self, key: str) -> str:
        return f"oss://{self._bucket_name}/{key}"


def parcel_product_prefix() -> str:
    """Return the configured S1/S2 parcel product prefix with a trailing slash."""
    prefix = settings.oss_prefix or ""
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"
    return prefix


@lru_cache(maxsize=1)
def get_storage() -> ObjectStorage:
    """Return a cached storage backend instance for the configured backend."""
    backend = (settings.storage_backend or "oss").strip().lower()
    if backend == "oss":
        return OssStorage()
    if backend == "minio":
        return MinioStorage()
    raise ValueError(
        f"Unsupported STORAGE_BACKEND={settings.storage_backend!r}; "
        "expected 'minio' or 'oss'"
    )


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
    "MinioStorage",
    "OssStorage",
    "get_storage",
    "get_parcel_product_storage",
    "parcel_product_prefix",
    "clear_storage_cache",
]
