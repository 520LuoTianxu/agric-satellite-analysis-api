"""After an OSS overwrite, registration must use the live size/hash, not a stale one."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from app.tasks import biweekly_report as br


MIN_PDF = b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n"
NEW_PDF = b"%PDF-1.4\n" + b"overwritten-official-pdf\n" * 80 + b"%%EOF\n"
JOB = "bj-d6124fd5ec80e670"
LAND = "23859"
KEY = f"reports/default/{LAND}/biweekly-{JOB}.pdf"
STALE_SHA = "a" * 64
LIVE_SHA = hashlib.sha256(NEW_PDF).hexdigest()


class Retry(Exception):
    def __init__(self, countdown=None):
        self.countdown = countdown


class DummyTask:
    max_retries = 40
    default_retry_delay = 60

    def __init__(self):
        self.request = type("R", (), {"retries": 0})()
        self.retry_countdowns = []

    def retry(self, countdown=None, kwargs=None):
        self.retry_countdowns.append(countdown)
        self.retry_kwargs = kwargs
        raise Retry(countdown)


def _run(task, **kwargs):
    return br.generate_biweekly_report.run.__func__(task, **kwargs)


class MemoryOSS:
    def __init__(self, key: str, data: bytes):
        self.key = key
        self.data = data
        self.heads = 0

    def exists(self, key: str) -> bool:
        return key == self.key

    def stat_object(self, key: str) -> dict:
        self.heads += 1
        return {"size_bytes": len(self.data), "content_type": "application/pdf"}

    def iter_bytes(self, key: str, *, chunk_size: int = 64 * 1024):
        yield self.data


class SizeMismatch(Exception):
    def __init__(self, message="biweekly/complete failed status=409 detail={'error': 'ARTIFACT_SIZE_MISMATCH'}"):
        super().__init__(message)
        self.status_code = 409


class OssOverwriteRegisterTests(unittest.TestCase):
    def test_stale_progress_registers_live_object_without_rerender(self) -> None:
        renders: list[int] = []
        uploads: list[str] = []
        completes: list[dict] = []
        oss_store = MemoryOSS(KEY, NEW_PDF)

        def fake_render(facts, dest, figures_dir=None):
            renders.append(1)
            Path(dest).write_bytes(MIN_PDF)

        def fake_upload(path, **kwargs):
            uploads.append(str(path))
            raise AssertionError("must not re-upload when only registering")

        def fake_complete(*args, **kwargs):
            completes.append({"oss": dict(kwargs.get("oss") or {})})
            oss = kwargs.get("oss") or {}
            if int(oss.get("size_bytes") or 0) != len(NEW_PDF) or oss.get("sha256") != LIVE_SHA:
                raise SizeMismatch()
            return {"job": {"status": "ready"}}

        progress = {
            "stage": "registration_pending",
            "official_status": "ready",
            "filename": "种植双周报_23859_2026-11-12_2026-11-25.pdf",
            "mime": "application/pdf",
            "kind": "official_visual_artifact",
            "facts": {"land_id": LAND},
            "note": "ok",
            "oss_artifact": {
                "bucket": "agric-test",
                "key": KEY,
                "size_bytes": len(MIN_PDF),
                "sha256": STALE_SHA,
                "content_type": "application/pdf",
            },
            "registration_attempts": 2,
        }
        import sys

        gen_mod = ModuleType("app.reports.biweekly.generate")
        gen_mod.generate_official_biweekly_pdf = fake_render
        previous = sys.modules.get("app.reports.biweekly.generate")
        sys.modules["app.reports.biweekly.generate"] = gen_mod
        task = DummyTask()
        try:
            with (
                patch(
                    "agric_satellite_analysis_common.storage.get_storage",
                    return_value=oss_store,
                ),
                patch(
                    "agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs",
                    return_value={"status": "ready", "facts": {"land_id": LAND}},
                ),
                patch(
                    "agric_satellite_analysis_common.internal_api.get_job",
                    return_value={"status": "running", "progress_json": {"stage": "running"}},
                ),
                patch.object(br, "_upload_official_object", side_effect=fake_upload),
                patch.object(br, "_complete", side_effect=fake_complete),
                patch("app.core.job_http.update_job_record"),
                patch("app.core.job_http.complete_work_item_http", return_value=True),
                patch("app.core.job_http.progress_work_item_http"),
                patch("app.core.job_http.fail_work_item_http") as fail_work,
            ):
                out = _run(
                    task,
                    report_job_id="report-3c3516fa",
                    biweekly_job_id=JOB,
                    land_id=LAND,
                    work_item_id="w-overwrite",
                    registration_progress=progress,
                )
        finally:
            if previous is None:
                sys.modules.pop("app.reports.biweekly.generate", None)
            else:
                sys.modules["app.reports.biweekly.generate"] = previous

        self.assertEqual(out["status"], "ready")
        self.assertEqual(renders, [])
        self.assertEqual(uploads, [])
        fail_work.assert_not_called()
        self.assertGreaterEqual(len(completes), 1)
        last = completes[-1]["oss"]
        self.assertEqual(last["size_bytes"], len(NEW_PDF))
        self.assertEqual(last["sha256"], LIVE_SHA)
        self.assertNotEqual(last["size_bytes"], len(MIN_PDF))
        self.assertGreater(oss_store.heads, 0)

    def test_refresh_reads_live_size_and_hash(self) -> None:
        oss = {
            "bucket": "agric-test",
            "key": KEY,
            "size_bytes": 9147811,
            "sha256": STALE_SHA,
            "content_type": "application/pdf",
        }
        with patch(
            "agric_satellite_analysis_common.storage.get_storage",
            return_value=MemoryOSS(KEY, NEW_PDF),
        ):
            updated = br._refresh_oss_artifact(oss)
        self.assertEqual(updated["size_bytes"], len(NEW_PDF))
        self.assertEqual(updated["sha256"], LIVE_SHA)
        self.assertEqual(updated["key"], KEY)


if __name__ == "__main__":
    unittest.main()
