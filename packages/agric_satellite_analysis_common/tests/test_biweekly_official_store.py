"""Official OSS artifact refs and job-state machine, including restart recovery."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.official_ref import (
    inspect_official_object,
    official_object_key,
    parse_oss_ref,
    sha256_bytes,
)
from agric_satellite_analysis_common.biweekly.official_store import (
    DurableJsonOfficialStore,
    MemoryOfficialStore,
    apply_status_event,
)


MIN_PDF = b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n"
P15_JOB = "bj-ef25e6391ad7437b"
P12_JOB = "bj-77f9bbfabcbbed32"
LAND = "32696"


class FakeObjectStorage:
    def __init__(self, bucket: str = "agric-test"):
        self._bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.full_reads = 0
        self.prefix_reads = 0

    @property
    def bucket(self) -> str:
        return self._bucket

    def exists(self, key: str) -> bool:
        return key in self.objects

    def stat_object(self, key: str) -> dict:
        data = self.objects[key]
        return {
            "size_bytes": len(data),
            "content_type": "application/pdf",
            "etag": None,
        }

    def read_prefix(self, key: str, n: int) -> bytes:
        self.prefix_reads += 1
        return self.objects[key][:n]

    def iter_bytes(self, key: str, chunk_size: int = 64 * 1024):
        self.full_reads += 1
        data = self.objects[key]
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def put_bytes(self, key: str, data: bytes, content_type=None) -> str:
        self.objects[key] = data
        return key

    def upload_file(self, key: str, file_path: str, content_type=None) -> str:
        self.objects[key] = Path(file_path).read_bytes()
        return key


def _ref(
    job_id: str,
    data: bytes,
    bucket: str = "agric-test",
    land: str = LAND,
    kind="official_visual_artifact",
):
    return {
        "bucket": bucket,
        "key": official_object_key(land, job_id, kind=kind),
        "size_bytes": len(data),
        "sha256": sha256_bytes(data),
        "content_type": "application/pdf",
    }


class OfficialRefTests(unittest.TestCase):
    def test_parse_rejects_signed_url_and_cross_tenant_key(self):
        with self.assertRaises(ReportError) as exc:
            parse_oss_ref(
                {
                    "oss": {
                        "bucket": "agric-test",
                        "key": "https://evil.example/a.pdf",
                        "size_bytes": 10,
                        "sha256": "a" * 64,
                    }
                },
                land_id=LAND,
                job_id=P15_JOB,
                kind="official_visual_artifact",
                expected_bucket="agric-test",
            )
        self.assertEqual(exc.exception.code, "ARTIFACT_URL_FORBIDDEN")

        with self.assertRaises(ReportError) as exc:
            parse_oss_ref(
                {
                    "oss_bucket": "attacker-bucket",
                    "oss_key": official_object_key(LAND, P15_JOB),
                    "size_bytes": 10,
                    "sha256": "a" * 64,
                },
                land_id=LAND,
                job_id=P15_JOB,
                kind="official_visual_artifact",
                expected_bucket="agric-test",
            )
        self.assertEqual(exc.exception.code, "ARTIFACT_BUCKET_MISMATCH")

        foreign = official_object_key("19994", P15_JOB)
        with self.assertRaises(ReportError) as exc:
            parse_oss_ref(
                {
                    "oss": {
                        "bucket": "agric-test",
                        "key": foreign,
                        "size_bytes": 10,
                        "sha256": "a" * 64,
                    }
                },
                land_id=LAND,
                job_id=P15_JOB,
                kind="official_visual_artifact",
                expected_bucket="agric-test",
            )
        self.assertEqual(exc.exception.code, "ARTIFACT_KEY_MISMATCH")

    def test_inspect_uses_prefix_and_stream_not_callback_body(self):
        storage = FakeObjectStorage()
        key = official_object_key(LAND, P15_JOB)
        storage.put_bytes(key, MIN_PDF)
        ref = _ref(P15_JOB, MIN_PDF)
        out = inspect_official_object(storage, ref, expect_pdf=True)
        self.assertEqual(out["sha256"], sha256_bytes(MIN_PDF))
        self.assertGreaterEqual(storage.prefix_reads, 1)
        self.assertEqual(storage.full_reads, 1)


class OfficialStateTests(unittest.TestCase):
    def _job(self, status, artifact=None, error=None):
        return {
            "job_id": P15_JOB,
            "kind": "generate",
            "status": status,
            "land_id": LAND,
            "season_id": "2025/26",
            "period_id": "15",
            "artifact": artifact,
            "history": [],
            "created_at": "2026-11-23T15:21:02+08:00",
        }

    def test_ready_not_overwritten_by_stale_fail(self):
        artifact = {**_ref(P15_JOB, MIN_PDF), "kind": "official_visual_artifact"}
        current, _ = apply_status_event(
            self._job("dispatched"), {"status": "ready", "artifact": artifact}
        )
        kept, outcome = apply_status_event(
            current, {"status": "failed", "error_code": "GENERATE_FAILED"}
        )
        self.assertEqual(outcome, "ignored_stale_fail")
        self.assertEqual(kept["status"], "ready")
        self.assertEqual(kept["artifact"]["sha256"], artifact["sha256"])

    def test_failed_job_recovers_when_oss_object_is_registered(self):
        artifact = {**_ref(P12_JOB, MIN_PDF), "kind": "official_visual_artifact"}
        failed = self._job("failed")
        failed["job_id"] = P12_JOB
        failed["period_id"] = "12"
        failed["error_code"] = "GENERATE_FAILED"
        next_job, outcome = apply_status_event(
            failed, {"status": "ready", "artifact": artifact}
        )
        self.assertEqual(outcome, "recovered")
        self.assertEqual(next_job["status"], "ready")

    def test_duplicate_complete_is_idempotent(self):
        artifact = {**_ref(P15_JOB, MIN_PDF), "kind": "official_visual_artifact"}
        current, _ = apply_status_event(
            self._job("dispatched"), {"status": "ready", "artifact": artifact}
        )
        again, outcome = apply_status_event(
            current, {"status": "ready", "artifact": artifact}
        )
        self.assertEqual(outcome, "idempotent")
        self.assertEqual(again["status"], "ready")

    def test_conflict_on_different_artifact(self):
        first = {**_ref(P15_JOB, MIN_PDF), "kind": "official_visual_artifact"}
        other_pdf = MIN_PDF + b"x"
        second = {
            **_ref(P15_JOB, other_pdf),
            "kind": "official_visual_artifact",
            "sha256": sha256_bytes(other_pdf),
            "size_bytes": len(other_pdf),
        }
        current, _ = apply_status_event(
            self._job("dispatched"), {"status": "ready", "artifact": first}
        )
        with self.assertRaises(ReportError) as exc:
            apply_status_event(current, {"status": "ready", "artifact": second})
        self.assertEqual(exc.exception.code, "ARTIFACT_CONFLICT")


class DurableStoreRestartTests(unittest.TestCase):
    def test_restart_reloads_periods_job_and_artifact_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "official-jobs.json"
            store = DurableJsonOfficialStore(path)
            artifact = {**_ref(P15_JOB, MIN_PDF), "kind": "official_visual_artifact"}
            store.upsert(
                {
                    "job_id": P15_JOB,
                    "kind": "generate",
                    "status": "ready",
                    "land_id": LAND,
                    "season_id": "2025/26",
                    "period_id": "15",
                    "artifact": artifact,
                    "created_at": "2026-11-23T15:21:02+08:00",
                    "payload": {},
                }
            )
            revived = DurableJsonOfficialStore(path)
            job = revived.get(P15_JOB)
            self.assertEqual(job["status"], "ready")
            self.assertEqual(job["artifact"]["key"], official_object_key(LAND, P15_JOB))
            latest = revived.latest_for_period(LAND, "2025/26", "15")
            self.assertEqual(latest["job_id"], P15_JOB)
            self.assertIsNone(revived.latest_for_period(LAND, "2025/26", "14"))

    def test_store_errors_are_not_empty_results(self):
        store = MemoryOfficialStore()
        store.broken = True
        with self.assertRaises(Exception):
            store.get(P15_JOB)
        with self.assertRaises(Exception):
            store.latest_for_period(LAND, "2025/26", "15")

    def test_concurrent_complete_stays_idempotent(self):
        store = MemoryOfficialStore()
        artifact = {**_ref(P15_JOB, MIN_PDF), "kind": "official_visual_artifact"}
        store.upsert(
            {
                "job_id": P15_JOB,
                "kind": "generate",
                "status": "dispatched",
                "land_id": LAND,
                "season_id": "2025/26",
                "period_id": "15",
                "created_at": "2026-11-23T15:21:02+08:00",
                "payload": {},
            }
        )
        outcomes = []

        def _once():
            _, outcome = store.apply_event(
                P15_JOB, {"status": "ready", "artifact": artifact}
            )
            outcomes.append(outcome)

        threads = [threading.Thread(target=_once) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        job = store.get(P15_JOB)
        self.assertEqual(job["status"], "ready")
        self.assertTrue(set(outcomes) <= {"applied", "idempotent"})
        self.assertIn("applied", outcomes)


if __name__ == "__main__":
    unittest.main()
