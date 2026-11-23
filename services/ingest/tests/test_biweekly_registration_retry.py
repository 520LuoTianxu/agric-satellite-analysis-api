"""After OSS upload, complete failures retry registration only — never GENERATE_FAILED."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.tasks import biweekly_report as br


MIN_PDF = b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n"
P15_JOB = "bj-ef25e6391ad7437b"
LAND = "32696"


class Retry(Exception):
    def __init__(self, countdown=None):
        self.countdown = countdown


class DummyTask:
    max_retries = 40
    default_retry_delay = 60

    def __init__(self):
        self.request = type("R", (), {"retries": 0})()
        self.retry_countdowns = []

    def retry(self, countdown=None):
        self.retry_countdowns.append(countdown)
        raise Retry(countdown)


def _run(task, **kwargs):
    # Invoke the raw task function so DummyTask.retry is used, not Celery's.
    return br.generate_biweekly_report.run.__func__(task, **kwargs)


class RegistrationRetryTests(unittest.TestCase):
    def test_timeout_after_upload_retries_without_rerender_or_reupload(self):
        renders = []
        uploads = []
        completes = []
        job_updates = []

        def fake_render(facts, dest, figures_dir=None):
            renders.append(1)
            Path(dest).write_bytes(MIN_PDF)

        def fake_upload(path, **kwargs):
            uploads.append(path)
            return {
                "bucket": "agric-test",
                "key": f"reports/default/{LAND}/biweekly-{P15_JOB}.pdf",
                "size_bytes": len(MIN_PDF),
                "sha256": "a" * 64,
                "content_type": "application/pdf",
                "kind": "official_visual_artifact",
            }

        def fake_complete(*args, **kwargs):
            completes.append({"oss": kwargs.get("oss"), "content": kwargs.get("content")})
            raise TimeoutError("read timeout")

        task = DummyTask()
        progress_holder = {}

        def fake_get_job(job_id):
            if job_id == "report-1":
                return {"status": "running", "progress_json": progress_holder.get("p")}
            raise AssertionError(job_id)

        def fake_update(a, b, status, progress=None, error=None, job_id=None):
            if progress:
                progress_holder["p"] = dict(progress)
            job_updates.append({"status": status, "progress": progress, "error": error})

        import sys
        from types import ModuleType

        gen_mod = ModuleType("app.reports.biweekly.generate")
        gen_mod.generate_official_biweekly_pdf = fake_render
        sys.modules["app.reports.biweekly.generate"] = gen_mod
        sys.modules.setdefault("app.reports", ModuleType("app.reports"))
        sys.modules.setdefault("app.reports.biweekly", ModuleType("app.reports.biweekly"))

        with (
            patch("agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs", return_value={"status": "ready", "facts": {"land_id": LAND, "period": {"start": "2026-05-16", "end_inclusive": "2026-05-29"}}}),
            patch("agric_satellite_analysis_common.internal_api.get_job", side_effect=fake_get_job),
            patch.object(br, "_upload_official_object", side_effect=fake_upload),
            patch.object(br, "_complete", side_effect=fake_complete),
            patch("app.core.job_http.update_job_record", side_effect=fake_update),
            patch("app.core.job_http.complete_work_item_http"),
            patch("app.core.job_http.progress_work_item_http"),
            patch("app.core.job_http.fail_work_item_http") as fail_work,
        ):
            with self.assertRaises(Retry):
                _run(
                    task,
                    report_job_id="report-1",
                    biweekly_job_id=P15_JOB,
                    land_id=LAND,
                    work_item_id="w1",
                )
            self.assertEqual(len(renders), 1)
            self.assertEqual(len(uploads), 1)
            self.assertIsNotNone(completes[0]["oss"])
            self.assertIsNone(completes[0]["content"])
            fail_work.assert_not_called()
            self.assertEqual(progress_holder["p"]["stage"], "registration_pending")
            self.assertNotEqual(job_updates[-1]["status"], "failed")

            # Second run: registration only.
            with self.assertRaises(Retry):
                _run(
                    task,
                    report_job_id="report-1",
                    biweekly_job_id=P15_JOB,
                    land_id=LAND,
                    work_item_id="w1",
                )
            self.assertEqual(len(renders), 1)
            self.assertEqual(len(uploads), 1)
            self.assertGreaterEqual(len(completes), 2)
            self.assertTrue(all(item["content"] is None for item in completes))
            fail_work.assert_not_called()

    def test_registration_exhausted_stays_pending_not_generate_failed(self):
        progress_holder = {
            "p": {
                "stage": "registration_pending",
                "official_status": "ready",
                "filename": "x.pdf",
                "mime": "application/pdf",
                "kind": "official_visual_artifact",
                "facts": {"land_id": LAND},
                "note": "ok",
                "oss_artifact": {
                    "bucket": "agric-test",
                    "key": f"reports/default/{LAND}/biweekly-{P15_JOB}.pdf",
                    "size_bytes": len(MIN_PDF),
                    "sha256": "a" * 64,
                    "content_type": "application/pdf",
                },
                "registration_attempts": br.REGISTRATION_MAX_ATTEMPTS - 1,
            }
        }

        def fake_get_job(job_id):
            return {"status": "running", "progress_json": progress_holder["p"]}

        def fake_update(a, b, status, progress=None, error=None, job_id=None):
            if progress:
                progress_holder["p"] = dict(progress)

        task = DummyTask()
        with (
            patch("agric_satellite_analysis_common.internal_api.get_job", side_effect=fake_get_job),
            patch.object(br, "_complete", side_effect=TimeoutError("read timeout")),
            patch("app.core.job_http.update_job_record", side_effect=fake_update),
            patch("app.core.job_http.complete_work_item_http") as complete_work,
            patch("app.core.job_http.progress_work_item_http"),
            patch("app.core.job_http.fail_work_item_http") as fail_work,
        ):
            out = _run(
                task,
                report_job_id="report-1",
                biweekly_job_id=P15_JOB,
                land_id=LAND,
                work_item_id="w1",
            )
        self.assertEqual(out["status"], "registration_pending")
        self.assertEqual(out["error_code"], "REGISTRATION_PENDING")
        complete_work.assert_not_called()
        fail_work.assert_called()
        self.assertTrue(fail_work.call_args.kwargs.get("retry") is True or fail_work.call_args[1].get("retry") is True)
        self.assertFalse(task.retry_countdowns)


    def test_work_item_artifact_used_when_job_progress_missing(self):
        """Job.progress_json empty or get_job fails; work-item kwargs still register only."""
        renders = []
        uploads = []
        completes = []

        def fake_render(facts, dest, figures_dir=None):
            renders.append(1)
            Path(dest).write_bytes(MIN_PDF)

        def fake_upload(path, **kwargs):
            uploads.append(path)
            raise AssertionError("must not re-upload")

        def fake_complete(*args, **kwargs):
            completes.append({"oss": kwargs.get("oss"), "content": kwargs.get("content")})
            return {"job": {"status": "ready"}}

        def fake_get_job(job_id):
            # Business job write failed: no oss_artifact here.
            return {"status": "running", "progress_json": {"stage": "running"}}

        def boom_update(*args, **kwargs):
            raise RuntimeError("job write failed")

        progress = {
            "stage": "registration_pending",
            "official_status": "ready",
            "filename": "x.pdf",
            "mime": "application/pdf",
            "kind": "official_visual_artifact",
            "facts": {"land_id": LAND},
            "note": "ok",
            "oss_artifact": {
                "bucket": "agric-test",
                "key": f"reports/default/{LAND}/biweekly-{P15_JOB}.pdf",
                "size_bytes": len(MIN_PDF),
                "sha256": "a" * 64,
                "content_type": "application/pdf",
            },
            "registration_attempts": 2,
        }
        import sys
        from types import ModuleType

        gen_mod = ModuleType("app.reports.biweekly.generate")
        gen_mod.generate_official_biweekly_pdf = fake_render
        sys.modules["app.reports.biweekly.generate"] = gen_mod
        sys.modules.setdefault("app.reports", ModuleType("app.reports"))
        sys.modules.setdefault("app.reports.biweekly", ModuleType("app.reports.biweekly"))

        task = DummyTask()
        with (
            patch("agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs", return_value={"status": "ready", "facts": {"land_id": LAND}}),
            patch("agric_satellite_analysis_common.internal_api.get_job", side_effect=fake_get_job),
            patch.object(br, "_upload_official_object", side_effect=fake_upload),
            patch.object(br, "_complete", side_effect=fake_complete),
            patch("app.core.job_http.update_job_record", side_effect=boom_update),
            patch("app.core.job_http.complete_work_item_http"),
            patch("app.core.job_http.progress_work_item_http"),
            patch("app.core.job_http.fail_work_item_http") as fail_work,
        ):
            out = _run(
                task,
                report_job_id="report-1",
                biweekly_job_id=P15_JOB,
                land_id=LAND,
                work_item_id="w1",
                registration_progress=progress,
            )
        self.assertEqual(out["status"], "ready")
        self.assertEqual(renders, [])
        self.assertEqual(uploads, [])
        self.assertEqual(len(completes), 1)
        self.assertEqual(completes[0]["oss"]["sha256"], "a" * 64)
        self.assertIsNone(completes[0]["content"])
        fail_work.assert_not_called()


    def test_job_write_failure_still_persists_work_item_progress(self):
        persisted = []

        def fake_render(facts, dest, figures_dir=None):
            Path(dest).write_bytes(MIN_PDF)

        def fake_upload(path, **kwargs):
            return {
                "bucket": "agric-test",
                "key": f"reports/default/{LAND}/biweekly-{P15_JOB}.pdf",
                "size_bytes": len(MIN_PDF),
                "sha256": "a" * 64,
                "content_type": "application/pdf",
                "kind": "official_visual_artifact",
            }

        def fake_complete(*args, **kwargs):
            return {"job": {"status": "ready"}}

        import sys
        from types import ModuleType

        gen_mod = ModuleType("app.reports.biweekly.generate")
        gen_mod.generate_official_biweekly_pdf = fake_render
        sys.modules["app.reports.biweekly.generate"] = gen_mod
        sys.modules.setdefault("app.reports", ModuleType("app.reports"))
        sys.modules.setdefault("app.reports.biweekly", ModuleType("app.reports.biweekly"))

        task = DummyTask()
        with (
            patch("agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs", return_value={"status": "ready", "facts": {"land_id": LAND, "period": {"start": "2026-05-16", "end_inclusive": "2026-05-29"}}}),
            patch("agric_satellite_analysis_common.internal_api.get_job", return_value={"status": "running", "progress_json": {}}),
            patch.object(br, "_upload_official_object", side_effect=fake_upload),
            patch.object(br, "_complete", side_effect=fake_complete),
            patch("app.core.job_http.update_job_record", side_effect=RuntimeError("job write failed")),
            patch("app.core.job_http.complete_work_item_http"),
            patch("app.core.job_http.progress_work_item_http", side_effect=lambda *a, **k: persisted.append(k.get("progress") or (a[1] if len(a) > 1 else None))),
            patch("app.core.job_http.fail_work_item_http"),
        ):
            out = _run(
                task,
                report_job_id="report-1",
                biweekly_job_id=P15_JOB,
                land_id=LAND,
                work_item_id="w1",
            )
        self.assertEqual(out["status"], "ready")
        self.assertTrue(any(
            isinstance(p, dict) and p.get("oss_artifact", {}).get("sha256") == "a" * 64
            for p in persisted
        ))


if __name__ == "__main__":
    unittest.main()
