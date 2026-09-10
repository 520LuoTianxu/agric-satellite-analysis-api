"""Shared env settings for workers and object storage."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class CommonSettings(BaseSettings):
    """Subset of env used by ingest/storage and ObjectStorage."""

    redis_url: str = "redis://redis:6379/0"

    # Sync DB (ingest Celery tasks)
    database_url: str = "postgresql+asyncpg://openfarm:openfarm_dev@db:5432/openfarm"
    database_url_sync: str = ""

    # Object storage backend: oss | minio
    storage_backend: str = "oss"

    # MinIO
    minio_endpoint: str = "minio:9000"
    minio_public_endpoint: str = ""
    minio_access_key: str = "openfarm"
    minio_secret_key: str = "openfarm_dev_secret"
    minio_bucket: str = "openfarm"
    minio_secure: bool = False

    # Aliyun OSS
    oss_region: str = "oss-cn-beijing"
    oss_endpoint: str = "https://oss-cn-beijing.aliyuncs.com"
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    oss_bucket: str = "agric-dev"
    oss_prefix: str = "s1s2_parcel/json/"

    # Shared scratch volume between ingest and storage workers
    openfarm_scratch_dir: str = "/data/scratch"
    openfarm_storage_upload_timeout: float = 900.0

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = CommonSettings()


def sync_database_url() -> str:
    """Return a sync SQLAlchemy URL (psycopg2)."""
    if settings.database_url_sync:
        return settings.database_url_sync
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
