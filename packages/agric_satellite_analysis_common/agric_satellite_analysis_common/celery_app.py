"""Celery app factory shared by api (client), ingest, and storage workers."""

from __future__ import annotations

import socket
from typing import Any

from celery import Celery
from celery.schedules import crontab

from agric_satellite_analysis_common.settings import CommonSettings, settings
from agric_satellite_analysis_common.task_priority import (
    CELERY_PRIORITY_STEPS,
    TASK_PRIORITY_MAX,
    install_priority_signals,
)

# 业务队列按资源类型隔离：卫星数据任务不能堵住 CPU 计算任务，反之亦然。
SATELLITE_DOWNLOAD_QUEUE = "satellite_download"
CPU_COMPUTE_QUEUE = "cpu_compute"
LEGACY_INGEST_QUEUE = "ingest"

SATELLITE_TASK_PREFIXES = (
    "app.tasks.agri_lonlat.",
    "app.tasks.sentinel1.",
    "app.tasks.satellite_batch.",
)

CPU_TASK_PREFIXES = (
    "app.tasks.backfill.",
    "app.tasks.weather.",
    "app.tasks.soil.",
    "app.tasks.pipeline.",
    "app.tasks.vegetation.",
    "app.tasks.ndvi.",
    "app.tasks.indices.",
    "app.tasks.agri_bridge.",
    "app.tasks.bridge_stac_cogs_to_agri_lonlat.",
    "app.tasks.agri_alerts.",
    "app.tasks.assessment_report.",
    "app.tasks.season_growth_report.",
    "app.tasks.overview_preagg.",
)


def task_queue_for(
    task_name: str,
    *,
    requested_queue: str | None = None,
) -> str:
    """按任务资源类型选择队列，保留显式 storage/decloud 队列。"""
    # 旧调用方大量显式传 queue=ingest；这里统一把已识别任务迁移到新队列，
    # 避免遗漏某个 producer 后又把卫星任务塞回拥堵的旧 ingest 队列。
    if requested_queue and requested_queue != LEGACY_INGEST_QUEUE:
        return requested_queue
    if task_name.startswith(SATELLITE_TASK_PREFIXES):
        return SATELLITE_DOWNLOAD_QUEUE
    if task_name.startswith(CPU_TASK_PREFIXES):
        return CPU_COMPUTE_QUEUE
    return requested_queue or LEGACY_INGEST_QUEUE


# Shared task routes — must stay stable across services.
TASK_ROUTES: dict[str, dict[str, str]] = {
    "app.tasks.weather.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.soil.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.pipeline.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.sentinel1.*": {"queue": SATELLITE_DOWNLOAD_QUEUE},
    "app.tasks.satellite_batch.*": {"queue": SATELLITE_DOWNLOAD_QUEUE},
    "app.tasks.vegetation.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.ndvi.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.indices.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.agri_bridge.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.agri_lonlat.*": {"queue": SATELLITE_DOWNLOAD_QUEUE},
    "app.tasks.decloud_uncrtaints.*": {"queue": "decloud"},
    "app.tasks.bridge_stac_cogs_to_agri_lonlat.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.backfill.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.agri_alerts.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.assessment_report.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.season_growth_report.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.overview_preagg.*": {"queue": CPU_COMPUTE_QUEUE},
    "app.tasks.storage.*": {"queue": "storage"},
}

BEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    "fetch-weather-daily": {
        "task": "app.tasks.weather.schedule_daily_weather_fetch",
        "schedule": crontab(hour=8, minute=0),
    },
    "refresh-satellite-overview-daily": {
        "task": "app.tasks.overview_preagg.refresh_daily_satellite",
        # Celery显式使用UTC；17:00对应北京时间次日01:00，检查近7个自然日的S1/S2缺失观测。
        "schedule": crontab(hour=17, minute=0),
    },
    "refresh-overview-stats-daily": {
        "task": "app.tasks.overview_preagg.refresh_overview_stats",
        # Celery显式使用UTC；20:00对应北京时间04:00，避开卫星刷新后的数据准备窗口。
        "schedule": crontab(hour=20, minute=0),
    },
    "satellite-history-weekly": {
        "task": "app.tasks.satellite_history.schedule_satellite_history_backfill",
        # 周一 18:30 UTC = 北京时间周二 02:30；默认关闭，避免升级后自动拉取五年数据。
        "schedule": crontab(day_of_week=1, hour=18, minute=30),
    },
}

BEAT_SWITCHES = {
    "fetch-weather-daily": "schedule_daily_weather_enabled",
    "refresh-satellite-overview-daily": "schedule_daily_satellite_enabled",
    "refresh-overview-stats-daily": "schedule_overview_refresh_enabled",
    "satellite-history-weekly": "schedule_satellite_history_enabled",
}


def enabled_beat_schedule(
    cfg: CommonSettings | None = None,
) -> dict[str, dict[str, Any]]:
    """按env逐项注册周期任务，启动Beat本身不代表开启下载、天气或统计刷新。"""
    cfg = cfg or settings
    return {
        name: dict(entry)
        for name, entry in BEAT_SCHEDULE.items()
        if getattr(cfg, BEAT_SWITCHES[name])
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
        # Redis transport 以拆分 list 模拟优先级；细分为 0..9，避免 5/9
        # 等业务优先级被默认的 0/3/6/9 粗粒度合并。
        "priority_steps": list(CELERY_PRIORITY_STEPS),
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
        key: value
        for key, value in transport.items()
        if key not in ("visibility_timeout", "priority_steps")
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
    default_queue: str = CPU_COMPUTE_QUEUE,
    with_beat_schedule: bool = False,
) -> Celery:
    """构造共享 broker 与路由的 Celery app。

    API 当客户端用（include 为空）。ingest Beat 打开 with_beat_schedule，
    include 仍为空。worker 传入各自的任务模块。
    """
    app = Celery(
        name,
        broker=settings.redis_url,
        backend=settings.redis_url,
    )
    conf: dict[str, Any] = {
        "timezone": "UTC",
        "enable_utc": True,
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        # 只有空闲 worker 才会取下一条任务，避免普通任务预取后挡住报告请求。
        "worker_prefetch_multiplier": 1,
        "task_queue_max_priority": TASK_PRIORITY_MAX,
        # 任务内部继续派发的子任务继承父任务优先级，保证遥感 fan-out 不降级。
        "task_inherit_parent_priority": True,
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
        conf["beat_schedule"] = enabled_beat_schedule()
    app.conf.update(conf)
    from agric_satellite_analysis_common.trace import install_trace_signals

    install_trace_signals()
    install_priority_signals()
    return app


# Lightweight client for send_task helpers (api + ingest callers).
celery_client = create_celery_app(
    name="openfarm-client", include=[], default_queue=CPU_COMPUTE_QUEUE
)
