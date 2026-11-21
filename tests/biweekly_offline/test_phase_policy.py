import unittest
from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.phase_policy import (
    compare_to_prior,
    display_phase,
    resolve_wheat_stage,
    wheat_snapshot_phases,
)


class PhasePolicyTests(unittest.TestCase):
    def test_official_wheat_mapping(self):
        self.assertEqual(resolve_wheat_stage("RS")["phase_label"], "S2")
        self.assertEqual(resolve_wheat_stage("rising")["phase_label"], "S2")
        self.assertEqual(resolve_wheat_stage("OW")["tags"], ["DORMANCY"])
        self.assertIsNone(resolve_wheat_stage("SG")["phase_label"])
        self.assertEqual(resolve_wheat_stage("SG")["status"], "NOT_APPLICABLE")
        with self.assertRaises(ReportError) as ctx:
            resolve_wheat_stage("booting")
        self.assertEqual(ctx.exception.code, "STAGE_MAPPING_UNAVAILABLE")
        phases = wheat_snapshot_phases()
        self.assertEqual(phases["rising"], "S2")
        self.assertEqual(phases["GR"], "S1")
        self.assertIsNone(phases["SG"])
        self.assertNotIn("booting", phases)

    def test_issue12_observations_stay_in_s2_and_next_week_is_s3(self):
        intervals = [
            {"stage_code": "JT", "start": "2026-04-04", "end_exclusive": "2026-04-16"},
            {"stage_code": "HA", "start": "2026-04-16", "end_exclusive": "2026-05-02"},
        ]
        result = display_phase(
            planned_intervals=intervals,
            observation_dates=["2026-04-06", "2026-04-11"],
            window_start="2026-04-04",
            end_exclusive="2026-04-18",
        )
        self.assertEqual(result["display_phase"], "S2")
        self.assertEqual(result["primary_metrics"], ["ndre", "evi"])
        self.assertEqual(result["supporting_metrics"], ["ndvi", "ndmi"])
        self.assertEqual(result["transitions"], [
            {
                "on": "2026-04-16",
                "from_stage": "JT",
                "from_phase": "S2",
                "to_stage": "HA",
                "to_phase": "S3",
            }
        ])
        self.assertEqual(result["next_week"]["phase_label"], "S3")
        self.assertEqual(result["next_week"]["primary_metrics"], ["ndre", "ndmi"])
        self.assertTrue(all(row["phase_label"] == "S2" for row in result["observation_stages"]))

    def test_prior_comparison_requires_same_metrics_and_two_current_days(self):
        current = [
            {"date": "2026-04-06", "ndvi_avg": 0.713, "ndre_avg": 0.574, "evi_avg": 0.554, "ndmi_avg": 0.345},
            {"date": "2026-04-11", "ndvi_avg": 0.726, "ndre_avg": 0.556, "evi_avg": 0.591, "ndmi_avg": 0.362},
        ]
        prior = [
            {"date": "2026-04-01", "ndvi_avg": 0.649, "ndre_avg": 0.488, "evi_avg": 0.517, "ndmi_avg": 0.304},
            {"date": "2026-04-03", "ndvi_avg": 0.641, "ndre_avg": 0.484, "evi_avg": 0.467, "ndmi_avg": 0.298},
        ]
        result = compare_to_prior(current, prior, ["ndre", "evi", "ndvi", "ndmi"])
        self.assertTrue(result["all_compared_higher"])
        self.assertTrue(result["can_claim_sustained_improvement"])
        one = compare_to_prior(current[:1], prior, ["ndvi"])
        self.assertTrue(one["metrics"]["ndvi"]["higher"])
        self.assertFalse(one["can_claim_sustained_improvement"])
