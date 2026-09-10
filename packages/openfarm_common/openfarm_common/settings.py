"""Shared env settings for workers and object storage."""

from __future__ import annotations

from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CommonSettings(BaseSettings):
    """Subset of env used by ingest/storage and ObjectStorage."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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

    # CloudAMQP outer task bus (never commit real URL)
    cloudamqp_url: str = ""
    # Download queue: API publishes TaskMessage; mq_consumer + ingest consume
    cloudamqp_download_queue: str = "openfarm_download"
    # Process queue: workers publish ResultMessage; mq_result_writer consumes
    cloudamqp_process_queue: str = "openfarm_process"
    # One-release backward-compat aliases (env: CLOUDAMQP_TASK_QUEUE / CLOUDAMQP_RESULT_QUEUE)
    cloudamqp_task_queue: str = ""
    cloudamqp_result_queue: str = ""

    @model_validator(mode="after")
    def _apply_queue_aliases(self) -> Self:
        """Prefer DOWNLOAD/PROCESS env; fall back to TASK/RESULT for one release."""
        fields_set = self.model_fields_set
        download = self.cloudamqp_download_queue
        if "cloudamqp_download_queue" not in fields_set and self.cloudamqp_task_queue:
            download = self.cloudamqp_task_queue
        process = self.cloudamqp_process_queue
        if "cloudamqp_process_queue" not in fields_set and self.cloudamqp_result_queue:
            process = self.cloudamqp_result_queue
        object.__setattr__(self, "cloudamqp_download_queue", download)
        object.__setattr__(self, "cloudamqp_process_queue", process)
        # Keep old attribute names pointing at resolved queues for leftover callers
        object.__setattr__(self, "cloudamqp_task_queue", download)
        object.__setattr__(self, "cloudamqp_result_queue", process)
        return self


settings = CommonSettings()


def sync_database_url() -> str:
    """Return a sync SQLAlchemy URL (psycopg2)."""
    if settings.database_url_sync:
        return settings.database_url_sync
    return settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
