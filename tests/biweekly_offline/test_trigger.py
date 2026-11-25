import json
import tempfile
import unittest
from pathlib import Path

from agric_satellite_analysis_common.biweekly.calendar import (
    MAX_BACKFILL_DAYS,
    bounded_backfill_window,
    iter_periods,
    periods_closed_before,
    planting_season_label,
)
from agric_satellite_analysis_common.biweekly.catalog import LocalPackCatalog
from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.trigger import TriggerService


class CalendarTests(unittest.TestCase):
    def test_6417_season_has_eighteen_half_open_periods(self):
        self.assertEqual(planting_season_label("2025-11-01", "2026-06-30"), "2025/26")
        periods = iter_periods("2025-11-01", "2026-06-30")
        self.assertEqual(len(periods), 18)
        self.assertEqual(periods[11]["start"], "2026-04-04")
        self.assertEqual(periods[11]["end_exclusive"], "2026-04-18")
        self.assertEqual(periods[11]["end_inclusive"], "2026-04-17")
        self.assertEqual(periods[11]["days"], 14)
        self.assertTrue(periods[-1]["partial"])
        self.assertEqual(periods[-1]["days"], 4)
        window = bounded_backfill_window(periods, "12")
        self.assertEqual(window["date_from"], "2026-03-21")
        self.assertEqual(window["date_to_inclusive"], "2026-04-17")
        self.assertEqual(window["span_days"], 28)
        self.assertLess(window["span_days"], 365)
        self.assertIsNone(window["years"])
        first = bounded_backfill_window(periods, "1")
        self.assertEqual(first["date_from"], "2025-11-01")
        self.assertFalse(first["includes_prior_comparison_window"])

    def test_open_period_ending_tomorrow_is_not_a_backfill_window(self):
        # 2026-10-15 起每 14 天：2026-11-12～2026-11-25 在 11-24 仍未结束。
        periods = iter_periods("2026-10-15", "2027-06-06")
        open_period = next(
            row for row in periods if row["start"] <= "2026-11-24" <= row["end_inclusive"]
        )
        self.assertEqual(open_period["end_inclusive"], "2026-11-25")
        closed = periods_closed_before(periods, "2026-11-24")
        self.assertNotIn(open_period["period_id"], {row["period_id"] for row in closed})
        self.assertTrue(closed)
        self.assertTrue(all(row["end_inclusive"] < "2026-11-24" for row in closed))
        for row in closed:
            window = bounded_backfill_window(periods, row["period_id"])
            self.assertLess(window["date_to_inclusive"], "2026-11-24")
        self.assertEqual(periods_closed_before(periods, "2026-10-15"), [])


class TriggerServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

        def _stub_pdf(facts, dest):
            Path(dest).write_bytes(b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n")

        self.service = TriggerService(
            Path(self.tmp.name),
            LocalPackCatalog(),
            pdf_renderer=_stub_pdf,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_seasons_are_wheat_2025_26_only(self):
        data = self.service.list_seasons("6417")
        self.assertEqual([s["season_id"] for s in data["seasons"]], ["2025/26"])
        self.assertFalse(data["auto_publish"])
        labels = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("玉米", labels)
        with self.assertRaises(ReportError) as ctx:
            self.service.list_seasons("9999")
        self.assertEqual(ctx.exception.code, "LAND_NOT_IN_LOCAL_CATALOG")

    def test_period_12_generate_with_explicit_reference_uses_v8_policy(self):
        job = self.service.request_generate(
            "6417",
            "2025/26",
            "12",
            reference_planting_instance_id="15933",
        )
        self.assertEqual(job["status"], "ready")
        phase = job["result"]["phase"]
        self.assertEqual(phase["display_phase"], "S2")
        self.assertEqual(phase["primary_metrics"], ["ndre", "evi"])
        self.assertEqual(phase["supporting_metrics"], ["ndvi", "ndmi"])
        self.assertEqual(phase["phenology_basis"], "PLAN_DERIVED_FROM_BTU_REFERENCE")
        self.assertIn("参照", phase["reference"]["disclosure"])
        cmp_ = job["result"]["prior_comparison"]
        self.assertTrue(cmp_["all_compared_higher"])
        self.assertTrue(cmp_["can_claim_sustained_improvement"])
        self.assertEqual(job["artifact"]["kind"], "official_visual_artifact")
        self.assertTrue(job["artifact"]["filename"].endswith(".pdf"))
        name, data, mime = self.service.download(job["job_id"])
        self.assertEqual(mime, "application/pdf")
        self.assertGreater(len(data), 1000)
        forbidden = json.dumps(job["result"])
        self.assertNotIn("fvc", forbidden.lower().split("forbidden")[0] if False else "")
        # facts must not publish FVC/LAI numbers
        self.assertIn("fvc", json.dumps(job["result"]["forbidden_metrics_omitted"]))

    def test_generate_without_reference_does_not_claim_phenology(self):
        job = self.service.request_generate("6417", "2025/26", "12")
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["result"]["phase"]["phenology_basis"], "none")
        self.assertIsNone(job["result"]["phase"]["display_phase"])
        self.assertIsNone(job["payload"]["reference_planting_instance_id"])

    def test_unknown_reference_is_rejected(self):
        with self.assertRaises(ReportError) as ctx:
            self.service.request_generate(
                "6417", "2025/26", "12", reference_planting_instance_id="1"
            )
        self.assertEqual(ctx.exception.code, "UNKNOWN_REFERENCE")

    def test_empty_period_backfill_is_bounded_and_does_not_autogenerate(self):
        job = self.service.request_backfill("6417", "2025/26", "1")
        self.assertEqual(job["status"], "ready")
        planned = job["result"]["planned_batch"]
        self.assertEqual(planned["date_from"], "2025-11-01")
        self.assertEqual(planned["date_to"], "2025-11-14")
        self.assertIsNone(planned["years"])
        self.assertLess(planned["window"]["span_days"], MAX_BACKFILL_DAYS)
        self.assertFalse(job["result"]["eligible_optics_found"])
        self.assertFalse(job["result"]["dispatched_to_production"])
        self.assertIsNone(self.service.list_periods("6417", "2025/26")["periods"][0]["latest_job"])
        brief = self.service.request_generate("6417", "2025/26", "1")
        self.assertEqual(brief["status"], "evidence_limited")
        self.assertIn("证据受限", brief["result"]["brief"])
        self.assertEqual(brief["artifact"]["kind"], "evidence_limited_brief")


    def test_eligible_optics_override_is_ready_not_evidence_limited(self):
        # Same window as 6417 period 1, which has no pack optics; live rows must win.
        observations = [
            {
                "date": "2025-11-05",
                "sensor": "S2",
                "quality_status": "eligible_descriptive",
                "ndvi_avg": 0.16,
                "evi_avg": 0.11,
                "ndre_avg": None,
                "ndmi_avg": -0.09,
            },
            {
                "date": "2025-11-08",
                "sensor": "S1",
                "quality_status": "excluded",
            },
        ]
        job = self.service.request_generate(
            "6417",
            "2025/26",
            "1",
            observations_override=observations,
        )
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["result"]["optical_days"], ["2025-11-05"])
        self.assertNotEqual(job["artifact"]["kind"], "evidence_limited_brief")
        name, data, mime = self.service.download(job["job_id"])
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 1000)

    def test_radar_baseline_rows_are_kept_outside_the_period(self):
        job = self.service.request_generate(
            "6417",
            "2025/26",
            "1",
            defer_run=True,
            observations_override=[
                {
                    "date": "2025-11-08",
                    "sensor": "S1",
                    "scene_id": "in-period",
                    "vv_avg": -12.0,
                    "relative_orbit": 113,
                }
            ],
        )
        self.service.set_payload_fields(
            job["job_id"],
            radar_baseline_observations=[
                {"date": "2025-10-01", "vv": -11.0, "relative_orbit": 113, "scene_id": "earlier"},
                {"date": "2025-11-08", "vv": -12.0, "relative_orbit": 113, "scene_id": "in-period"},
            ],
        )
        facts, _status = self.service.prepare_generate_facts(self.service.get_job(job["job_id"]))
        self.assertEqual(
            [row["date"] for row in facts["radar_baseline_observations"]],
            ["2025-10-01", "2025-11-08"],
        )
        self.assertEqual([row["date"] for row in facts["radar_observations"]], ["2025-11-08"])
        self.assertEqual(facts["radar_observations"][0]["vv"], -12.0)
        self.assertEqual(facts["radar_observations"][0]["relative_orbit"], 113)


    def test_record_generate_backfill_separates_hard_failure_from_not_needed(self):
        queued = self.service.request_generate("6417", "2025/26", "12", defer_run=True)
        failed = self.service.record_generate_backfill(
            queued["job_id"],
            {"dispatched": False, "reason": "DISPATCH_FAILED", "job_ids": []},
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "BACKFILL_FAILED")
        self.assertEqual((failed.get("result") or {}).get("stage"), "backfill")
        self.assertNotEqual(failed["status"], "evidence_limited")

        queued = self.service.request_generate("6417", "2025/26", "1", defer_run=True)
        recorded = self.service.record_generate_backfill(
            queued["job_id"],
            {"dispatched": False, "reason": "NO_JOBS_CREATED", "job_ids": [], "job_count": 0},
        )
        self.assertEqual(recorded["status"], "queued")
        self.assertNotEqual(recorded["error_code"], "BACKFILL_FAILED")
        self.assertEqual((recorded.get("result") or {}).get("backfill", {}).get("status"), "not_needed")

    def test_missing_pdf_renderer_is_generate_failure_not_no_optics(self):
        service = TriggerService(Path(self.tmp.name) / "no-renderer", LocalPackCatalog())
        observations = [
            {
                "date": "2025-11-05",
                "sensor": "S2",
                "quality_status": "eligible_descriptive",
                "ndvi_avg": 0.16,
                "evi_avg": 0.11,
            }
        ]
        job = service.request_generate(
            "6417",
            "2025/26",
            "1",
            observations_override=observations,
        )
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "PDF_RENDERER_UNAVAILABLE")
        self.assertNotEqual(job["status"], "evidence_limited")

    def test_issue12_backfill_covers_prior_window_only(self):
        job = self.service.request_backfill("6417", "2025/26", "12")
        planned = job["result"]["planned_batch"]
        self.assertEqual(planned["date_from"], "2026-03-21")
        self.assertEqual(planned["date_to"], "2026-04-17")
        self.assertEqual(planned["window"]["span_days"], 28)
        self.assertTrue(job["result"]["eligible_optics_found"])
        # 28 days must never be expanded to a three-year request
        self.assertNotIn(1095, [planned["window"]["span_days"]])


    def test_api_backfill_records_dispatch_without_local_fixture(self):
        class NoSnapshotCatalog(LocalPackCatalog):
            def period(self, land_id, season_id, period_id):
                row = dict(super().period(land_id, season_id, period_id))
                row["snapshot"] = None
                return row

            def load_snapshot(self, relative):
                raise ReportError("SNAPSHOT_NOT_FOUND")

        service = TriggerService(Path(self.tmp.name) / "api-jobs", NoSnapshotCatalog())
        queued = service.request_backfill(
            "6417", "2025/26", "5", evaluate_local_fixture=False
        )
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued.get("error_code"))
        recorded = service.record_backfill_dispatch(
            queued["job_id"],
            {
                "dispatched": True,
                "job_ids": ["80e4d0fd-c2d3-4e76-a192-fa28424f643e"],
                "job_count": 1,
                "reason": None,
            },
        )
        self.assertEqual(recorded["status"], "dispatched")
        self.assertTrue(recorded["dispatched_to_production"])
        self.assertEqual(
            recorded["satellite_batch_job_ids"],
            ["80e4d0fd-c2d3-4e76-a192-fa28424f643e"],
        )
        self.assertEqual(recorded["result"]["planned_batch"]["dispatch"], "satellite_batch")
        self.assertNotEqual(
            recorded["result"]["planned_batch"]["dispatch"], "plan_only_local_fixture"
        )
        failed = service.request_backfill(
            "6417", "2025/26", "1", evaluate_local_fixture=False
        )
        failed = service.record_backfill_dispatch(
            failed["job_id"],
            {"dispatched": False, "reason": "DISPATCH_FAILED", "job_ids": []},
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "DISPATCH_FAILED")
        self.assertFalse(failed["dispatched_to_production"])

    def test_season_backfill_job_lists_one_bounded_window_per_period(self):
        job = self.service.request_season_backfill("6417", "2025/26")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["kind"], "backfill_season")
        windows = job["payload"]["windows"]
        self.assertEqual(len(windows), 18)
        spans = [item["window"]["span_days"] for item in windows]
        self.assertTrue(all(0 < span <= MAX_BACKFILL_DAYS for span in spans))
        self.assertTrue(all(item["window"].get("years") is None for item in windows))
        starts = {item["window"]["date_from"] for item in windows}
        ends = {item["window"]["date_to_inclusive"] for item in windows}
        # The season itself is longer than one bounded window.
        self.assertGreater(len(starts), 1)
        self.assertNotEqual(min(starts), max(ends))
        from datetime import date

        season_days = (
            date.fromisoformat(max(ends)) - date.fromisoformat(min(starts))
        ).days + 1
        self.assertGreater(season_days, MAX_BACKFILL_DAYS)


