"""Explicit LOCAL/OFFLINE API-control-plane store. Never instantiate on download host.

Production persistence/transaction adapters are intentionally not implemented.
No import-time connection, environment lookup, Postgres/Redis or scheduler.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .models import (
    Artifact,
    JobOptions,
    Lease,
    Narrative,
    ReportError,
    Snapshot,
    canonical,
    digest,
)


class OfflineSqliteRepository:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS snapshots (sha TEXT PRIMARY KEY, body TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, snapshot_sha TEXT NOT NULL, options TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, token TEXT, expires REAL, error TEXT, artifact TEXT, narrative TEXT, invalidated TEXT)"
            )

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(connection, job_id):
        row = connection.execute(
            "SELECT j.*, s.body FROM jobs j JOIN snapshots s ON s.sha=j.snapshot_sha WHERE j.id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ReportError("JOB_NOT_FOUND")
        value = dict(row)
        value["snapshot"] = Snapshot(value.pop("body"), value["snapshot_sha"])
        value["options"] = JobOptions(**json.loads(value["options"]))
        value["artifact"] = json.loads(value["artifact"]) if value["artifact"] else None
        return value

    def create(self, snapshot: Snapshot, options: JobOptions):
        job_id = "bwjob-" + digest(
            canonical({"snapshot": snapshot.sha256, "options": options.identity()})
        )
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?,?)",
                (snapshot.sha256, snapshot.canonical_json),
            )
            connection.execute(
                "INSERT OR IGNORE INTO jobs (id,snapshot_sha,options,state) VALUES (?,?,?,'queued')",
                (job_id, snapshot.sha256, canonical(asdict(options))),
            )
            return self._row(connection, job_id)

    def get(self, job_id):
        with self._transaction() as connection:
            return self._row(connection, job_id)

    def claim(self, job_id, seconds=600):
        if not 0 < seconds <= 3600:
            raise ReportError("LEASE_TTL_INVALID")
        with self._transaction() as connection:
            row = self._row(connection, job_id)
            if row["invalidated"]:
                return None
            if row["state"] == "running" and row["expires"] <= self.clock():
                connection.execute(
                    "UPDATE jobs SET state='failed',error='LEASE_EXPIRED',token=NULL WHERE id=?",
                    (job_id,),
                )
                return None
            if row["state"] != "queued":
                return None
            if row["attempts"] >= row["options"].max_attempts:
                raise ReportError("RETRY_EXHAUSTED")
            token = uuid.uuid4().hex
            connection.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1,token=?,expires=?,error=NULL WHERE id=?",
                (token, self.clock() + seconds, job_id),
            )
            return Lease(job_id, token, row["snapshot"], row["options"])

    def _active(self, connection, lease):
        row = self._row(connection, lease.job_id)
        if row["invalidated"]:
            raise ReportError("SNAPSHOT_STALE")
        if (
            row["state"] != "running"
            or row["token"] != lease.token
            or row["expires"] <= self.clock()
            or row["snapshot_sha"] != lease.snapshot.sha256
        ):
            raise ReportError("LEASE_STALE")
        return row

    def fail(self, lease: Lease, code: str):
        codes = {
            "NARRATIVE_INVALID",
            "PROVIDER_UNAVAILABLE",
            "PROVENANCE_MISMATCH",
            "PDF_RENDER_FAILED",
            "FONT_UNAVAILABLE",
            "WORKER_FAILED",
        }
        if code not in codes:
            code = "WORKER_FAILED"
        with self._transaction() as connection:
            self._active(connection, lease)
            connection.execute(
                "UPDATE jobs SET state='failed',error=?,token=NULL WHERE id=?",
                (code, lease.job_id),
            )

    def complete(self, lease: Lease, artifact: Artifact, narrative: Narrative):
        with self._transaction() as connection:
            row = self._active(connection, lease)
            options = row["options"]
            if (
                artifact.job_id != lease.job_id
                or artifact.snapshot_sha256 != row["snapshot_sha"]
                or artifact.template_version != options.template_version
                or artifact.model_version != options.model_version
                or artifact.prompt_version != options.prompt_version
                or artifact.narrative_sha256 != digest(narrative.canonical_json)
                or narrative.mode != options.narrative_mode
                or narrative.model_version != options.model_version
                or narrative.prompt_version != options.prompt_version
            ):
                raise ReportError("PROVENANCE_MISMATCH")
            if (
                artifact.size_bytes <= 0
                or not 0 < artifact.pages <= 1000
                or artifact.mime != "application/pdf"
            ):
                raise ReportError("ARTIFACT_INVALID")
            connection.execute(
                "UPDATE jobs SET state='ready',artifact=?,narrative=?,token=NULL,error=NULL WHERE id=?",
                (canonical(asdict(artifact)), narrative.canonical_json, lease.job_id),
            )

    def retry(self, job_id):
        with self._transaction() as connection:
            row = self._row(connection, job_id)
            if row["invalidated"]:
                raise ReportError("SNAPSHOT_STALE")
            expired = row["state"] == "running" and row["expires"] <= self.clock()
            if row["state"] != "failed" and not expired:
                raise ReportError("RETRY_NOT_ALLOWED")
            if row["attempts"] >= row["options"].max_attempts:
                raise ReportError("RETRY_EXHAUSTED")
            connection.execute(
                "UPDATE jobs SET state='queued',token=NULL,expires=NULL WHERE id=?",
                (job_id,),
            )
            return self._row(connection, job_id)

    def invalidate(self, job_id, reason):
        if reason not in {"SOURCE_CHANGED", "POLICY_REVOKED"}:
            raise ReportError("INVALIDATION_REASON_INVALID")
        with self._transaction() as connection:
            self._row(connection, job_id)
            connection.execute(
                "UPDATE jobs SET invalidated=?,token=NULL WHERE id=?", (reason, job_id)
            )
