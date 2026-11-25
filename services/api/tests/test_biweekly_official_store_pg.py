"""SQLAlchemy official store against an isolated real PostgreSQL.

These tests read the local isolated test container credentials from env or
/workspace/isolated/t9-oss-pg-data/.pgpass-local. They never use the
production database. If the isolated database is not running, they are
skipped rather than silently passing on fakes.
"""

from __future__ import annotations

import os
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.official_ref import (
    OfficialStoreNotMigrated,
    OfficialStoreUnavailable,
    official_object_key,
    sha256_bytes,
)
from agric_satellite_analysis_common.biweekly.official_store import UnavailableOfficialStore
from agric_satellite_analysis_common.biweekly.trigger import TriggerService
from agric_satellite_analysis_common.biweekly.catalog import LocalPackCatalog

P15 = "bj-ef25e6391ad7437b"
P12 = "bj-77f9bbfabcbbed32"
LAND = "32696"
SEASON = "2025/26"
MIN_PDF = b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n"
PASS_FILE = Path("/workspace/isolated/t9-oss-pg-data/.pgpass-local")


def _pg_url(dbname: str) -> str | None:
    password = os.environ.get("T9_OSS_PG_PASSWORD")
    if not password and PASS_FILE.is_file():
        password = PASS_FILE.read_text(encoding="utf-8").strip()
    if not password:
        return None
    host = os.environ.get("T9_OSS_PG_HOST", "127.0.0.1")
    port = os.environ.get("T9_OSS_PG_PORT", "55432")
    user = os.environ.get("T9_OSS_PG_USER", "t9oss")
    from urllib.parse import quote_plus

    return f"postgresql+psycopg2://{user}:{quote_plus(password)}@{host}:{port}/{dbname}"


def _connectable():
    url = _pg_url("t9oss_adapter_test")
    if not url:
        return None
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"options": "-csearch_path=agric_satellite,public"},
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM agric_satellite.biweekly_official_jobs LIMIT 0"))
        return engine
    except Exception:
        return None


ENGINE = _connectable()


def _session_factory(engine):
    from sqlalchemy.orm import Session, sessionmaker

    return sessionmaker(engine, class_=Session, expire_on_commit=False)


