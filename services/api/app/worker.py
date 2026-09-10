"""Celery worker configuration - broker=Redis, per PRD Section 7.4.

Queues:
  - ``ingest``  — foreign-source download + raster compute (see design doc)
  - ``storage`` — OSS/MinIO put/get helpers only
"""

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "openfarm",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    # At-least-once delivery
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Visibility timeout > max job duration
    broker_transport_options={"visibility_timeout": 7200},
    # Concurrency - match 8 vCPU / 16 GB RAM spec
    worker_concurrency=4,
    # Timeouts
    task_time_limit=1800,  # 30 min hard kill
    task_soft_time_limit=1500,  # 25 min soft warning
    # Serialization
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Default all business work to ingest; storage.* routed below
    task_default_queue="ingest",
    task_routes={
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
    },
    # Task discovery
    include=[
        "app.tasks.ndvi",
        "app.tasks.vegetation",
        "app.tasks.weather",
        "app.tasks.backfill",
        "app.tasks.soil",
        "app.tasks.agri_bridge",
        "app.tasks.agri_alerts",
        "app.tasks.sentinel1",
        "app.tasks.assessment_report",
        "app.tasks.overview_preagg",
        "app.tasks.storage_tasks",
    ],
    # Celery Beat schedule (run via dedicated ``beat`` service or ingest -B)
    beat_schedule={
        "compute-indices-weekly": {
            "task": "app.tasks.backfill.schedule_weekly_index_compute",
            "schedule": crontab(hour=6, minute=0, day_of_week=1),
        },
        "fetch-weather-daily": {
            "task": "app.tasks.weather.schedule_daily_weather_fetch",
            "schedule": crontab(hour=8, minute=0),
        },
        # 02:30 Asia/Shanghai → 18:30 UTC (CST/CST no DST)
        "refresh-overview-stats-daily": {
            "task": "app.tasks.overview_preagg.refresh_overview_stats",
            "schedule": crontab(hour=18, minute=30),
        },
    },
)
