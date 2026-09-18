"""Celery worker for the ingest queue (download + raster compute)."""

from agric_satellite_analysis_common.celery_app import CPU_COMPUTE_QUEUE, create_celery_app
from agric_satellite_analysis_common.logging import setup_logging

setup_logging()

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
    "app.tasks.agri_alerts",
    "app.tasks.sentinel1",
    "app.tasks.assessment_report",
    "app.tasks.season_growth_report",
    "app.tasks.overview_preagg",
]

celery_app = create_celery_app(
    name="openfarm-ingest",
    include=INGEST_INCLUDES,
    default_queue=CPU_COMPUTE_QUEUE,
)
