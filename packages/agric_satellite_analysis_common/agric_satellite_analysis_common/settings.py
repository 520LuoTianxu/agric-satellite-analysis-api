"""Shared env settings for workers and object storage."""

from __future__ import annotations

from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import ArgumentError


class CommonSettings(BaseSettings):
    """Subset of env used by ingest/storage and ObjectStorage."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True
    )

    redis_url: str = "redis://redis:6379/0"

    # 所有周期任务默认关闭，只有显式开启对应env开关时Beat才注册该任务。
    schedule_daily_satellite_enabled: bool = False
    schedule_daily_weather_enabled: bool = False
    schedule_overview_refresh_enabled: bool = False
    # 五年项目区历史回填默认关闭，开启后按周触发一次共享窗口补齐。
    schedule_virtual_area_history_enabled: bool = False

    # Celery / kombu Redis transport. Defaults match a remote broker over a
    # flaky path (download-machine, nested Docker NAT). 0 max retries = forever.
    celery_broker_visibility_timeout: int = 7200
    celery_redis_socket_timeout: float = 5.0
    celery_redis_socket_connect_timeout: float = 5.0
    celery_redis_socket_keepalive: bool = True
    celery_redis_retry_on_timeout: bool = True
    celery_redis_health_check_interval: int = 25
    celery_broker_connection_max_retries: int = 0

    # Sync DB (ingest Celery tasks)
    database_url: str = "postgresql+asyncpg://openfarm:openfarm_dev@db:5432/openfarm"
    database_url_sync: str = ""
    # 数据库只保留 agric_satellite；应用表和扩展对象均在此 schema。
    database_schema: str = "agric_satellite"

    # Production object storage backend is Aliyun OSS.
    storage_backend: str = "oss"

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


def resolve_sync_database_url(
    database_url_sync: str = "",
    database_url: str = "",
) -> str:
    """Build a psycopg2 SQLAlchemy URL from env-style strings.

    ``DATABASE_URL_SYNC`` wins when it is non-blank. Otherwise ``DATABASE_URL``
    is used after converting ``postgresql+asyncpg://`` to ``postgresql://``.
    """
    url = (database_url_sync or "").strip() or (database_url or "").strip()
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    url = url.replace("postgres+asyncpg://", "postgresql://")
    if not url:
        raise ValueError(
            "DATABASE_URL_SYNC or DATABASE_URL is required for the sync engine"
        )
    try:
        make_url(url)
    except ArgumentError as exc:
        raise ValueError(str(exc)) from exc
    return url


def sync_database_url() -> str:
    """Return a sync SQLAlchemy URL (psycopg2)."""
    return resolve_sync_database_url(settings.database_url_sync, settings.database_url)
