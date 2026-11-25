"""Authoritative official biweekly job records, independent of /tmp job JSON.

Memory and durable JSON snapshot stores are for tests and local verification.
The API production adapter writes PostgreSQL agric_satellite.biweekly_official_jobs.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .models import ReportError
from .official_ref import (
    IN_FLIGHT_STATUSES,
    OFFICIAL_STATES,
    READY_STATUSES,
    OfficialStoreUnavailable,
    public_artifact,
    same_artifact,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def _same_oss_location(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    """True when both refs name the same OSS object (bucket + key)."""
    if not left or not right:
        return False
    bucket = left.get("bucket")
    key = left.get("key")
    if not bucket or not key:
        return False
    return bucket == right.get("bucket") and key == right.get("key")


def apply_status_event(current: dict[str, Any] | None, event: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Pure official-job state machine. Returns (next_job, outcome).

    Outcomes:
      applied, idempotent, ignored_stale_fail, recovered, replaced, conflict, created
    """
    incoming_status = event.get("status")
    if incoming_status not in OFFICIAL_STATES:
        raise ReportError("STATE_INVALID")
    incoming_artifact = public_artifact(event.get("artifact"))
    now = event.get("updated_at") or _now()

    if current is None:
        job = deepcopy(event)
        job["updated_at"] = now
        job.setdefault("created_at", now)
        job.setdefault("history", [])
        if not job["history"] or job["history"][-1].get("status") != incoming_status:
            job["history"] = list(job.get("history") or []) + [{"status": incoming_status, "at": now}]
        if incoming_artifact is not None:
            job["artifact"] = incoming_artifact
        job["status"] = incoming_status
        return job, "created"

    job = deepcopy(current)
    current_status = job.get("status")
    current_artifact = job.get("artifact") or {}

    if incoming_status == "failed":
        if current_status in READY_STATUSES:
            return current, "ignored_stale_fail"
        # A valid registered artifact must not be wiped by a late fail either.
        if current_status == "registration_pending" and current_artifact.get("sha256"):
            return current, "ignored_stale_fail"
        if current_status == "failed":
            return current, "idempotent"
        job["status"] = "failed"
        job["updated_at"] = now
        job["error_code"] = event.get("error_code")
        if event.get("result") is not None:
            job["result"] = event["result"]
        elif event.get("note"):
            result = dict(job.get("result") or {})
            result["note"] = event["note"]
            job["result"] = result
        job.setdefault("history", []).append({"status": "failed", "at": now, "error_code": event.get("error_code")})
        return job, "applied"

    if incoming_status in READY_STATUSES:
        if current_status in READY_STATUSES:
            if same_artifact(current_artifact, incoming_artifact) or (
                not incoming_artifact and current_artifact
            ):
                return current, "idempotent"
            if incoming_artifact is not None and _same_oss_location(
                current_artifact, incoming_artifact
            ):
                # Same OSS object was overwritten in place; update checksum
                # metadata and stay ready. A different bucket/key still conflicts.
                job["status"] = incoming_status
                job["updated_at"] = now
                job["artifact"] = incoming_artifact
                job["error_code"] = event.get("error_code")
                if event.get("result") is not None:
                    job["result"] = event["result"]
                if event.get("note"):
                    result = dict(job.get("result") or {})
                    result["note"] = event["note"]
                    job["result"] = result
                if event.get("facts") is not None:
                    job["facts"] = event["facts"]
                job.setdefault("history", []).append(
                    {"status": incoming_status, "at": now, "outcome": "replaced"}
                )
                return job, "replaced"
            raise ReportError("ARTIFACT_CONFLICT")
        outcome = "recovered" if current_status == "failed" else "applied"
        job["status"] = incoming_status
        job["updated_at"] = now
        job["error_code"] = event.get("error_code")
        if incoming_artifact is not None:
            job["artifact"] = incoming_artifact
        if event.get("result") is not None:
            job["result"] = event["result"]
        if event.get("note"):
            result = dict(job.get("result") or {})
            result["note"] = event["note"]
            job["result"] = result
        if event.get("facts") is not None:
            job["facts"] = event["facts"]
        job.setdefault("history", []).append({"status": incoming_status, "at": now, "outcome": outcome})
        return job, outcome

    if incoming_status == "registration_pending":
        if current_status in READY_STATUSES:
            return current, "idempotent"
        job["status"] = "registration_pending"
        job["updated_at"] = now
        if incoming_artifact is not None:
            job["artifact"] = incoming_artifact
        if event.get("result") is not None:
            job["result"] = event["result"]
        job.setdefault("history", []).append({"status": "registration_pending", "at": now})
        return job, "applied"

    # Other in-flight updates cannot downgrade a finished or pending-register job.
    if current_status in READY_STATUSES:
        return current, "idempotent"
    if current_status == "registration_pending" and incoming_status in IN_FLIGHT_STATUSES - {"registration_pending"}:
        return current, "ignored_stale_fail"
    job["status"] = incoming_status
    job["updated_at"] = now
    if event.get("result") is not None:
        job["result"] = event["result"]
    if event.get("payload") is not None:
        job["payload"] = event["payload"]
    job.setdefault("history", []).append({"status": incoming_status, "at": now})
    return job, "applied"


