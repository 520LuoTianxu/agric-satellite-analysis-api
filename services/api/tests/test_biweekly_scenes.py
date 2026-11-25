"""Biweekly optical eligibility from parcel_scene_products."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal

from agric_satellite_analysis_common.biweekly.scene_rows import to_radar_observation

from app.services.biweekly_scenes import (
    _eligible_optical,
    _to_observation,
    radar_baseline_start,
)


class EligibleOpticalTests(unittest.TestCase):
    def test_19994_in_window_s2_is_eligible(self) -> None:
        rows = [
            {
                "sensor": "S2",
                "date": date(2025, 12, 5),
                "parcel_cloud_cover_pct": 0.0,
                "cloud_cover": 0.562652,
                "ndvi_avg": 0.163057,
                "evi_avg": 0.116352,
                "ndre_avg": None,
                "ndmi_avg": -0.094621,
            },
            {
                "sensor": "S2",
                "date": date(2025, 12, 7),
                "parcel_cloud_cover_pct": Decimal("0.0"),
                "cloud_cover": Decimal("2.918007"),
                "ndvi_avg": Decimal("0.167545"),
                "evi_avg": Decimal("0.111034"),
                "ndre_avg": None,
                "ndmi_avg": Decimal("-0.105551"),
            },
            {
                "sensor": "s2",
                "date": datetime(2025, 12, 10, 0, 0, 0),
                "parcel_cloud_cover_pct": 0.0,
                "cloud_cover": 10.176522,
                "ndvi_avg": 0.144975,
                "evi_avg": 0.110558,
                "ndre_avg": None,
                "ndmi_avg": -0.12101,
            },
        ]
        for row in rows:
            self.assertTrue(_eligible_optical(row), msg=row["date"])
            obs = _to_observation({**row, "scene_id": "x"})
            self.assertEqual(obs["quality_status"], "eligible_descriptive")
            self.assertEqual(obs["sensor"], "S2")
            self.assertEqual(len(obs["date"]), 10)
            self.assertTrue(obs["date"].startswith("2025-12-"))
            self.assertIsInstance(obs["ndvi_avg"], float)
            self.assertGreater(obs["ndvi_avg"], 0)

    def test_high_parcel_cloud_is_excluded_even_if_metrics_exist(self) -> None:
        row = {
            "sensor": "S2",
            "date": date(2025, 12, 20),
            "parcel_cloud_cover_pct": 100.0,
            "cloud_cover": 17.158769,
            "ndvi_avg": 0.10064,
            "evi_avg": 0.099963,
            "ndre_avg": None,
            "ndmi_avg": 0.028775,
        }
        self.assertFalse(_eligible_optical(row))
        obs = _to_observation(row)
        self.assertEqual(obs["quality_status"], "excluded")
        # 未采用的原因和云量随观测下发，报告据此写「未采用：云量超过限值」。
        self.assertEqual(obs["exclusion_reason"], "cloud_over_limit")
        self.assertEqual(obs["parcel_cloud_cover_pct"], 100.0)
        self.assertAlmostEqual(obs["cloud_cover"], 17.158769)

    def test_eligible_row_carries_cloud_and_missing_cloud_stays_none(self) -> None:
        row = {
            "sensor": "S2",
            "date": date(2025, 12, 5),
            "parcel_cloud_cover_pct": None,
            "cloud_cover": Decimal("12.5"),
            "ndvi_avg": 0.163057,
            "evi_avg": 0.116352,
            "ndre_avg": None,
            "ndmi_avg": -0.094621,
        }
        obs = _to_observation(row)
        self.assertEqual(obs["quality_status"], "eligible_descriptive")
        self.assertIsNone(obs["exclusion_reason"])
        self.assertIsNone(obs["parcel_cloud_cover_pct"])
        self.assertEqual(obs["cloud_cover"], 12.5)
        self.assertIsNone(obs["ndre_avg"])

    def test_clear_scene_without_index_values_is_excluded_with_reason(self) -> None:
        row = {
            "sensor": "S2",
            "date": date(2025, 12, 6),
            "parcel_cloud_cover_pct": 2.0,
            "cloud_cover": 3.0,
            "ndvi_avg": None,
            "evi_avg": None,
            "ndre_avg": None,
            "ndmi_avg": None,
        }
        obs = _to_observation(row)
        self.assertEqual(obs["quality_status"], "excluded")
        self.assertEqual(obs["exclusion_reason"], "no_index_values")

    def test_radar_is_never_eligible_optical(self) -> None:
        row = {
            "sensor": "S1",
            "date": date(2025, 12, 2),
            "parcel_cloud_cover_pct": None,
            "cloud_cover": None,
            "ndvi_avg": None,
            "evi_avg": None,
            "ndre_avg": None,
            "ndmi_avg": None,
            "vv_avg": -11.5,
        }
        self.assertFalse(_eligible_optical(row))
        obs = _to_observation(row)
        self.assertEqual(obs["quality_status"], "excluded")
        self.assertEqual(obs["sensor"], "S1")
        self.assertIsNone(obs["exclusion_reason"])
        self.assertIsNone(obs["cloud_cover"])

    def test_radar_baseline_starts_ninety_days_before_the_period_end(self) -> None:
        self.assertEqual(radar_baseline_start("2026-05-20"), "2026-02-19")

    def test_radar_observation_keeps_orbit_and_backscatter(self) -> None:
        row = to_radar_observation(
            {
                "sensor": "S1",
                "date": date(2026, 5, 18),
                "scene_id": "S1A_example",
                "vv_avg": Decimal("-12.5"),
                "vh_avg": -18,
                "relative_orbit": "113",
                "platform": "sentinel-1a",
            }
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["date"], "2026-05-18")
        self.assertEqual(row["vv"], -12.5)
        self.assertEqual(row["vh"], -18.0)
        self.assertEqual(row["relative_orbit"], 113)
        self.assertEqual(row["platform"], "sentinel-1a")
        self.assertIsNone(to_radar_observation({"sensor": "S2", "date": "2026-05-18", "vv_avg": -1}))
