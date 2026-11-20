import math
import unittest
from fixtures import source
from agric_satellite_analysis_common.biweekly.snapshot import build_snapshot
from agric_satellite_analysis_common.biweekly.models import ReportError


class SnapshotTests(unittest.TestCase):
    def test_cross_period_unique_primary_and_plan_not_observed(self):
        s = build_snapshot(source())
        d = s.data
        self.assertEqual(d["primary_period_id"], "rising")
        self.assertEqual(
            [(x["period_id"], x["days"]) for x in d["covered_periods"]],
            [("rising", 6), ("jointing", 8)],
        )
        self.assertEqual(d["covered_periods"][0]["phase"], "S1")
        self.assertEqual(d["covered_periods"][1]["phase"], "S2")
        self.assertEqual(d["phenology_basis"], "planned_btu_not_observed")

    def test_frozen_copy_hash_and_source_revision(self):
        raw = source()
        a = build_snapshot(raw)
        raw["evidence"][0]["value"] = 0.99
        self.assertEqual(a.data["facts"]["rs.ndvi"]["value"], 0.64)
        view = a.data
        view["facts"].clear()
        self.assertIn("rs.ndvi", a.data["facts"])
        self.assertEqual(a.sha256, build_snapshot(source()).sha256)
        raw = source()
        raw["source_revision"] = "synthetic-input-v2"
        self.assertNotEqual(a.sha256, build_snapshot(raw).sha256)

    def test_missing_extended_metrics_never_invent_calibration(self):
        d = build_snapshot(source()).data
        self.assertNotIn("rs.fvc", d["facts"])
        self.assertIn("FVC_UNCALIBRATED", d["quality_flags"])
        self.assertIn("LAI_MODEL_UNAVAILABLE", d["quality_flags"])
        self.assertIn("DISASTER_EVIDENCE_INCOMPLETE", d["quality_flags"])
        self.assertEqual(
            next(e for e in d["evidence"] if e["id"] == "rs.fvc")["value"], 0.72
        )

    def test_missing_stage_and_observations_remain_explicit(self):
        raw = source()
        raw["stages"] = []
        raw["sampling"]["valid_days"] = 0
        d = build_snapshot(raw).data
        self.assertIsNone(d["primary_period_id"])
        self.assertIn("STAGE_PLAN_MISSING", d["quality_flags"])
        self.assertNotIn("rs.ndvi", d["facts"])
        self.assertIn("weather.rain", d["facts"])

    def test_invalid_inputs(self):
        cases = []
        x = source()
        x["crop"] = "unknown"
        cases.append(x)
        x = source()
        x["stages"][0]["stage_key"] = "unmapped"
        cases.append(x)
        x = source()
        x["stages"][1]["start"] = "2027-03-20"
        cases.append(x)
        x = source()
        x["evidence"][0]["value"] = math.nan
        cases.append(x)
        x = source()
        x["evidence"][0]["value"] = True
        cases.append(x)
        x = source()
        x["evidence"][0]["value"] = 2
        cases.append(x)
        x = source()
        x["evidence"][0]["start"] = "2027-03-01"
        cases.append(x)
        x = source()
        x["evidence"][1]["id"] = x["evidence"][0]["id"]
        cases.append(x)
        x = source()
        x["window"]["end"] = "2027-03-30"
        cases.append(x)
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ReportError):
                build_snapshot(raw)

    def test_maize_mapping_is_crop_specific_and_gaps_are_not_filled(self):
        raw = source()
        raw["crop"] = "maize"
        raw["stages"][0]["stage_key"] = "jointing"
        raw["stages"][1]["stage_key"] = "small_trumpet"
        d = build_snapshot(raw).data
        self.assertEqual([p["phase"] for p in d["covered_periods"]], ["S1", "S2"])
        raw["stages"][1]["start"] = "2027-03-22"
        d = build_snapshot(raw).data
        self.assertEqual(sum(p["days"] for p in d["covered_periods"]), 13)
        self.assertIn("STAGE_COVERAGE_INCOMPLETE", d["quality_flags"])

    def test_geometry_or_processing_version_invalidates_snapshot_identity(self):
        original = build_snapshot(source())
        for key in ["geometry_fingerprint", "processing_versions"]:
            raw = source()
            if key == "geometry_fingerprint":
                raw[key] = "mock-geometry-v2"
            else:
                raw[key]["mask"] = "mock-mask-v2"
            self.assertNotEqual(original.sha256, build_snapshot(raw).sha256)
