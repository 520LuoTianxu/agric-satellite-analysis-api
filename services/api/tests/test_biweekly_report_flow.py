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
        self._report = patch(
            "app.services.biweekly_report_flow.dispatch_biweekly_report",
            new=AsyncMock(
                return_value={
                    "dispatched": True,
                    "reason": None,
                    "job_ids": ["report-job-1"],
                    "job_count": 1,
                    "job_id": "report-job-1",
                    "job_type": "biweekly_report",
                    "claim_type": "biweekly_report",
                    "display_name": "双周报正式出报",
                }
            ),
        )
        self._report.start()

    def tearDown(self):
        self._weather.stop()
        self._land.stop()
        self._report.stop()
        self.tmp.cleanup()

    def test_successful_backfill_dispatches_report_and_does_not_render_locally(self):
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
        self.assertEqual(job["status"], "dispatched")
        self.assertNotEqual((job.get("artifact") or {}).get("kind"), "official_visual_artifact")
        self.assertEqual((job.get("result") or {}).get("stage"), "report")
        self.assertEqual(((job.get("result") or {}).get("report") or {}).get("job_type"), "biweekly_report")
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

    def test_finalize_successful_satellite_jobs_dispatches_report(self):
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
        self.assertEqual(finished["status"], "dispatched")
        self.assertEqual((finished.get("result") or {}).get("stage"), "report")
        self.assertEqual(((finished.get("result") or {}).get("report") or {}).get("job_type"), "biweekly_report")
        self.assertNotEqual((finished.get("artifact") or {}).get("kind"), "official_visual_artifact")

    def test_generate_dispatch_does_not_treat_missing_scenes_as_no_optics(self):
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
        self.assertEqual(job["status"], "dispatched")
        self.assertNotEqual(job["status"], "evidence_limited")
        self.assertNotEqual(job.get("error_code"), "SCENE_QUERY_FAILED")
        self.assertEqual(((job.get("result") or {}).get("report") or {}).get("job_type"), "biweekly_report")

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
        self.assertEqual(job["status"], "dispatched")
        children = (job.get("result") or {}).get("period_results") or []
        self.assertEqual(len(children), 18)
        self.assertTrue(all(item.get("status") == "dispatched" for item in children))
        self.assertTrue(all(not item.get("artifact_kind") for item in children))


class _OpenSeasonCatalog:
    def periods(self, land_id, season_id):
        from agric_satellite_analysis_common.biweekly.calendar import iter_periods

        return iter_periods("2026-10-15", "2027-06-06")

    def period(self, land_id, season_id, period_id):
        for row in self.periods(land_id, season_id):
            if row["period_id"] == str(period_id):
                return row
        raise AssertionError(period_id)


class OpenPeriodSeasonGenerateTests(unittest.TestCase):
    def test_one_click_does_not_dispatch_the_period_ending_on_nov_25(self):
        service = TriggerService(
            Path(tempfile.mkdtemp(prefix="open-period-")),
            _OpenSeasonCatalog(),
            pdf_renderer=_stub_pdf,
        )
        captured = []

        async def fake_dispatch(land_id, window, **kwargs):
            captured.append(window)
            return {
                "dispatched": True,
                "reason": None,
                "job_ids": [f"batch-{window['current_period_id']}"],
                "job_count": 1,
                "date_from": window["date_from"],
                "date_to": window["date_to_inclusive"],
            }

        with patch(
            "app.services.biweekly_report_flow.dispatch_bounded_backfill",
            new=fake_dispatch,
        ), patch(
            "app.services.biweekly_report_flow.load_weather_series",
            new=AsyncMock(return_value=[]),
        ), patch(
            "app.services.biweekly_report_flow.load_land_meta",
            new=AsyncMock(return_value={"land_name": "试验田"}),
        ), patch(
            "app.services.biweekly_report_flow.dispatch_biweekly_report",
            new=AsyncMock(
                return_value={
                    "dispatched": True,
                    "job_ids": ["report-1"],
                    "job_count": 1,
                    "job_type": "biweekly_report",
                }
            ),
        ):
            job = asyncio.run(
                start_season_generate(
                    service,
                    land_id="32696",
                    season_id="2026/27",
                    as_of="2026-11-24",
                )
            )
        ends = [window["date_to_inclusive"] for window in captured]
        self.assertTrue(ends)
        self.assertNotIn("2026-11-25", ends)
        self.assertTrue(all(end < "2026-11-24" for end in ends))
        period_ids = [item["period_id"] for item in job["result"]["period_results"]]
        open_id = next(
            row["period_id"]
            for row in service.catalog.periods("32696", "2026/27")
            if row["end_inclusive"] == "2026-11-25"
        )
        self.assertNotIn(open_id, period_ids)
        self.assertIn("尚未结束", job["result"]["note"])
        self.assertEqual(job["status"], "dispatched")


if __name__ == "__main__":
    unittest.main()
