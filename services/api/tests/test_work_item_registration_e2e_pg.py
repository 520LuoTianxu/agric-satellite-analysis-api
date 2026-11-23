"""Real isolated-Postgres work_items recovery: claim → lease expiry → reaper → reclaim.

This is not a FakeItem helper test. It uses agric_satellite.work_items on the
isolated Postgres 16 instance. Official complete / render / OSS are mocked
because those systems are not in this database; the queue and claim path are
real. Delivered official-jobs SQL is not modified.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agric_satellite_analysis_common.biweekly.official_ref import registration_backoff_s

PASS_FILE = Path("/workspace/isolated/t9-oss-pg-data/.pgpass-local")
DBNAME = "t9oss_work_items"
CONTAINER = "t9-oss-official-jobs-pg"
P15 = "bj-ef25e6391ad7437b"
LAND = "32696"
MIN_PDF = b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n"
OSS = {
    "bucket": "agric-test",
    "key": f"reports/default/{LAND}/biweekly-{P15}.pdf",
    "size_bytes": len(MIN_PDF),
    "sha256": "a" * 64,
    "content_type": "application/pdf",
    "kind": "official_visual_artifact",
}

def _run_ingest_worker(**task_kwargs) -> dict:
    """Invoke generate_biweekly_report in a clean ingest-package interpreter."""
    import json
    import sys

    repo = Path("/workspace/isolated/agric-t9-oss-complete")
    common = repo / "packages" / "agric_satellite_analysis_common"
    ingest = repo / "services" / "ingest"
    script = r"""
import json, sys
from types import ModuleType
from unittest.mock import patch

payload = json.loads(sys.stdin.read())
renders = []
uploads = []
completes = []

def fake_render(*args, **kwargs):
    renders.append(1)
    raise AssertionError("must not re-render")

def fake_upload(*args, **kwargs):
    uploads.append("x")
    raise AssertionError("must not re-upload")

def fake_complete(*args, **kwargs):
    completes.append({"oss": kwargs.get("oss"), "content": kwargs.get("content")})
    return {"job": {"status": "ready"}}

def fake_get_job(job_id):
    return {"status": "running", "progress_json": {"stage": "running"}}

gen_mod = ModuleType("app.reports.biweekly.generate")
gen_mod.generate_official_biweekly_pdf = fake_render
sys.modules["app.reports.biweekly.generate"] = gen_mod
sys.modules.setdefault("app.reports", ModuleType("app.reports"))
sys.modules.setdefault("app.reports.biweekly", ModuleType("app.reports.biweekly"))

from app.tasks import biweekly_report as br

class DummyTask:
    max_retries = 40
    default_retry_delay = 60
    def __init__(self):
        self.request = type("R", (), {"retries": 0})()
    def retry(self, countdown=None):
        raise RuntimeError("retry")

out = {"error": None, "renders": 0, "uploads": 0, "completes": 0, "status": None}
try:
    with (
        patch("agric_satellite_analysis_common.internal_api.get_job", side_effect=fake_get_job),
        patch.object(br, "_upload_official_object", side_effect=fake_upload),
        patch.object(br, "_complete", side_effect=fake_complete),
        patch("app.core.job_http.update_job_record"),
        patch("app.core.job_http.complete_work_item_http") as complete_work,
        patch("app.core.job_http.progress_work_item_http"),
        patch("app.core.job_http.fail_work_item_http") as fail_work,
    ):
        result = br.generate_biweekly_report.run.__func__(DummyTask(), **payload)
    out.update({
        "status": result.get("status"),
        "renders": len(renders),
        "uploads": len(uploads),
        "completes": len(completes),
        "oss_key": (completes[0]["oss"] or {}).get("key") if completes else None,
        "content": completes[0]["content"] if completes else "missing",
        "complete_called": complete_work.called,
        "fail_called": fail_work.called,
    })
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{common}:{ingest}"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(task_kwargs),
        cwd=str(ingest),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    if proc.returncode != 0:
        return {"error": f"worker_exit={proc.returncode} stderr={proc.stderr[-2000:]}"}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        return {"error": f"bad_worker_output {exc}: {proc.stdout[-1000:]} {proc.stderr[-1000:]}"}