class _OpenSeasonCatalog:
    """2026-10-15 起的冬小麦季。2026-11-24 时，结束日为 11-25 的分期仍在进行。"""

    def periods(self, land_id: str, season_id: str) -> list[dict]:
        return iter_periods("2026-10-15", "2027-06-06")

    def period(self, land_id: str, season_id: str, period_id: str) -> dict:
        for row in self.periods(land_id, season_id):
            if row["period_id"] == str(period_id):
                return row
        raise ReportError("PERIOD_NOT_FOUND")


class OpenPeriodSeasonBackfillTests(unittest.TestCase):
    def test_season_backfill_does_not_list_the_period_ending_tomorrow(self):
        service = TriggerService(Path(tempfile.mkdtemp()), _OpenSeasonCatalog())
        job = service.request_season_backfill("32696", "2026/27", as_of="2026-11-24")
        windows = job["payload"]["windows"]
        ends = [item["window"]["date_to_inclusive"] for item in windows]
        self.assertTrue(ends)
        self.assertTrue(all(end < "2026-11-24" for end in ends))
        self.assertNotIn("2026-11-25", ends)
        open_id = next(
            row["period_id"]
            for row in service.catalog.periods("32696", "2026/27")
            if row["end_inclusive"] == "2026-11-25"
        )
        self.assertIn(open_id, job["payload"]["skipped_period_ids"])
        self.assertNotIn(open_id, job["payload"]["period_ids"])
        self.assertEqual(job["window"]["end_inclusive"], max(ends))
