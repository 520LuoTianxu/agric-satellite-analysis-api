"""Process-wide trace_id for logs, MQ messages, Celery, and Internal HTTP."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import structlog

TRACE_HEADER = "X-Trace-Id"
TRACE_FIELD = "trace_id"
_MAX_LEN = 128
_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")

_SIGNALS_INSTALLED = False
_LOG_RECORD_FACTORY_INSTALLED = False


def new_trace_id() -> str:
    return str(uuid.uuid4())


def normalize_trace_id(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > _MAX_LEN or not _SAFE.fullmatch(text):
        return None
    return text


def current_trace_id() -> str | None:
    bound = structlog.contextvars.get_contextvars().get(TRACE_FIELD)
    return normalize_trace_id(bound)


def bind_trace_id(trace_id: object | None) -> str | None:
    normalized = normalize_trace_id(trace_id)
    if not normalized:
        structlog.contextvars.unbind_contextvars(TRACE_FIELD)
        return None
    structlog.contextvars.bind_contextvars(**{TRACE_FIELD: normalized})
    return normalized


def clear_trace_id() -> None:
    structlog.contextvars.unbind_contextvars(TRACE_FIELD)


def get_or_create_trace_id() -> str:
    existing = current_trace_id()
    if existing:
        return existing
    created = new_trace_id()
    bind_trace_id(created)
    return created


def extract_trace_id(mapping: dict[str, Any] | None) -> str | None:
    if not mapping:
        return None
    direct = normalize_trace_id(mapping.get(TRACE_FIELD))
    if direct:
        return direct
    extras = mapping.get("extras")
    if isinstance(extras, dict):
        return normalize_trace_id(extras.get(TRACE_FIELD))
    return None


def bind_trace_from_mapping(mapping: dict[str, Any] | None) -> str | None:
    return bind_trace_id(extract_trace_id(mapping))


def stamp_trace_on_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(payload or {})
    existing = normalize_trace_id(out.get(TRACE_FIELD))
    if existing:
        out[TRACE_FIELD] = existing
        return out
    current = current_trace_id()
    if current:
        out[TRACE_FIELD] = current
    return out


def attach_trace_header(request: Any) -> None:
    """httpx request hook: copy the bound trace_id onto Internal HTTP calls."""
    tid = current_trace_id()
    if not tid:
        return
    headers = getattr(request, "headers", None)
    if headers is None:
        return
    headers[TRACE_HEADER] = tid


def inject_trace_into_celery_headers(
    sender: Any = None,
    headers: dict[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    if headers is None:
        return
    tid = current_trace_id()
    if tid and not headers.get(TRACE_FIELD):
        headers[TRACE_FIELD] = tid


def bind_trace_from_celery_task(
    sender: Any = None,
    task: Any = None,
    kwargs: dict[str, Any] | None = None,
    **extra: Any,
) -> str:
    request = getattr(task, "request", None) if task is not None else None
    headers: dict[str, Any] = {}
    if request is not None:
        raw = getattr(request, TRACE_FIELD, None)
        bound = bind_trace_id(raw)
        if bound:
            return bound
        maybe_headers = getattr(request, "headers", None) or {}
        if isinstance(maybe_headers, dict):
            headers = maybe_headers
    found = extract_trace_id(headers) or extract_trace_id(kwargs)
    if found:
        bind_trace_id(found)
        return found
    return get_or_create_trace_id()


def _on_task_postrun(**kwargs: Any) -> None:
    clear_trace_id()


def install_trace_signals() -> None:
    """Attach Celery publish/prerun/postrun hooks once per process."""
    global _SIGNALS_INSTALLED
    if _SIGNALS_INSTALLED:
        return
    from celery.signals import before_task_publish, task_postrun, task_prerun

    before_task_publish.connect(inject_trace_into_celery_headers, weak=False)
    task_prerun.connect(bind_trace_from_celery_task, weak=False)
    task_postrun.connect(_on_task_postrun, weak=False)
    _SIGNALS_INSTALLED = True


def install_stdlib_trace_log_record() -> None:
    """Ensure stdlib LogRecord always has ``trace_id`` (``-`` when unbound)."""
    global _LOG_RECORD_FACTORY_INSTALLED
    if _LOG_RECORD_FACTORY_INSTALLED:
        return
    old_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        if not hasattr(record, TRACE_FIELD):
            setattr(record, TRACE_FIELD, current_trace_id() or "-")
        return record

    logging.setLogRecordFactory(factory)
    _LOG_RECORD_FACTORY_INSTALLED = True


__all__ = [
    "TRACE_FIELD",
    "TRACE_HEADER",
    "attach_trace_header",
    "bind_trace_from_celery_task",
    "bind_trace_from_mapping",
    "bind_trace_id",
    "clear_trace_id",
    "current_trace_id",
    "extract_trace_id",
    "get_or_create_trace_id",
    "inject_trace_into_celery_headers",
    "install_stdlib_trace_log_record",
    "install_trace_signals",
    "new_trace_id",
    "normalize_trace_id",
    "stamp_trace_on_payload",
]
