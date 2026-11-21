"""Internal biweekly inputs/complete: attach download-machine artifacts for user download."""

from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agric_satellite_analysis_common.biweekly.catalog import LocalPackCatalog
from agric_satellite_analysis_common.biweekly.trigger import TriggerService

from app.routers import internal_biweekly as biweekly_mod


class InternalBiweeklyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = TriggerService(Path(self.tmp.name), LocalPackCatalog())
        self._svc = patch("app.routers.internal_biweekly.build_trigger_service", return_value=self.service)
        self._svc.start()

    def tearDown(self):
        self._svc.stop()
        self.tmp.cleanup()

    def test_complete_ready_pdf_is_downloadable(self):
        job = self.service.request_generate("6417", "2025/26", "12", defer_run=True)
        pdf = b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n"
        body = biweekly_mod.CompleteRequest(
            status="ready",
            filename="种植双周报_6417_2026-04-04_2026-04-17.pdf",
            mime="application/pdf",
            kind="official_visual_artifact",
            content_base64=base64.b64encode(pdf).decode("ascii"),
            facts={"land_id": "6417"},
            note="正式报告 PDF 已由下载机生成。",
        )
        out = asyncio.run(biweekly_mod.biweekly_report_complete(job["job_id"], body, None))
        self.assertTrue(out["ok"])
        self.assertEqual(out["job"]["status"], "ready")
        name, data, mime = self.service.download(job["job_id"])
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 1000)

    def test_complete_failed_scene_query_is_not_evidence_limited(self):
        job = self.service.request_generate("6417", "2025/26", "12", defer_run=True)
        body = biweekly_mod.CompleteRequest(
            status="failed",
            error_code="SCENE_QUERY_FAILED",
            note="读取已入库景失败，不能当作本期没有可用光学。",
        )
        out = asyncio.run(biweekly_mod.biweekly_report_complete(job["job_id"], body, None))
        self.assertEqual(out["job"]["status"], "failed")
        self.assertEqual(out["job"]["error_code"], "SCENE_QUERY_FAILED")
        stored = self.service.get_job(job["job_id"])
        self.assertNotEqual(stored["status"], "evidence_limited")

    def test_inputs_scene_query_failure_is_explicit(self):
        job = self.service.request_generate("6417", "2025/26", "12", defer_run=True)
        # Drop snapshot so a failed live read cannot fall back to the pack.
        period = self.service.catalog.period("6417", "2025/26", "12")
        period["snapshot"] = None
        with patch.object(self.service.catalog, "period", return_value=period), patch(
            "app.routers.internal_biweekly._enrich_payload",
            new=AsyncMock(side_effect=__import__("agric_satellite_analysis_common.biweekly.models", fromlist=["ReportError"]).ReportError("SCENE_QUERY_FAILED")),
        ):
            out = asyncio.run(biweekly_mod.biweekly_report_inputs(job["job_id"], None))
        self.assertEqual(out["error_code"], "SCENE_QUERY_FAILED")
        self.assertIn("不能当作本期没有可用光学", out["note"])


if __name__ == "__main__":
    unittest.main()
