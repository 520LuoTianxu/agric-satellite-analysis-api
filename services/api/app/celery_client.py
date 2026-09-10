"""Thin Celery client helpers for API routers (no task module imports)."""

from __future__ import annotations

from typing import Any

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
        options["queue"] = queue
    return celery_app.send_task(name, args=args or (), kwargs=kwargs or {}, **options)
