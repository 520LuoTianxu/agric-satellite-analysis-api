"""Backfill-then-generate flow: staged errors and season roll-up."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agric_satellite_analysis_common.biweekly.catalog import LocalPackCatalog
from agric_satellite_analysis_common.biweekly.trigger import TriggerService

from app.services.biweekly_report_flow import (
    finish_generate_job,
    maybe_finalize_biweekly_job,
    start_period_generate,
    start_season_generate,
)


def _stub_pdf(facts, dest, **kwargs):
    Path(dest).write_bytes(b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n")
    return Path(dest)


NO_JOBS = {
    "dispatched": False,
    "reason": "NO_JOBS_CREATED",
    "job_ids": [],
    "job_count": 0,
    "date_from": "2026-03-21",
    "date_to": "2026-04-17",
}


class ReportFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="flow-")
        self.service = TriggerService(
            Path(self.tmp.name),
            LocalPackCatalog(),
            pdf_renderer=_stub_pdf,
        )
        self._weather = patch(
            "app.services.biweekly_report_flow.load_weather_series",
            new=AsyncMock(return_value=[]),
        )
        self._land = patch(
            "app.services.biweekly_report_flow.load_land_meta",
            new=AsyncMock(return_value={"land_name": "试验田6417"}),
        )
        self._weather.start()
        self._land.start()

    def tearDown(self):
        self._weather.stop()
        self._land.stop()
        self.tmp.cleanup()

    def test_successful_backfill_with_pack_optics_writes_official_pdf(self):
        with patch(
            "app.services.biweekly_report_flow.dispatch_bounded_backfill",
            new=AsyncMock(return_value=dict(NO_JOBS)),
        ), patch(
            "app.services.biweekly_report_flow.load_scene_observations",
            new=AsyncMock(return_value=None),
        ):
            job = asyncio.run(
                start_period_generate(
                    self.service,
                    land_id="6417",
                    season_id="2025/26",
                    period_id="12",
                )
            )
        self.assertEqual(job["status"], "ready")
        self.assertEqual((job.get("artifact") or {}).get("kind"), "official_visual_artifact")
        self.assertEqual((job.get("result") or {}).get("stage"), "done")
        self.assertIsNone(job["payload"].get("reference_planting_instance_id"))

    def test_dispatch_failed_stays_in_backfill_stage(self):
        with patch(
            "app.services.biweekly_report_flow.dispatch_bounded_backfill",
            new=AsyncMock(
                return_value={"dispatched": False, "reason": "DISPATCH_FAILED", "job_ids": []}
            ),
        ):
            job = asyncio.run(
                start_period_generate(
                    self.service,
                    land_id="6417",
                    season_id="2025/26",
                    period_id="12",
                )
            )
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "BACKFILL_FAILED")
        self.assertEqual((job.get("result") or {}).get("stage"), "backfill")
        self.assertNotEqual(job["status"], "evidence_limited")

    def test_finalize_all_failed_satellite_jobs_is_backfill_failure(self):
        queued = self.service.request_generate("6417", "2025/26", "12", defer_run=True)
        recorded = self.service.record_generate_backfill(
            queued["job_id"],
            {
                "dispatched": True,
                "reason": None,
                "job_ids": ["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
                "job_count": 1,
            },
        )
        self.assertEqual(recorded["status"], "dispatched")
        with patch(
            "app.services.biweekly_report_flow._job_statuses",
            new=AsyncMock(return_value={"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": "failed"}),
        ):
            finished = asyncio.run(maybe_finalize_biweekly_job(recorded["job_id"], service=self.service))
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "BACKFILL_FAILED")
        self.assertIn("补数", (finished.get("result") or {}).get("note") or "")
        self.assertNotEqual(finished["status"], "evidence_limited")

    def test_finalize_successful_satellite_jobs_writes_pdf(self):
        queued = self.service.request_generate("6417", "2025/26", "12", defer_run=True)
        recorded = self.service.record_generate_backfill(
            queued["job_id"],
            {
                "dispatched": True,
                "reason": None,
                "job_ids": ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"],
                "job_count": 1,
            },
        )
        with patch(
            "app.services.biweekly_report_flow._job_statuses",
            new=AsyncMock(return_value={"bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb": "completed"}),
        ), patch(
            "app.services.biweekly_report_flow.load_scene_observations",
            new=AsyncMock(return_value=None),
        ):
            finished = asyncio.run(maybe_finalize_biweekly_job(recorded["job_id"], service=self.service))
        self.assertEqual(finished["status"], "ready")
        self.assertEqual((finished.get("artifact") or {}).get("kind"), "official_visual_artifact")

    def test_scene_query_failure_is_not_evidence_limited(self):
        class NoSnapshot(LocalPackCatalog):
            def period(self, land_id, season_id, period_id):
                row = dict(super().period(land_id, season_id, period_id))
                row["snapshot"] = None
                return row

        service = TriggerService(Path(self.tmp.name) / "smart", NoSnapshot(), pdf_renderer=_stub_pdf)
        queued = service.request_generate("6417", "2025/26", "12", defer_run=True)
        service.record_generate_backfill(queued["job_id"], dict(NO_JOBS))
        with patch(
            "app.services.biweekly_report_flow.load_scene_observations",
            new=AsyncMock(return_value=None),
        ):
            finished = asyncio.run(finish_generate_job(service, queued["job_id"]))
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "SCENE_QUERY_FAILED")
        self.assertNotEqual(finished["status"], "evidence_limited")

    def test_season_one_click_emits_period_pdfs_when_backfill_not_needed(self):
        with patch(
            "app.services.biweekly_report_flow.dispatch_bounded_backfill",
            new=AsyncMock(return_value=dict(NO_JOBS)),
        ), patch(
            "app.services.biweekly_report_flow.load_scene_observations",
            new=AsyncMock(return_value=None),
        ):
            job = asyncio.run(
                start_season_generate(self.service, land_id="6417", season_id="2025/26")
            )
        self.assertEqual(job["status"], "ready")
        children = (job.get("result") or {}).get("period_results") or []
        self.assertEqual(len(children), 18)
        kinds = {item.get("artifact_kind") for item in children}
        self.assertIn("official_visual_artifact", kinds)
        self.assertIn("evidence_limited_brief", kinds)
        period12 = next(item for item in children if str(item.get("period_id")) == "12")
        self.assertEqual(period12["status"], "ready")
        period1 = next(item for item in children if str(item.get("period_id")) == "1")
        self.assertEqual(period1["status"], "evidence_limited")


if __name__ == "__main__":
    unittest.main()
