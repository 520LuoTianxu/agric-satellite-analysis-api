"""Thin Celery client helpers for API routers (no task module imports)."""

from __future__ import annotations

from typing import Any

from agric_satellite_analysis_common.celery_app import task_queue_for
from app.worker import celery_app


def send_task(
    name: str,
    args: list[Any] | tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
    *,
    queue: str | None = None,
) -> Any:
    """Dispatch a Celery task by stable name (ingest/storage workers consume)."""
    options: dict[str, Any] = {}
    if queue:
        # 兼容历史 producer 的 queue=ingest，同时将已分类任务送入资源隔离队列。
        options["queue"] = task_queue_for(name, requested_queue=queue)
    return celery_app.send_task(name, args=args or (), kwargs=kwargs or {}, **options)
