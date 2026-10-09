"""Celery worker for the ingest queue (download + raster compute)."""

from celery.signals import worker_process_shutdown

from agric_satellite_analysis_common.celery_app import CPU_COMPUTE_QUEUE, create_celery_app
from agric_satellite_analysis_common.internal_api import close_cached_result_client
from agric_satellite_analysis_common.logging import setup_logging

setup_logging()
# 结果回执连接池属于worker子进程资源，进程退出时主动释放长连接。
worker_process_shutdown.connect(
    close_cached_result_client,
    weak=False,
    dispatch_uid="close_cached_result_http_client",
)

INGEST_INCLUDES = [
    "app.tasks.ndvi",
    "app.tasks.vegetation",
    "app.tasks.weather",
    "app.tasks.backfill",
    "app.tasks.soil",
    "app.tasks.agri_bridge",
    "app.tasks.agri_lonlat",
    "app.tasks.satellite_batch",
    "app.tasks.decloud_uncrtaints",
    "app.tasks.decloud_schedule_outbox",
    "app.tasks.agri_alerts",
    "app.tasks.sentinel1",
    "app.tasks.assessment_report",
    "app.tasks.season_growth_report",
    "app.tasks.overview_preagg",
    "app.tasks.satellite_history",
]

celery_app = create_celery_app(
    name="openfarm-ingest",
    include=INGEST_INCLUDES,
    default_queue=CPU_COMPUTE_QUEUE,
)
