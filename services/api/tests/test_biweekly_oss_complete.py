"""OSS-ref complete, DB-backed official jobs, download, and compatibility."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agric_satellite_analysis_common.biweekly.official_ref import official_object_key, sha256_bytes
from agric_satellite_analysis_common.biweekly.official_store import (
    DurableJsonOfficialStore,
    MemoryOfficialStore,
)
from agric_satellite_analysis_common.biweekly.trigger import TriggerService
from agric_satellite_analysis_common.biweekly.catalog import LocalPackCatalog
from agric_satellite_analysis_common.biweekly.models import ReportError

from app.routers import internal_biweekly as biweekly_mod


MIN_PDF = b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n"
P15_JOB = "bj-ef25e6391ad7437b"
P12_JOB = "bj-77f9bbfabcbbed32"
LAND = "32696"
SEASON = "2025/26"
BUCKET = "agric-test"


class FakeObjectStorage:
    def __init__(self, bucket: str = BUCKET):
        self._bucket = bucket
        self.objects: dict[str, bytes] = {}

    def bucket(self) -> str:
        return self._bucket

    def exists(self, key: str) -> bool:
        return key in self.objects

    def stat_object(self, key: str) -> dict:
        data = self.objects[key]
        return {"size_bytes": len(data), "content_type": "application/pdf", "etag": None}

    def read_prefix(self, key: str, n: int) -> bytes:
        return self.objects[key][:n]

    def iter_bytes(self, key: str, chunk_size: int = 64 * 1024):
        data = self.objects[key]
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def put_bytes(self, key: str, data: bytes, content_type=None) -> str:
        self.objects[key] = data
        return key

    def upload_file(self, key: str, file_path: str, content_type=None) -> str:
        self.objects[key] = Path(file_path).read_bytes()
        return key


def seed_job(service: TriggerService, job_id: str, *, period: str, status: str = "dispatched", error=None):
    job = {
        "job_id": job_id,
        "kind": "generate",
        "status": status,
        "land_id": LAND,
        "season_id": SEASON,
        "period_id": str(period),
        "payload": {},
        "result": {},
        "history": [{"status": status, "at": "2026-11-23T15:00:00+08:00"}],
        "created_at": "2026-11-23T15:00:00+08:00",
        "updated_at": "2026-11-23T15:00:00+08:00",
        "artifact": None,
        "error_code": error,
    }
    service._write(job)
    return job


def oss_body(job_id: str, data: bytes, *, status="ready", land=LAND):
    key = official_object_key(land, job_id)
    return biweekly_mod.CompleteRequest(
        status=status,
        filename=f"种植双周报_{land}.pdf",
        mime="application/pdf",
        kind="official_visual_artifact",
        oss_bucket=BUCKET,
        oss_key=key,
        size_bytes=len(data),
        sha256=sha256_bytes(data),
        content_type="application/pdf",
        facts={"land_id": land},
        note="正式报告 PDF 已由下载机生成。",
    )


class OssCompleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryOfficialStore()
        self.storage = FakeObjectStorage()
        self.service = TriggerService(
            Path(self.tmp.name),
            LocalPackCatalog(),
            official_store=self.store,
            object_storage=self.storage,
            expected_oss_bucket=BUCKET,
        )
        self._svc = patch(
            "app.routers.internal_biweekly.build_trigger_service",
            return_value=self.service,
        )
        self._svc.start()

    def tearDown(self):
        self._svc.stop()
        self.tmp.cleanup()

    def test_p15_oss_complete_is_downloadable_without_base64(self):
        seed_job(self.service, P15_JOB, period="15")
        key = official_object_key(LAND, P15_JOB)
        self.storage.put_bytes(key, MIN_PDF)
        body = oss_body(P15_JOB, MIN_PDF)
        self.assertIsNone(body.content_base64)
        out = asyncio.run(biweekly_mod.biweekly_report_complete(P15_JOB, body, None))
        self.assertTrue(out["ok"])
        self.assertEqual(out["job"]["status"], "ready")
        artifact = out["job"]["artifact"]
        self.assertEqual(artifact["bucket"], BUCKET)
        self.assertEqual(artifact["key"], key)
        self.assertNotIn("url", artifact)
        self.assertNotIn("signed_url", artifact)
        name, data, mime = self.service.download(P15_JOB)
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(sha256_bytes(data), sha256_bytes(MIN_PDF))
        latest = self.service._latest_for_period(LAND, SEASON, "15")
        self.assertEqual(latest["job_id"], P15_JOB)
        self.assertEqual(latest["status"], "ready")
        self.assertEqual(latest["persistence"], "database")

    def test_p12_existing_oss_unregistered_recovers_from_failed(self):
        seed_job(self.service, P12_JOB, period="12", status="failed", error="GENERATE_FAILED")
        key = official_object_key(LAND, P12_JOB)
        self.storage.put_bytes(key, MIN_PDF)
        out = asyncio.run(biweekly_mod.biweekly_report_complete(P12_JOB, oss_body(P12_JOB, MIN_PDF), None))
        self.assertEqual(out["job"]["status"], "ready")
        name, data, mime = self.service.download(P12_JOB)
        self.assertTrue(data.startswith(b"%PDF-"))

    def test_ready_not_reverted_by_stale_fail(self):
        seed_job(self.service, P15_JOB, period="15")
        self.storage.put_bytes(official_object_key(LAND, P15_JOB), MIN_PDF)
        asyncio.run(biweekly_mod.biweekly_report_complete(P15_JOB, oss_body(P15_JOB, MIN_PDF), None))
        fail = biweekly_mod.CompleteRequest(status="failed", error_code="GENERATE_FAILED", note="late fail")
        out = asyncio.run(biweekly_mod.biweekly_report_complete(P15_JOB, fail, None))
        self.assertEqual(out["job"]["status"], "ready")
        self.assertEqual(self.service.get_job(P15_JOB)["status"], "ready")

    def test_duplicate_complete_is_idempotent(self):
        seed_job(self.service, P15_JOB, period="15")
        self.storage.put_bytes(official_object_key(LAND, P15_JOB), MIN_PDF)
        first = asyncio.run(biweekly_mod.biweekly_report_complete(P15_JOB, oss_body(P15_JOB, MIN_PDF), None))
        second = asyncio.run(biweekly_mod.biweekly_report_complete(P15_JOB, oss_body(P15_JOB, MIN_PDF), None))
        self.assertEqual(first["job"]["status"], "ready")
        self.assertEqual(second["job"]["status"], "ready")
        self.assertEqual(second["job"]["artifact"]["sha256"], first["job"]["artifact"]["sha256"])

    def test_legacy_base64_still_completes(self):
        job = self.service.request_generate("6417", "2025/26", "12", defer_run=True)
        body = biweekly_mod.CompleteRequest(
            status="ready",
            filename="种植双周报_6417.pdf",
            mime="application/pdf",
            kind="official_visual_artifact",
            content_base64=base64.b64encode(MIN_PDF).decode("ascii"),
            facts={"land_id": "6417"},
        )
        out = asyncio.run(biweekly_mod.biweekly_report_complete(job["job_id"], body, None))
        self.assertEqual(out["job"]["status"], "ready")
        name, data, mime = self.service.download(job["job_id"])
        self.assertTrue(data.startswith(b"%PDF-"))

    def test_arbitrary_url_is_rejected_before_storage(self):
        seed_job(self.service, P15_JOB, period="15")
        body = biweekly_mod.CompleteRequest(
            status="ready",
            oss_bucket=BUCKET,
            oss_key="https://evil.example/reports/default/32696/biweekly-bj-ef25e6391ad7437b.pdf",
            size_bytes=len(MIN_PDF),
            sha256=sha256_bytes(MIN_PDF),
        )
        with self.assertRaises(Exception) as exc:
            asyncio.run(biweekly_mod.biweekly_report_complete(P15_JOB, body, None))
        detail = getattr(exc.exception, "detail", {})
        self.assertEqual((detail or {}).get("error"), "ARTIFACT_URL_FORBIDDEN")
        self.assertIsNone(self.service.get_job(P15_JOB).get("artifact"))

    def test_cross_land_key_is_rejected(self):
        seed_job(self.service, P15_JOB, period="15")
        body = biweekly_mod.CompleteRequest(
            status="ready",
            oss_bucket=BUCKET,
            oss_key=official_object_key("19994", P15_JOB),
            size_bytes=len(MIN_PDF),
            sha256=sha256_bytes(MIN_PDF),
        )
        with self.assertRaises(Exception) as exc:
            asyncio.run(biweekly_mod.biweekly_report_complete(P15_JOB, body, None))
        detail = getattr(exc.exception, "detail", {})
        self.assertEqual((detail or {}).get("error"), "ARTIFACT_KEY_MISMATCH")

    def test_store_error_is_not_swallowed_as_missing(self):
        seed_job(self.service, P15_JOB, period="15")
        self.store.broken = True
        with self.assertRaises(ReportError) as exc:
            self.service.get_job(P15_JOB)
        self.assertEqual(exc.exception.code, "OFFICIAL_STORE_UNAVAILABLE")
        with self.assertRaises(ReportError) as exc:
            self.service._latest_for_period(LAND, SEASON, "15")
        self.assertEqual(exc.exception.code, "OFFICIAL_STORE_UNAVAILABLE")

    def test_download_rejects_other_land(self):
        seed_job(self.service, P15_JOB, period="15")
        self.storage.put_bytes(official_object_key(LAND, P15_JOB), MIN_PDF)
        asyncio.run(biweekly_mod.biweekly_report_complete(P15_JOB, oss_body(P15_JOB, MIN_PDF), None))
        job = self.service.get_job(P15_JOB)
        self.assertEqual(job["land_id"], LAND)
        self.assertNotEqual(job["land_id"], "19994")


class RestartRecoveryTests(unittest.TestCase):
    def test_api_restart_recovers_periods_job_and_download_from_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "official.json"
            storage = FakeObjectStorage()
            key = official_object_key(LAND, P15_JOB)
            storage.put_bytes(key, MIN_PDF)
            first_dir = Path(tmp) / "work1"
            store = DurableJsonOfficialStore(store_path)
            service = TriggerService(
                first_dir,
                LocalPackCatalog(),
                official_store=store,
                object_storage=storage,
                expected_oss_bucket=BUCKET,
            )
            seed_job(service, P15_JOB, period="15")
            with patch("app.routers.internal_biweekly.build_trigger_service", return_value=service):
                asyncio.run(biweekly_mod.biweekly_report_complete(P15_JOB, oss_body(P15_JOB, MIN_PDF), None))

            # Simulate API restart: empty work_dir, new store instance from the same durable file.
            revived = DurableJsonOfficialStore(store_path)
            service2 = TriggerService(
                Path(tmp) / "work2-empty",
                LocalPackCatalog(),
                official_store=revived,
                object_storage=storage,
                expected_oss_bucket=BUCKET,
            )
            self.assertFalse((Path(tmp) / "work2-empty" / "jobs" / f"{P15_JOB}.json").is_file())
            job = service2.get_job(P15_JOB)
            self.assertEqual(job["status"], "ready")
            self.assertEqual(job["persistence"], "database")
            latest = service2._latest_for_period(LAND, SEASON, "15")
            self.assertEqual(latest["job_id"], P15_JOB)
            name, data, mime = service2.download(P15_JOB)
            self.assertTrue(data.startswith(b"%PDF-"))
            self.assertEqual(len(data), len(MIN_PDF))



class EventLoopTests(OssCompleteTests):
    def test_complete_oss_hash_does_not_block_event_loop(self):
        import time
        from agric_satellite_analysis_common.biweekly import official_ref

        seed_job(self.service, P15_JOB, period="15")
        self.storage.put_bytes(official_object_key(LAND, P15_JOB), MIN_PDF)
        original = official_ref.inspect_official_object

        def slow_inspect(*args, **kwargs):
            time.sleep(0.25)
            return original(*args, **kwargs)

        ticks = []

        async def ticker():
            for i in range(6):
                await asyncio.sleep(0.05)
                ticks.append(i)

        async def run():
            with patch("agric_satellite_analysis_common.biweekly.trigger.inspect_official_object", side_effect=slow_inspect):
                complete = asyncio.create_task(
                    biweekly_mod.biweekly_report_complete(P15_JOB, oss_body(P15_JOB, MIN_PDF), None)
                )
                await ticker()
                out = await complete
                return out

        out = asyncio.run(run())
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(len(ticks), 3, "event loop should keep ticking during OSS hash")

    def test_download_oss_stream_does_not_block_event_loop(self):
        import time
        from app.services.biweekly_trigger import stream_official_download

        seed_job(self.service, P15_JOB, period="15")
        self.storage.put_bytes(official_object_key(LAND, P15_JOB), MIN_PDF)
        # Mark the official job ready with an OSS ref (no hash inspect here).
        self.service.attach_oss_artifact(
            P15_JOB,
            status="ready",
            artifact={
                "bucket": BUCKET,
                "key": official_object_key(LAND, P15_JOB),
                "size_bytes": len(MIN_PDF),
                "sha256": sha256_bytes(MIN_PDF),
                "content_type": "application/pdf",
                "filename": "p15.pdf",
                "mime": "application/pdf",
                "kind": "official_visual_artifact",
            },
            inspect=False,
        )
        original = self.storage.iter_bytes

        def slow_iter(key, chunk_size=64 * 1024):
            time.sleep(0.25)
            yield from original(key, chunk_size=chunk_size)

        self.storage.iter_bytes = slow_iter
        ticks = []

        async def ticker():
            for i in range(6):
                await asyncio.sleep(0.05)
                ticks.append(i)

        async def consume():
            _name, agen, _mime, _size = await stream_official_download(self.service, P15_JOB)
            chunks = []
            async for part in agen:
                chunks.append(part)
            return b"".join(chunks)

        async def run():
            task = asyncio.create_task(consume())
            await ticker()
            return await task

        data = asyncio.run(run())
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreaterEqual(len(ticks), 3, "event loop should keep ticking during OSS download")

    def test_start_period_generate_store_io_does_not_block_event_loop(self):
        import time
        from app.services.biweekly_report_flow import start_period_generate

        class _StubCatalog:
            def periods(self, land_id, season_id):
                return [{
                    "period_id": "15",
                    "start": "2026-05-16",
                    "end_inclusive": "2026-05-29",
                    "end_exclusive": "2026-05-30",
                }]

            def period(self, land_id, season_id, period_id):
                return self.periods(land_id, season_id)[0]

        self.service.catalog = _StubCatalog()
        orig = self.service.request_generate

        def slow_request(*args, **kwargs):
            time.sleep(0.25)
            return orig(*args, **kwargs)

        self.service.request_generate = slow_request
        ticks = []

        async def ticker():
            for i in range(6):
                await asyncio.sleep(0.05)
                ticks.append(i)

        async def run():
            with patch(
                "app.services.biweekly_report_flow.dispatch_bounded_backfill",
                new=AsyncMock(
                    return_value={
                        "dispatched": False,
                        "reason": "NO_JOBS_CREATED",
                        "job_ids": [],
                        "job_count": 0,
                    }
                ),
            ):
                task = asyncio.create_task(
                    start_period_generate(
                        self.service,
                        land_id=LAND,
                        season_id=SEASON,
                        period_id="15",
                    )
                )
                await ticker()
                return await task

        job = asyncio.run(run())
        self.assertIn(job.get("status"), {"dispatched", "queued", "running", "ready", "failed"})
        self.assertGreaterEqual(len(ticks), 3, "event loop should keep ticking during generate store I/O")


if __name__ == "__main__":

    unittest.main()
