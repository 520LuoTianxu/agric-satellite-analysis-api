"""Celery client for the API process (send_task / beat).

Heavy task modules live in ``services/ingest`` and ``services/storage``.
This module must not import them — routers dispatch by stable task name.
"""

from openfarm_common.celery_app import create_celery_app

celery_app = create_celery_app(
    name="openfarm",
    include=[],
    default_queue="ingest",
    with_beat_schedule=True,
)
