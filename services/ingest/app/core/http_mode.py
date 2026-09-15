"""Download-host HTTP-only helpers (no SyncSession when PG is disabled)."""

from __future__ import annotations

from typing import Any


def ingest_http_only() -> bool:
    """True when download must not open SyncSession (reads or writes)."""
    try:
        from openfarm_common.internal_api import (
            ingest_pg_reads_allowed,
            ingest_pg_writes_enabled,
            internal_api_enabled,
        )
    except ImportError:
        return False

    if not internal_api_enabled():
        return False
    if not ingest_pg_writes_enabled() or not ingest_pg_reads_allowed():
        return True
    return False


def resolve_field_http(field_id: str) -> dict[str, Any]:
    """GET /v1/internal/fields/resolve for tags / land_id."""
    from openfarm_common.internal_api import resolve_field

    data = resolve_field(field_id=str(field_id))
    if not isinstance(data, dict):
        raise RuntimeError("fields/resolve returned non-object")
    return data


def field_geom_http(field_id: str, *, include_geojson: bool = True) -> dict[str, Any]:
    from openfarm_common.internal_api import field_geom

    data = field_geom(str(field_id), include_geojson=include_geojson)
    if not isinstance(data, dict):
        raise RuntimeError("fields/geom returned non-object")
    return data


def patch_job_http(job_id: str | None, body: dict[str, Any]) -> None:
    if not job_id:
        return
    from openfarm_common.internal_api import patch_job

    patch_job(str(job_id), body)


def get_job_http(job_id: str) -> dict[str, Any] | None:
    try:
        from openfarm_common.internal_api import get_job, internal_api_enabled

        if not internal_api_enabled():
            return None
        data = get_job(str(job_id))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