def _password() -> str | None:
    password = os.environ.get("T9_OSS_PG_PASSWORD")
    if not password and PASS_FILE.is_file():
        password = PASS_FILE.read_text(encoding="utf-8").strip()
    return password or None


def _sync_url(dbname: str) -> str | None:
    password = _password()
    if not password:
        return None
    host = os.environ.get("T9_OSS_PG_HOST", "127.0.0.1")
    port = os.environ.get("T9_OSS_PG_PORT", "55432")
    user = os.environ.get("T9_OSS_PG_USER", "t9oss")
    from urllib.parse import quote_plus

    return f"postgresql+psycopg2://{user}:{quote_plus(password)}@{host}:{port}/{dbname}"


def _ensure_database() -> bool:
    """Create the isolated work_items database if needed. Never touches production."""
    inspect = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Health.Status}}",
            CONTAINER,
        ],
        capture_output=True,
        text=True,
    )
    if inspect.returncode != 0 or inspect.stdout.strip() != "healthy":
        return False
    exists = subprocess.run(
        [
            "docker",
            "exec",
            CONTAINER,
            "psql",
            "-U",
            "t9oss",
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname='{DBNAME}'",
        ],
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        return False
    if exists.stdout.strip() != "1":
        created = subprocess.run(
            [
                "docker",
                "exec",
                CONTAINER,
                "psql",
                "-U",
                "t9oss",
                "-d",
                "postgres",
                "-c",
                f"CREATE DATABASE {DBNAME}",
            ],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            return False
    ext = subprocess.run(
        [
            "docker",
            "exec",
            CONTAINER,
            "psql",
            "-U",
            "t9oss",
            "-d",
            DBNAME,
            "-c",
            'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"; CREATE SCHEMA IF NOT EXISTS agric_satellite;',
        ],
        capture_output=True,
        text=True,
    )
    return ext.returncode == 0 and _sync_url(DBNAME) is not None


def _prepare_schema(engine) -> None:
    from sqlalchemy import text

    from app.models.tables import WorkItem

    with engine.begin() as conn:
        conn.execute(text("SET search_path TO agric_satellite, public"))
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS agric_satellite"))
        WorkItem.__table__.create(conn, checkfirst=True)
        conn.execute(text("TRUNCATE agric_satellite.work_items"))


AVAILABLE = _ensure_database()


@unittest.skipUnless(AVAILABLE, "isolated PostgreSQL work_items database is not reachable")
class WorkItemRegistrationE2EPgTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.orm import Session, sessionmaker

        sync_url = _sync_url(DBNAME)
        if not sync_url:
            raise unittest.SkipTest("isolated Postgres credentials are not available")
        cls.sync_engine = create_engine(
            sync_url,
            pool_pre_ping=True,
            connect_args={"options": "-csearch_path=agric_satellite,public"},
        )
        _prepare_schema(cls.sync_engine)
        async_url = sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        # One loop for the class: asyncpg pools cannot hop between asyncio.run() loops.
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)
        cls.async_engine = create_async_engine(async_url, pool_pre_ping=False)
        cls.async_sessions = async_sessionmaker(cls.async_engine, expire_on_commit=False)
        cls.sync_sessions = sessionmaker(cls.sync_engine, class_=Session, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.loop.run_until_complete(cls.async_engine.dispose())
        cls.loop.close()
        cls.sync_engine.dispose()

    def setUp(self):
        from sqlalchemy import text

        with self.sync_engine.begin() as conn:
            conn.execute(text("TRUNCATE agric_satellite.work_items"))

    def _run(self, coro):
        return type(self).loop.run_until_complete(coro)

    def _progress(self, *, attempts=8, retry_after=None):
        payload = {
            "stage": "registration_pending",
            "official_status": "ready",
            "filename": "种植双周报_32696.pdf",
            "mime": "application/pdf",
            "kind": "official_visual_artifact",
            "facts": {"land_id": LAND},
            "note": "uploaded",
            "oss_artifact": dict(OSS),
            "registration_attempts": attempts,
        }
        if retry_after is not None:
            payload["registration_retry_after"] = retry_after
        return payload

    def _insert_pending(self, *, attempts=0, progress=None):
        from app.models.tables import WorkItem

        item_id = uuid.uuid4()
        job_id = uuid.uuid4()
        with self.sync_sessions() as db:
            row = WorkItem(
                id=item_id,
                type="biweekly_report",
                parent_job_id=None,
                payload_json={
                    "land_id": LAND,
                    "extras": {
                        "job_id": str(job_id),
                        "biweekly_job_id": P15,
                        "date_from": "2026-05-16",
                        "date_to": "2026-05-29",
                    },
                },
                status="pending",
                priority=0,
                attempts=attempts,
                progress_json=progress or self._progress(),
            )
            db.add(row)
            db.commit()
        return item_id, job_id

    async def _session_call(self, fn, *args, **kwargs):
        async with self.async_sessions() as db:
            await db.execute(
                __import__("sqlalchemy", fromlist=["text"]).text(
                    "SET search_path TO agric_satellite, public"
                )
            )
            result = await fn(db, *args, **kwargs)
            await db.commit()
            return result

    def test_claim_lease_expiry_reaper_reclaim_registers_only_when_job_missing_artifact(self):
        from app.services import work_items as wi
        from app.models.tables import WorkItem

        item_id, report_job_id = self._insert_pending(progress=self._progress(attempts=8))

        async def first_claim():
            async with self.async_sessions() as db:
                await db.execute(
                    __import__("sqlalchemy", fromlist=["text"]).text(
                        "SET search_path TO agric_satellite, public"
                    )
                )
                claimed = await wi.claim_work_items(
                    db, worker_id="worker-a", types=["biweekly_report"], limit=1, lease_seconds=30
                )
                await db.commit()
                return claimed

        claimed = self._run(first_claim())
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].id, item_id)
        self.assertEqual(claimed[0].status, "leased")
        self.assertEqual(claimed[0].attempts, 1)
        self.assertEqual(claimed[0].lease_owner, "worker-a")

        async def expire_and_reap():
            async with self.async_sessions() as db:
                await db.execute(
                    __import__("sqlalchemy", fromlist=["text"]).text(
                        "SET search_path TO agric_satellite, public"
                    )
                )
                row = await db.get(WorkItem, item_id)
                row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=5)
                await db.flush()
                reaped = await wi.reaper_expired_leases(db)
                await db.commit()
                row = await db.get(WorkItem, item_id)
                return reaped, row.status, row.attempts, row.error, row.lease_owner

        reaped, status, attempts, error, owner = self._run(expire_and_reap())
        self.assertEqual(reaped, 1)
        self.assertEqual(status, "pending")
        self.assertEqual(attempts, 1)
        self.assertNotEqual(status, "failed")
        self.assertIsNone(owner)
        self.assertEqual(error, "REGISTRATION_PENDING")

        async def reclaim(worker):
            async with self.async_sessions() as db:
                await db.execute(
                    __import__("sqlalchemy", fromlist=["text"]).text(
                        "SET search_path TO agric_satellite, public"
                    )
                )
                claimed = await wi.claim_work_items(
                    db, worker_id=worker, types=["biweekly_report"], limit=1, lease_seconds=30
                )
                await db.commit()
                return [row.id for row in claimed], [row.attempts for row in claimed]

        # No retry_after yet: failover reclaim is immediate.
        ids, att = self._run(reclaim("worker-b"))
        self.assertEqual(ids, [item_id])
        self.assertEqual(att, [2])

        async def exhaust_and_requeue():
            async with self.async_sessions() as db:
                await db.execute(
                    __import__("sqlalchemy", fromlist=["text"]).text(
                        "SET search_path TO agric_satellite, public"
                    )
                )
                # attempts already 2; bump to MAX so we assert recoverable still requeues.
                row = await db.get(WorkItem, item_id)
                row.attempts = wi.MAX_WORK_ITEM_ATTEMPTS
                await db.flush()
                item = await wi.fail_work_item(
                    db, item_id, error="REGISTRATION_PENDING", worker_id="worker-b", retry=True
                )
                await db.commit()
                return item.status, item.attempts, dict(item.progress_json or {})

        status, attempts, progress = self._run(exhaust_and_requeue())
        self.assertEqual(status, "pending")
        self.assertGreaterEqual(attempts, wi.MAX_WORK_ITEM_ATTEMPTS)
        retry_after = progress.get("registration_retry_after")
        self.assertTrue(retry_after)
        due = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00"))
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        self.assertGreater(due, datetime.now(timezone.utc))
        expected_delay = registration_backoff_s(8)
        self.assertEqual(expected_delay, 300)
        self.assertGreaterEqual((due - datetime.now(timezone.utc)).total_seconds(), 200)

        # Tight loop must not reclaim while backoff is in the future.
        ids, _ = self._run(reclaim("worker-c"))
        self.assertEqual(ids, [])

        async def expire_backoff_and_reclaim():
            async with self.async_sessions() as db:
                await db.execute(
                    __import__("sqlalchemy", fromlist=["text"]).text(
                        "SET search_path TO agric_satellite, public"
                    )
                )
                row = await db.get(WorkItem, item_id)
                progress = dict(row.progress_json or {})
                progress["registration_retry_after"] = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                ).isoformat()
                row.progress_json = progress
                await db.flush()
                claimed = await wi.claim_work_items(
                    db, worker_id="worker-c", types=["biweekly_report"], limit=1, lease_seconds=30
                )
                await db.commit()
                return claimed

        claimed = self._run(expire_backoff_and_reclaim())
        self.assertEqual(len(claimed), 1)
        row = claimed[0]
        self.assertEqual(row.id, item_id)
        self.assertEqual(row.last_claimed_by, "worker-c")
        self.assertGreater(row.attempts, wi.MAX_WORK_ITEM_ATTEMPTS)
        self.assertEqual(row.status, "leased")


        # Same helper the work agent uses, then a fresh ingest-worker process
        # so this API test does not import a second `app` package.
        from agric_satellite_analysis_common.biweekly.official_ref import (
            registration_progress_from_work_item,
        )

        item_dict = {
            "id": str(row.id),
            "type": row.type,
            "payload_json": row.payload_json,
            "progress_json": row.progress_json,
            "attempts": row.attempts,
        }
        progress = registration_progress_from_work_item(item_dict)
        self.assertIsNotNone(progress)
        self.assertEqual(progress["oss_artifact"]["sha256"], "a" * 64)
        worker_out = _run_ingest_worker(
            report_job_id=str(report_job_id),
            biweekly_job_id=P15,
            land_id=LAND,
            work_item_id=str(row.id),
            registration_progress=progress,
        )
        self.assertIsNone(worker_out.get("error"), worker_out)
        self.assertEqual(worker_out["status"], "ready")
        self.assertEqual(worker_out["renders"], 0)
        self.assertEqual(worker_out["uploads"], 0)
        self.assertEqual(worker_out["completes"], 1)
        self.assertEqual(worker_out["oss_key"], OSS["key"])
        self.assertIsNone(worker_out["content"])
        self.assertTrue(worker_out["complete_called"])
        self.assertFalse(worker_out["fail_called"])

    def test_reaper_does_not_fail_parent_when_attempts_full_but_oss_present(self):
        from app.services import work_items as wi
        from app.models.tables import WorkItem

        item_id, _ = self._insert_pending(
            attempts=wi.MAX_WORK_ITEM_ATTEMPTS,
            progress=self._progress(attempts=8),
        )

        async def lease_expire_and_reap():
            async with self.async_sessions() as db:
                await db.execute(
                    __import__("sqlalchemy", fromlist=["text"]).text(
                        "SET search_path TO agric_satellite, public"
                    )
                )
                now = datetime.now(timezone.utc)
                row = await db.get(WorkItem, item_id)
                row.status = "leased"
                row.lease_owner = "worker-a"
                row.lease_until = now - timedelta(seconds=5)
                await db.flush()
                reaped = await wi.reaper_expired_leases(db)
                await db.commit()
                row = await db.get(WorkItem, item_id)
                return reaped, row.status, row.error

        reaped, status, error = self._run(lease_expire_and_reap())
        self.assertEqual(reaped, 1)
        self.assertEqual(status, "pending")
        self.assertEqual(error, "REGISTRATION_PENDING")


if __name__ == "__main__":
    unittest.main()
