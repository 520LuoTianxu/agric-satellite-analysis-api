"""Upload success must be registered, not marked GENERATE_FAILED.

The storage object is a real OssStorage. oss2 network calls are mocked.
PDF bytes are a synthetic fixture, not the production P18 object, and this
test does not claim that production was re-checked.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import structlog

from agric_satellite_analysis_common.biweekly.official_ref import (
    registration_progress_from_work_item,
    sha256_bytes,
)
from agric_satellite_analysis_common.logging import processor_chain
from agric_satellite_analysis_common.settings import CommonSettings
from agric_satellite_analysis_common import storage as storage_mod
from agric_satellite_analysis_common import storage_client

from app.tasks import biweekly_report as br


BUCKET = "agric-isolated-fixture"
LAND = "32696"
JOB = "bj-b435c12f9a70f8a9"
REPORT = "d08fa037-ef4a-40ab-999c-3b6eb5290a26"
SECRET = "s3cret-pass"
MIN_PDF = b"%PDF-1.4\n" + b"isolated-fixture-pdf\n" * 80 + b"%%EOF\n"
FACTS = {
    "land_id": LAND,
    "period": {"start": "2026-05-02", "end_inclusive": "2026-05-15"},
}


class Retry(Exception):
    def __init__(self, countdown=None):
        self.countdown = countdown


class DummyTask:
    max_retries = 40
    default_retry_delay = 60

    def __init__(self, kwargs=None):
        self.request = type(
            "R",
            (),
            {"retries": 0, "kwargs": dict(kwargs or {})},
        )()
        self.retry_countdowns = []
        self.retry_kwargs = None

    def retry(self, countdown=None, kwargs=None):
        self.retry_countdowns.append(countdown)
        self.retry_kwargs = kwargs
        raise Retry(countdown)


def _run(task, **kwargs):
    return br.generate_biweekly_report.run.__func__(task, **kwargs)


def make_oss_storage() -> storage_mod.OssStorage:
    settings = CommonSettings(
        oss_access_key_id="AKTEST",
        oss_access_key_secret="SECRETVALUE",
        oss_bucket=BUCKET,
        oss_endpoint="https://oss-cn-beijing.aliyuncs.com",
        oss_is_cname=False,
        storage_backend="oss",
    )
    with (
        patch.object(storage_mod, "settings", settings),
        patch.object(storage_mod.oss2, "Auth", return_value=MagicMock()),
        patch.object(storage_mod.oss2, "Session", return_value=MagicMock()),
        patch.object(storage_mod.oss2, "Bucket", return_value=MagicMock()),
    ):
        storage = storage_mod.OssStorage(sleep=lambda _s: None)
    storage._multipart_threshold = 1
    storage._max_retries = 1
    # 断点目录默认在 /data/scratch。测试只写临时目录，不碰那条路径。
    storage._store_root = tempfile.mkdtemp(prefix="oss-resume-fixture-")
    return storage


def _install_renderer():
    def fake_render(facts, dest, figures_dir=None):
        Path(dest).write_bytes(MIN_PDF)

    gen_mod = ModuleType("app.reports.biweekly.generate")
    gen_mod.generate_official_biweekly_pdf = fake_render
    sys.modules["app.reports.biweekly.generate"] = gen_mod
    return fake_render


class UploadCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._modules = {
            name: sys.modules.get(name) for name in ("app.reports.biweekly.generate",)
        }

    def tearDown(self) -> None:
        for name, module in self._modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _capture_logger(self):
        buf = io.StringIO()
        previous = structlog.get_config()
        structlog.configure(
            processors=processor_chain(),
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(file=buf),
            cache_logger_on_first_use=False,
        )
        br.logger = structlog.get_logger("biweekly-upload-commit-fixture")
        return buf, previous

    def _restore_logger(self, previous) -> None:
        structlog.configure(
            processors=previous["processors"],
            wrapper_class=previous["wrapper_class"],
            context_class=previous["context_class"],
            logger_factory=previous["logger_factory"],
            cache_logger_on_first_use=previous["cache_logger_on_first_use"],
        )
        from app.core.logging import logger as app_logger

        br.logger = app_logger

    def test_real_storage_upload_then_job_and_complete_errors_only_register(
        self,
    ) -> None:
        """Confirmed path: OssStorage.bucket is a property; the task must use it.

        Hypothesis covered here, not observed in the 20:37 log: job write and
        complete raise after the upload helper has already returned.
        """
        storage = make_oss_storage()
        uploads = {"n": 0}
        renders = {"n": 0}
        completes = []
        work_progress = []
        job_updates = []
        fails = []

        def resumable(*_a, **_k):
            uploads["n"] += 1
            return None

        def fake_render(facts, dest, figures_dir=None):
            renders["n"] += 1
            Path(dest).write_bytes(MIN_PDF)

        gen_mod = ModuleType("app.reports.biweekly.generate")
        gen_mod.generate_official_biweekly_pdf = fake_render
        sys.modules["app.reports.biweekly.generate"] = gen_mod

        progress_holder = {"p": {"stage": "waiting_for_backfill"}}

        def fake_get_job(job_id):
            if job_id == REPORT:
                return {"status": "running", "progress_json": progress_holder["p"]}
            return {"status": "completed"}

        def fake_update(*args, **kwargs):
            status = (
                kwargs["status"]
                if "status" in kwargs
                else (args[2] if len(args) > 2 else None)
            )
            progress = kwargs.get("progress")
            job_updates.append(
                {
                    "status": status,
                    "error": kwargs.get("error"),
                    "stage": (progress or {}).get("stage"),
                }
            )
            if (
                status == "running"
                and isinstance(progress, dict)
                and progress.get("oss_artifact")
            ):
                raise RuntimeError("job write failed")
            if status == "completed":
                raise RuntimeError("job align failed")
            if progress:
                progress_holder["p"] = dict(progress)

        def fake_progress(_work_id, payload, worker_id=None):
            work_progress.append(payload)

        def fake_complete(*_a, **kwargs):
            completes.append(
                {
                    "status": kwargs.get("status"),
                    "oss": kwargs.get("oss"),
                    "content": kwargs.get("content"),
                    "error_code": kwargs.get("error_code"),
                }
            )
            if kwargs.get("status") != "registration_pending":
                raise TimeoutError("complete read timeout")
            raise TimeoutError("registration_pending notify timeout")

        def fake_fail(*_a, **kwargs):
            fails.append(kwargs or {})

        task_kwargs = {
            "report_job_id": REPORT,
            "biweekly_job_id": JOB,
            "land_id": LAND,
            "work_item_id": "e50a590b-fixture",
            "wait_for_job_ids": ["backfill-1"],
        }
        task = DummyTask(kwargs=task_kwargs)
        with (
            patch.object(storage_mod.oss2, "resumable_upload", side_effect=resumable),
            patch.object(storage_mod.oss2, "ResumableStore", return_value=MagicMock()),
            patch.object(storage_client, "_running_in_celery_task", return_value=True),
            patch.object(storage_mod, "get_storage", return_value=storage),
            patch(
                "agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs",
                return_value={"status": "ready", "facts": FACTS},
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.get_job",
                side_effect=fake_get_job,
            ),
            patch("app.core.job_http.update_job_record", side_effect=fake_update),
            patch(
                "app.core.job_http.progress_work_item_http", side_effect=fake_progress
            ),
            patch("app.core.job_http.complete_work_item_http") as complete_work,
            patch("app.core.job_http.fail_work_item_http", side_effect=fake_fail),
            patch.object(br, "_complete", side_effect=fake_complete),
        ):
            with self.assertRaises(Retry):
                _run(task, **task_kwargs)

        self.assertEqual(uploads["n"], 1)
        self.assertEqual(renders["n"], 1)
        self.assertTrue(work_progress)
        artifact = work_progress[0]["oss_artifact"]
        self.assertEqual(artifact["bucket"], BUCKET)
        self.assertEqual(
            artifact["key"],
            f"reports/default/{LAND}/biweekly-{JOB}.pdf",
        )
        self.assertEqual(artifact["sha256"], sha256_bytes(MIN_PDF))
        self.assertEqual(artifact["size_bytes"], len(MIN_PDF))
        self.assertNotIn("SECRETVALUE", json.dumps(artifact))
        self.assertTrue(
            all(item.get("error_code") != "GENERATE_FAILED" for item in completes)
        )
        self.assertTrue(all(item["content"] is None for item in completes))
        self.assertTrue(all(item["oss"] for item in completes))
        complete_work.assert_not_called()
        self.assertFalse(
            any(
                not item.get("retry", False) and "GENERATE" in str(item)
                for item in fails
            )
        )
        self.assertNotIn("failed", [item["status"] for item in job_updates])
        self.assertTrue(
            any(item["stage"] == "registration_pending" for item in job_updates)
        )
        saved = task.retry_kwargs["registration_progress"]
        self.assertEqual(saved["oss_artifact"]["sha256"], sha256_bytes(MIN_PDF))
        self.assertEqual(
            saved["biweekly_job_id"]
            if "biweekly_job_id" in saved
            else task_kwargs["biweekly_job_id"],
            JOB,
        )
        self.assertIn("biweekly_job_id", task.retry_kwargs)
        self.assertEqual(task.retry_kwargs["biweekly_job_id"], JOB)

        # Process did not finish registration. The work-item payload is what
        # the claim agent passes back; the next run must not render or upload.
        replay = registration_progress_from_work_item(
            {"progress_json": work_progress[-1], "payload_json": {}}
        )
        self.assertEqual(replay["oss_artifact"]["key"], artifact["key"])
        task2 = DummyTask(kwargs={**task_kwargs, "registration_progress": replay})

        def fake_complete_ok(*_a, **kwargs):
            completes.append(
                {
                    "status": kwargs.get("status"),
                    "oss": kwargs.get("oss"),
                    "content": kwargs.get("content"),
                    "error_code": kwargs.get("error_code"),
                }
            )
            return {"job": {"status": "ready"}}

        with (
            patch.object(storage_mod.oss2, "resumable_upload", side_effect=resumable),
            patch.object(storage_mod.oss2, "ResumableStore", return_value=MagicMock()),
            patch.object(storage_client, "_running_in_celery_task", return_value=True),
            patch.object(storage_mod, "get_storage", return_value=storage),
            patch(
                "agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs",
                side_effect=AssertionError("inputs must not be loaded again"),
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.get_job",
                side_effect=fake_get_job,
            ),
            patch("app.core.job_http.update_job_record", side_effect=fake_update),
            patch(
                "app.core.job_http.progress_work_item_http", side_effect=fake_progress
            ),
            patch("app.core.job_http.complete_work_item_http", return_value=False),
            patch("app.core.job_http.fail_work_item_http", side_effect=fake_fail),
            patch.object(br, "_complete", side_effect=fake_complete_ok),
        ):
            out = _run(task2, **task_kwargs, registration_progress=replay)

        self.assertEqual(uploads["n"], 1)
        self.assertEqual(renders["n"], 1)
        self.assertEqual(out["status"], "ready")
        self.assertEqual(out.get("job_align"), "pending")
        self.assertTrue(fails)
        self.assertTrue(
            fails[-1].get("retry") is True or fails[-1].get("retry") is True
        )

    def test_bucket_property_failure_uses_upload_uri_and_does_not_generate_fail(
        self,
    ) -> None:
        """Hypothesis: bucket_name raises after oss_upload_ok. The upload return uri still has the bucket."""
        storage = make_oss_storage()
        _install_renderer()
        buf, previous = self._capture_logger()
        work_progress = []
        completes = []

        def boom(*_a, **_k):
            raise RuntimeError(f"property read failed password={SECRET}")

        def fake_complete(*_a, **kwargs):
            completes.append(kwargs.get("error_code"))
            raise TimeoutError("complete timeout")

        task = DummyTask(
            kwargs={
                "report_job_id": REPORT,
                "biweekly_job_id": JOB,
                "land_id": LAND,
                "work_item_id": "e50a590b-fixture",
            }
        )
        try:
            with (
                patch.object(storage_mod.oss2, "resumable_upload", return_value=None),
                patch.object(
                    storage_mod.oss2, "ResumableStore", return_value=MagicMock()
                ),
                patch.object(
                    storage_client, "_running_in_celery_task", return_value=True
                ),
                patch.object(storage_mod, "get_storage", return_value=storage),
                patch.object(storage_mod, "bucket_name", side_effect=boom),
                patch(
                    "agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs",
                    return_value={"status": "ready", "facts": FACTS},
                ),
                patch(
                    "agric_satellite_analysis_common.internal_api.get_job",
                    return_value={
                        "status": "running",
                        "progress_json": {"stage": "waiting_for_backfill"},
                    },
                ),
                patch("app.core.job_http.update_job_record"),
                patch(
                    "app.core.job_http.progress_work_item_http",
                    side_effect=lambda *_a, **_k: work_progress.append(
                        _a[1] if len(_a) > 1 else _k.get("progress")
                    ),
                ),
                patch("app.core.job_http.fail_work_item_http") as fail_work,
                patch.object(br, "_complete", side_effect=fake_complete),
            ):
                with self.assertRaises(Retry):
                    _run(
                        task,
                        report_job_id=REPORT,
                        biweekly_job_id=JOB,
                        land_id=LAND,
                        work_item_id="e50a590b-fixture",
                    )
        finally:
            text = buf.getvalue()
            self._restore_logger(previous)

        self.assertEqual(work_progress[0]["oss_artifact"]["bucket"], BUCKET)
        self.assertNotIn("GENERATE_FAILED", completes)
        fail_work.assert_not_called()
        self.assertIn("biweekly_oss_bucket_property_failed", text)
        line = next(
            item
            for item in text.splitlines()
            if "biweekly_oss_bucket_property_failed" in item
        )
        payload = json.loads(line)
        self.assertEqual(payload["exc_type"], "RuntimeError")
        self.assertIn("Traceback", payload["exc_stack"])
        self.assertNotIn("exc_info", payload)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("SECRETVALUE", text)

    def test_pre_upload_failure_log_contains_redacted_stack(self) -> None:
        buf, previous = self._capture_logger()
        try:
            with (
                patch(
                    "agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs",
                    side_effect=RuntimeError(f"inputs down password={SECRET}"),
                ),
                patch("app.core.job_http.update_job_record"),
                patch("app.core.job_http.fail_work_item_http"),
                patch.object(br, "_complete", return_value={"ok": True}),
                patch(
                    "agric_satellite_analysis_common.internal_api.get_job",
                    return_value={"status": "running", "progress_json": {}},
                ),
            ):
                out = _run(
                    DummyTask(),
                    report_job_id=REPORT,
                    biweekly_job_id=JOB,
                    land_id=LAND,
                    work_item_id="e50a590b-fixture",
                )
        finally:
            text = buf.getvalue()
            self._restore_logger(previous)
        self.assertEqual(out["error_code"], "GENERATE_FAILED")
        payload = json.loads(
            [line for line in text.splitlines() if "biweekly_report_failed" in line][-1]
        )
        self.assertEqual(payload["exc_type"], "RuntimeError")
        self.assertEqual(payload["land_id"], LAND)
        self.assertIn("Traceback", payload["exc_stack"])
        self.assertNotIn("exc_info", payload)
        self.assertNotIn(SECRET, text)


if __name__ == "__main__":
    unittest.main()
