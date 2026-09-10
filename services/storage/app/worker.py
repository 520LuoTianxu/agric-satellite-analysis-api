"""Celery worker for the storage queue only."""

from openfarm_common.celery_app import create_celery_app

celery_app = create_celery_app(
    name="openfarm-storage",
    include=["app.tasks.storage_tasks"],
    default_queue="storage",
)