@unittest.skipUnless(ENGINE is not None, "isolated PostgreSQL adapter test database is not reachable")
class SqlAlchemyOfficialStorePgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.services.biweekly_official_store import SqlAlchemyOfficialStore

        cls.engine = ENGINE
        cls.Session = _session_factory(cls.engine)
        cls.Store = SqlAlchemyOfficialStore

    def setUp(self):
        self.store = self.Store(self.Session)
        self._ids: list[str] = []

    def tearDown(self):
        if not self._ids:
            return
        from sqlalchemy import text

        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM agric_satellite.biweekly_official_jobs WHERE job_id = ANY(:ids)"),
                {"ids": self._ids},
            )

    def _job(self, job_id: str, *, period: str, status: str = "dispatched", artifact=None, created=None):
        self._ids.append(job_id)
        created = created or "2026-11-23T15:21:02+08:00"
        return {
            "job_id": job_id,
            "kind": "generate",
            "status": status,
            "land_id": LAND,
            "season_id": SEASON,
            "period_id": str(period),
            "payload": {},
            "result": {},
            "history": [],
            "created_at": created,
            "updated_at": created,
            "artifact": artifact,
        }

    def test_persist_and_new_session_recovers_periods_job_and_artifact(self):
        artifact = {
            "storage": "oss",
            "bucket": "agric-test",
            "key": official_object_key(LAND, P15),
            "size_bytes": len(MIN_PDF),
            "sha256": sha256_bytes(MIN_PDF),
            "content_type": "application/pdf",
            "kind": "official_visual_artifact",
        }
        self.store.upsert(self._job(P15, period="15", status="ready", artifact=artifact))
        revived = self.Store(_session_factory(self.engine))
        job = revived.get(P15)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["period_id"], "15")
        self.assertEqual(job["artifact"]["key"], official_object_key(LAND, P15))
        self.assertEqual(job["artifact"]["sha256"], sha256_bytes(MIN_PDF))
        self.assertNotIn("signed_url", job["artifact"])
        # created_at must round-trip as a comparable timestamp (Asia/Shanghai written).
        created = job["created_at"]
        self.assertTrue(str(created).startswith("2026-11-23T15:21:02") or "15:21:02" in str(created))
        latest = revived.latest_for_period(LAND, SEASON, "15")
        self.assertEqual(latest["job_id"], P15)
        self.assertEqual(latest["status"], "ready")

    def test_ready_not_reverted_and_duplicate_complete_is_idempotent(self):
        artifact = {
            "storage": "oss",
            "bucket": "agric-test",
            "key": official_object_key(LAND, P15),
            "size_bytes": len(MIN_PDF),
            "sha256": sha256_bytes(MIN_PDF),
        }
        self.store.upsert(self._job(P15, period="15", status="dispatched"))
        job, outcome = self.store.apply_event(P15, {"status": "ready", "artifact": artifact})
        self.assertIn(outcome, {"applied", "created"})
        self.assertEqual(job["status"], "ready")
        again, outcome = self.store.apply_event(P15, {"status": "ready", "artifact": artifact})
        self.assertEqual(outcome, "idempotent")
        kept, outcome = self.store.apply_event(
            P15, {"status": "failed", "error_code": "GENERATE_FAILED"}
        )
        self.assertEqual(outcome, "ignored_stale_fail")
        self.assertEqual(kept["status"], "ready")

    def test_same_key_hash_change_updates_ready_artifact(self):
        first = {
            "storage": "oss",
            "bucket": "agric-test",
            "key": official_object_key(LAND, P15),
            "size_bytes": len(MIN_PDF),
            "sha256": sha256_bytes(MIN_PDF),
        }
        other = {**first, "sha256": sha256_bytes(MIN_PDF + b"x"), "size_bytes": len(MIN_PDF) + 1}
        self.store.upsert(self._job(P15, period="15"))
        self.store.apply_event(P15, {"status": "ready", "artifact": first})
        job, outcome = self.store.apply_event(P15, {"status": "ready", "artifact": other})
        self.assertEqual(outcome, "replaced")
        self.assertEqual(job["status"], "ready")
        stored = self.store.get(P15)
        self.assertEqual(stored["status"], "ready")
        self.assertEqual(stored["artifact"]["sha256"], other["sha256"])
        self.assertEqual(stored["artifact"]["size_bytes"], other["size_bytes"])
        self.assertEqual(stored["artifact"]["key"], first["key"])

    def test_conflicting_key_is_rejected(self):
        first = {
            "storage": "oss",
            "bucket": "agric-test",
            "key": official_object_key(LAND, P15),
            "size_bytes": len(MIN_PDF),
            "sha256": sha256_bytes(MIN_PDF),
        }
        other = {
            **first,
            "key": official_object_key(LAND, P12),
            "sha256": sha256_bytes(MIN_PDF + b"x"),
            "size_bytes": len(MIN_PDF) + 1,
        }
        self.store.upsert(self._job(P15, period="15"))
        self.store.apply_event(P15, {"status": "ready", "artifact": first})
        with self.assertRaises(ReportError) as exc:
            self.store.apply_event(P15, {"status": "ready", "artifact": other})
        self.assertEqual(exc.exception.code, "ARTIFACT_CONFLICT")
        self.assertEqual(self.store.get(P15)["artifact"]["sha256"], first["sha256"])
        self.assertEqual(self.store.get(P15)["artifact"]["key"], first["key"])

    def test_concurrent_complete_is_idempotent(self):
        artifact = {
            "storage": "oss",
            "bucket": "agric-test",
            "key": official_object_key(LAND, P15),
            "size_bytes": len(MIN_PDF),
            "sha256": sha256_bytes(MIN_PDF),
        }
        self.store.upsert(self._job(P15, period="15"))
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def _once():
            try:
                _, outcome = self.Store(_session_factory(self.engine)).apply_event(
                    P15, {"status": "ready", "artifact": artifact}
                )
                outcomes.append(outcome)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertTrue(set(outcomes) <= {"applied", "idempotent"})
        self.assertIn("applied", outcomes)
        self.assertEqual(self.store.get(P15)["status"], "ready")

    def test_failed_constraint_does_not_leave_a_row(self):
        bad_id = "not-a-bj-id"
        with self.assertRaises(Exception):
            self.store.upsert(self._job(bad_id, period="15", status="ready"))
        self._ids.remove(bad_id)
        self.assertIsNone(self.store.get(bad_id))

    def test_p12_recovery_from_failed_without_forging_migration(self):
        self.store.upsert(self._job(P12, period="12", status="failed"))
        artifact = {
            "storage": "oss",
            "bucket": "agric-test",
            "key": official_object_key(LAND, P12),
            "size_bytes": len(MIN_PDF),
            "sha256": sha256_bytes(MIN_PDF),
        }
        job, outcome = self.store.apply_event(P12, {"status": "ready", "artifact": artifact})
        self.assertEqual(outcome, "recovered")
        self.assertEqual(job["status"], "ready")

    def test_missing_table_is_explicit_not_empty(self):
        from app.services.biweekly_official_store import SqlAlchemyOfficialStore

        url = _pg_url("t9_neg_nojobs")  # schema may exist without the official table
        if not url:
            self.skipTest("no isolated pg url")
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session, sessionmaker

        engine = create_engine(url, connect_args={"options": "-csearch_path=agric_satellite,public"})
        store = SqlAlchemyOfficialStore(sessionmaker(engine, class_=Session, expire_on_commit=False))
        with self.assertRaises((OfficialStoreNotMigrated, OfficialStoreUnavailable)):
            store.latest_for_period(LAND, SEASON, "15")

    def test_unavailable_store_does_not_look_like_missing_jobs(self):
        store = UnavailableOfficialStore("injected")
        service = TriggerService(Path("/tmp/t9-official-store-none"), LocalPackCatalog(), official_store=store)
        with self.assertRaises(ReportError) as exc:
            service._latest_for_period(LAND, SEASON, "15")
        self.assertEqual(exc.exception.code, "OFFICIAL_STORE_UNAVAILABLE")
        with self.assertRaises(ReportError) as exc:
            service.get_job(P15)
        self.assertEqual(exc.exception.code, "OFFICIAL_STORE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
