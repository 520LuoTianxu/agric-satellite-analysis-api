"""Celery app factory shared by api (client), ingest, and storage workers."""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.schedules import crontab

from openfarm_common.settings import settings

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
        "broker_transport_options": {"visibility_timeout": 7200},
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
    if with_beat_schedule:
        conf["beat_schedule"] = BEAT_SCHEDULE
    app.conf.update(conf)
    return app


# Lightweight client for send_task helpers (api + ingest callers).
celery_client = create_celery_app(
    name="openfarm-client", include=[], default_queue="ingest"
)