class OfficialJobStore(Protocol):
    def get(self, job_id: str) -> dict[str, Any] | None: ...

    def upsert(self, job: dict[str, Any]) -> dict[str, Any]: ...

    def apply_event(self, job_id: str, event: dict[str, Any]) -> tuple[dict[str, Any], str]: ...

    def latest_for_period(
        self, land_id: str, season_id: str, period_id: str | int
    ) -> dict[str, Any] | None: ...

    def list_for_land_season(self, land_id: str, season_id: str) -> list[dict[str, Any]]: ...


class MemoryOfficialStore:
    """Thread-safe in-memory store used by unit tests and as a composition base."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.broken = False

    def _check(self) -> None:
        if self.broken:
            raise OfficialStoreUnavailable("injected store failure")

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._check()
            job = self._jobs.get(job_id)
            return deepcopy(job) if job is not None else None

    def upsert(self, job: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._check()
            stored = deepcopy(job)
            stored.setdefault("persistence", "database")
            self._jobs[stored["job_id"]] = stored
            return deepcopy(stored)

    def apply_event(self, job_id: str, event: dict[str, Any]) -> tuple[dict[str, Any], str]:
        with self._lock:
            self._check()
            current = self._jobs.get(job_id)
            next_job, outcome = apply_status_event(current, {**event, "job_id": job_id})
            if outcome in {"ignored_stale_fail", "idempotent", "conflict"}:
                # conflict raises; ignored/idempotent keep the stored row.
                return deepcopy(current or next_job), outcome
            next_job.setdefault("persistence", "database")
            self._jobs[job_id] = next_job
            return deepcopy(next_job), outcome

    def latest_for_period(
        self, land_id: str, season_id: str, period_id: str | int
    ) -> dict[str, Any] | None:
        with self._lock:
            self._check()
            matches = [
                job
                for job in self._jobs.values()
                if job.get("land_id") == str(land_id)
                and job.get("season_id") == season_id
                and str(job.get("period_id")) == str(period_id)
                and job.get("kind") == "generate"
            ]
            if not matches:
                return None
            matches.sort(key=lambda job: job.get("created_at") or "")
            return deepcopy(matches[-1])

    def list_for_land_season(self, land_id: str, season_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._check()
            return [
                deepcopy(job)
                for job in self._jobs.values()
                if job.get("land_id") == str(land_id) and job.get("season_id") == season_id
            ]

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {key: deepcopy(job) for key, job in self._jobs.items()}

    def restore(self, jobs: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self._jobs = {key: deepcopy(job) for key, job in jobs.items()}


class DurableJsonOfficialStore(MemoryOfficialStore):
    """File-backed snapshot of the memory store, used to simulate API restart."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        if self.path.is_file():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.restore(raw)

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def upsert(self, job: dict[str, Any]) -> dict[str, Any]:
        stored = super().upsert(job)
        self._persist()
        return stored

    def apply_event(self, job_id: str, event: dict[str, Any]) -> tuple[dict[str, Any], str]:
        stored, outcome = super().apply_event(job_id, event)
        self._persist()
        return stored, outcome


def latest_brief(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "created_at": job.get("created_at"),
        "reference": bool((job.get("payload") or {}).get("reference_planting_instance_id")),
        "persistence": job.get("persistence") or "database",
    }


class UnavailableOfficialStore:
    """Fail closed when the official store cannot be constructed.

    Callers must not treat this as an empty work_dir catalog.
    """

    def __init__(self, reason: str = "official job store unavailable"):
        self.reason = reason

    def _raise(self):
        raise OfficialStoreUnavailable(self.reason)

    def get(self, job_id: str):
        self._raise()

    def upsert(self, job: dict):
        self._raise()

    def apply_event(self, job_id: str, event: dict):
        self._raise()

    def latest_for_period(self, land_id: str, season_id: str, period_id):
        self._raise()

    def list_for_land_season(self, land_id: str, season_id: str):
        self._raise()
