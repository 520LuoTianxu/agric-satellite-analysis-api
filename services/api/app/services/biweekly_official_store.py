"""PostgreSQL adapter for official biweekly jobs.

The table is applied by scripts/20261123_biweekly_official_jobs.sql, not at
API startup. A missing table is an explicit OfficialStoreNotMigrated signal
so callers can fall back to legacy work_dir files. Any other database error
is OfficialStoreUnavailable and must not be treated as an empty job list.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from agric_satellite_analysis_common.biweekly.official_ref import (
    OfficialStoreNotMigrated,
    OfficialStoreUnavailable,
    public_artifact,
)
from agric_satellite_analysis_common.biweekly.official_store import apply_status_event

from app.models.tables import BiweeklyOfficialJob

logger = logging.getLogger(__name__)


def is_missing_official_table(exc: BaseException) -> bool:
    parts = [str(exc)]
    orig = getattr(exc, "orig", None)
    if orig is not None:
        parts.append(str(orig))
        if getattr(orig, "pgcode", None):
            parts.append(str(orig.pgcode))
    text = " ".join(parts).lower()
    if "biweekly_official_jobs" not in text and "42p01" not in text:
        return False
    return (
        "does not exist" in text
        or "undefinedtable" in text
        or "undefined table" in text
        or "42p01" in text
    )


def _parse_uuid(value: Any) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def row_to_job(row: BiweeklyOfficialJob) -> dict[str, Any]:
    job = deepcopy(row.job_json or {})
    job["job_id"] = row.job_id
    job["land_id"] = row.land_id
    job["season_id"] = row.season_id
    job["period_id"] = row.period_id
    job["kind"] = row.kind
    job["status"] = row.status
    if row.report_job_id is not None:
        job["report_job_id"] = str(row.report_job_id)
    if row.artifact_json is not None:
        job["artifact"] = public_artifact(row.artifact_json)
    job["persistence"] = "database"
    return job


def _fill_row(row: BiweeklyOfficialJob, job: dict[str, Any]) -> None:
    clean = dict(job)
    if clean.get("artifact"):
        clean["artifact"] = public_artifact(clean["artifact"])
    row.job_id = clean["job_id"]
    row.land_id = str(clean["land_id"])
    row.season_id = clean["season_id"]
    row.period_id = str(clean["period_id"])
    row.kind = clean.get("kind") or "generate"
    row.status = clean["status"]
    row.report_job_id = _parse_uuid(clean.get("report_job_id") or (clean.get("payload") or {}).get("report_job_id"))
    row.artifact_json = clean.get("artifact")
    row.job_json = clean
    created = _parse_time(clean.get("created_at"))
    if created is not None:
        row.created_at = created
    row.updated_at = _parse_time(clean.get("updated_at")) or datetime.now(timezone.utc)


class SqlAlchemyOfficialStore:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def _translate(self, exc: BaseException) -> OfficialStoreUnavailable | OfficialStoreNotMigrated:
        if is_missing_official_table(exc):
            return OfficialStoreNotMigrated()
        return OfficialStoreUnavailable(str(type(exc).__name__))

    def get(self, job_id: str) -> dict[str, Any] | None:
        try:
            with self._session() as db:
                row = db.get(BiweeklyOfficialJob, job_id)
                return row_to_job(row) if row is not None else None
        except OfficialStoreUnavailable:
            raise
        except (ProgrammingError, SQLAlchemyError) as exc:
            raise self._translate(exc) from exc

    def upsert(self, job: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._session() as db:
                row = db.get(BiweeklyOfficialJob, job["job_id"])
                if row is None:
                    row = BiweeklyOfficialJob(job_id=job["job_id"])
                    db.add(row)
                _fill_row(row, job)
                db.commit()
                db.refresh(row)
                return row_to_job(row)
        except (ProgrammingError, SQLAlchemyError) as exc:
            raise self._translate(exc) from exc

    def apply_event(self, job_id: str, event: dict[str, Any]) -> tuple[dict[str, Any], str]:
        try:
            with self._session() as db:
                row = (
                    db.execute(
                        select(BiweeklyOfficialJob)
                        .where(BiweeklyOfficialJob.job_id == job_id)
                        .with_for_update()
                    )
                    .scalar_one_or_none()
                )
                current = row_to_job(row) if row is not None else None
                next_job, outcome = apply_status_event(current, {**event, "job_id": job_id})
                if outcome in {"ignored_stale_fail", "idempotent"}:
                    db.rollback()
                    return current or next_job, outcome
                if row is None:
                    row = BiweeklyOfficialJob(job_id=job_id)
                    db.add(row)
                _fill_row(row, next_job)
                db.commit()
                db.refresh(row)
                return row_to_job(row), outcome
        except (ProgrammingError, SQLAlchemyError) as exc:
            raise self._translate(exc) from exc

    def latest_for_period(
        self, land_id: str, season_id: str, period_id: str | int
    ) -> dict[str, Any] | None:
        try:
            with self._session() as db:
                row = db.execute(
                    select(BiweeklyOfficialJob)
                    .where(
                        BiweeklyOfficialJob.land_id == str(land_id),
                        BiweeklyOfficialJob.season_id == season_id,
                        BiweeklyOfficialJob.period_id == str(period_id),
                        BiweeklyOfficialJob.kind == "generate",
                    )
                    .order_by(BiweeklyOfficialJob.created_at.asc())
                ).scalars().all()
                return row_to_job(row[-1]) if row else None
        except (ProgrammingError, SQLAlchemyError) as exc:
            raise self._translate(exc) from exc

    def list_for_land_season(self, land_id: str, season_id: str) -> list[dict[str, Any]]:
        try:
            with self._session() as db:
                rows = db.execute(
                    select(BiweeklyOfficialJob).where(
                        BiweeklyOfficialJob.land_id == str(land_id),
                        BiweeklyOfficialJob.season_id == season_id,
                    )
                ).scalars().all()
                return [row_to_job(item) for item in rows]
        except (ProgrammingError, SQLAlchemyError) as exc:
            raise self._translate(exc) from exc


def build_official_store():
    from app.core.database_sync import SyncSession

    return SqlAlchemyOfficialStore(SyncSession)
