"""Celery app factory shared by api (client), ingest, and storage workers."""

from __future__ import annotations

import socket
from typing import Any

from celery import Celery
from celery.schedules import crontab

from openfarm_common.settings import CommonSettings, settings

# Shared task routes — must stay stable across services.
TASK_ROUTES: dict[str, dict[str, str]] = {
    "app.tasks.weather.*": {"queue": "ingest"},
    "app.tasks.soil.*": {"queue": "ingest"},
    "app.tasks.pipeline.*": {"queue": "ingest"},
    "app.tasks.sentinel1.*": {"queue": "ingest"},
    "app.tasks.vegetation.*": {"queue": "ingest"},
    "app.tasks.ndvi.*": {"queue": "ingest"},
    "app.tasks.indices.*": {"queue": "ingest"},
    "app.tasks.agri_bridge.*": {"queue": "ingest"},
    "app.tasks.bridge_stac_cogs_to_agri_lonlat.*": {"queue": "ingest"},
    "app.tasks.backfill.*": {"queue": "ingest"},
    "app.tasks.agri_alerts.*": {"queue": "ingest"},
    "app.tasks.assessment_report.*": {"queue": "ingest"},
    "app.tasks.overview_preagg.*": {"queue": "ingest"},
    "app.tasks.storage.*": {"queue": "storage"},
}

BEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    "compute-indices-weekly": {
        "task": "app.tasks.backfill.schedule_weekly_index_compute",
        "schedule": crontab(hour=6, minute=0, day_of_week=1),
    },
    "fetch-weather-daily": {
        "task": "app.tasks.weather.schedule_daily_weather_fetch",
        "schedule": crontab(hour=8, minute=0),
    },
    "refresh-overview-stats-daily": {
        "task": "app.tasks.overview_preagg.refresh_overview_stats",
        "schedule": crontab(hour=18, minute=30),
    },
}

# Linux default TCP_KEEPIDLE is 7200s. Remote Redis and nested Docker NAT
# often drop idle sockets much sooner; these probes surface a dead
# connection in about 90s instead of leaving BRPOP / restore_visible hung.
_TCP_KEEPIDLE_SECONDS = 60
_TCP_KEEPINTVL_SECONDS = 10
_TCP_KEEPCNT = 3


def redis_socket_keepalive_options(
    *,
    keepidle: int = _TCP_KEEPIDLE_SECONDS,
    keepintvl: int = _TCP_KEEPINTVL_SECONDS,
    keepcnt: int = _TCP_KEEPCNT,
) -> dict[int, int]:
    """Return TCP keepalive options for redis-py, skipping flags the OS lacks."""
    opts: dict[int, int] = {}
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
        socket, "TCP_KEEPALIVE", None
    )
    interval = getattr(socket, "TCP_KEEPINTVL", None)
    count = getattr(socket, "TCP_KEEPCNT", None)
    if idle is not None:
        opts[int(idle)] = keepidle
    if interval is not None:
        opts[int(interval)] = keepintvl
    if count is not None:
        opts[int(count)] = keepcnt
    return opts


def celery_redis_transport_options(
    cfg: CommonSettings | None = None,
) -> dict[str, Any]:
    """Kombu Redis broker options shared by every Celery app in this repo.

    Kombu defaults ``socket_timeout=None`` (block forever) and does not enable
    TCP keepalive or ``retry_on_timeout``. A half-open remote Redis socket then
    stalls ``restore_visible`` / lock acquire / health-check PING, so the
    worker stops consuming while a fresh short-lived client still PINGs.
    """
    cfg = cfg or settings
    opts: dict[str, Any] = {
        "visibility_timeout": cfg.celery_broker_visibility_timeout,
        "socket_timeout": cfg.celery_redis_socket_timeout,
        "socket_connect_timeout": cfg.celery_redis_socket_connect_timeout,
        "socket_keepalive": cfg.celery_redis_socket_keepalive,
        "retry_on_timeout": cfg.celery_redis_retry_on_timeout,
        "health_check_interval": cfg.celery_redis_health_check_interval,
    }
    if cfg.celery_redis_socket_keepalive:
        keepalive = redis_socket_keepalive_options()
        if keepalive:
            opts["socket_keepalive_options"] = keepalive
    return opts


def celery_app_config(cfg: CommonSettings | None = None) -> dict[str, Any]:
    """Celery conf keys that harden Redis broker and result-backend sockets."""
    cfg = cfg or settings
    transport = celery_redis_transport_options(cfg)
    backend_transport = {
        key: value for key, value in transport.items() if key != "visibility_timeout"
    }
    return {
        "broker_transport_options": transport,
        "result_backend_transport_options": backend_transport,
        "broker_connection_retry": True,
        "broker_connection_retry_on_startup": True,
        "broker_connection_max_retries": cfg.celery_broker_connection_max_retries,
        "broker_connection_timeout": cfg.celery_redis_socket_connect_timeout,
        "broker_channel_error_retry": True,
        "redis_retry_on_timeout": cfg.celery_redis_retry_on_timeout,
        "redis_socket_keepalive": cfg.celery_redis_socket_keepalive,
        "redis_socket_timeout": cfg.celery_redis_socket_timeout,
        "redis_socket_connect_timeout": cfg.celery_redis_socket_connect_timeout,
        "redis_backend_health_check_interval": cfg.celery_redis_health_check_interval,
    }


def create_celery_app(
    *,
    name: str = "openfarm",
    include: list[str] | None = None,
    default_queue: str = "ingest",
    with_beat_schedule: bool = False,
) -> Celery:
    """Build a Celery app with shared broker/routes.

    API uses this as a **client** (empty include). Workers pass their task modules.
    """
    app = Celery(
        name,
        broker=settings.redis_url,
        backend=settings.redis_url,
    )
    conf: dict[str, Any] = {
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "worker_concurrency": 4,
        "task_time_limit": 1800,
        "task_soft_time_limit": 1500,
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "task_default_queue": default_queue,
        "task_routes": TASK_ROUTES,
        "include": include or [],
    }
    conf.update(celery_app_config())
    if with_beat_schedule:
        conf["beat_schedule"] = BEAT_SCHEDULE
    app.conf.update(conf)
    return app


# Lightweight client for send_task helpers (api + ingest callers).
celery_client = create_celery_app(
    name="openfarm-client", include=[], default_queue="ingest"
)
