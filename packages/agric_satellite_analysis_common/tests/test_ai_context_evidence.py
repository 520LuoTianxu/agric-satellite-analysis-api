"""Compact autumn-emergence and 积温 evidence for LLM prompts."""

from __future__ import annotations

import unittest

from agric_satellite_analysis_common.autumn_emergence import (
    CLASS_LABELS_ZH,
    METHOD_VERSION,
    attach_daily_field,
    summary_for_model,
)
from agric_satellite_analysis_common.weather_gdd import (
    gdd_base_c,
    gdd_between,
    gdd_for_model,
    weather_period_for_model,
)


def _field(*, day: str = "2026-11-11", status: str = "emerging", pixels: bool = True) -> dict:
    field = {
        "kind": "autumn_emergence",
        "method_version": METHOD_VERSION,
        "season_key": "2026-autumn",
        "sowing_date": "2026-10-08",
        "as_of_date": day,
        "status": status,
        "status_zh": {"emerging": "出苗中", "emerged": "已出苗", "not_emerged": "未出苗"}[status],
        "emerged_flag": status == "emerged",
        "reason": "emerging_partial_or_unconfirmed",
        "baseline_dates": ["2026-10-20", "2026-10-28"],
        "obs_dates": ["2026-10-20", "2026-10-28", day],
        "n_clear": 3,
        "n_pixels": 100,
        "median_increment": 0.041,
        "share_above_noise": 0.62,
        "share_sustained": 0.31,
        "share_ndvi_ge_0_30": 0.02,
        "class_counts": {"0": 38, "1": 31, "2": 31},
        "class_labels": dict(CLASS_LABELS_ZH),
        "in_baseline": False,
        "median_ndvi": 0.19,
    }
    if pixels:
        field["pixels"] = {
            "lon": [115.0] * 4,
            "lat": [34.8] * 4,
            "class": [0, 1, 2, 2],
            "ndvi": [0.1, 0.2, 0.3, 0.3],
        }
    return field


class EmergenceForModelTests(unittest.TestCase):
    def test_summary_drops_pixel_arrays_and_keeps_shares(self) -> None:
        product = attach_daily_field({"pixel_data": {}}, _field())
        out = summary_for_model(daily_fields={ "2026-11-11": product["autumn_emergence"] })
        assert out is not None
        self.assertEqual(out["method_version"], METHOD_VERSION)
        self.assertEqual(out["source"], "algorithm")
        self.assertEqual(out["status"], "emerging")
        self.assertEqual(out["status_zh"], "出苗中")
        self.assertFalse(out["emerged_flag"])
        self.assertEqual(out["sowing_date"], "2026-10-08")
        self.assertEqual(out["first_emerged_date"] if "first_emerged_date" in out else None, None)
        self.assertEqual(out["class_shares"]["within_noise"], 0.38)
        self.assertEqual(out["class_shares"]["n_pixels"], 100)
        self.assertNotIn("pixels", out)
        self.assertIn("timeseries", out)
        self.assertNotIn("pixels", out["timeseries"][0])
        self.assertIn("不参与出苗判定", out["share_ndvi_ge_0_30_note"])
        self.assertIn("土壤本底", out["rule_zh"])

    def test_missing_emergence_returns_none(self) -> None:
        self.assertIsNone(summary_for_model())
        self.assertIsNone(summary_for_model(daily_fields={}, land={}))

    def test_land_flag_alone_is_enough_and_independent(self) -> None:
        out = summary_for_model(
            land={
                "autumn_emerged": True,
                "autumn_emerged_season": "2026-autumn",
                "autumn_emerged_date": "2026-11-08",
                "autumn_sowing_date": "2026-10-08",
            }
        )
        assert out is not None
        self.assertTrue(out["emerged_flag"])
        self.assertTrue(out["frozen"])
        self.assertEqual(out["status_zh"], "已出苗")
        self.assertEqual(out["first_emerged_date"], "2026-11-08")
        self.assertEqual(out["detail_source"], "land_flag")


class WeatherGddForModelTests(unittest.TestCase):
    def test_wheat_uses_zero_base_and_skips_missing_days(self) -> None:
        self.assertEqual(gdd_base_c("小麦"), 0.0)
        self.assertEqual(gdd_base_c("玉米"), 10.0)
        rows = [
            {"date": "2026-10-08", "min_c": 2.0, "max_c": 8.0, "precipitation_mm": 0.0},
            {"date": "2026-10-09", "min_c": -2.0, "max_c": 4.0, "precipitation_mm": 1.5},
            {"date": "2026-10-10", "min_c": None, "max_c": 9.0, "precipitation_mm": 0.0},
        ]
        window = gdd_between(rows, "2026-10-08", "2026-10-10", 0.0)
        assert window is not None
        self.assertEqual(window["sum"], "6.0")
        self.assertEqual(window["days"], 2)

    def test_gdd_without_sowing_still_returns_period(self) -> None:
        rows = [
            {
                "date": "2026-11-01",
                "temperature_2m_min": 4.0,
                "temperature_2m_max": 12.0,
                "precipitation_sum": 3.0,
            },
            {
                "date": "2026-11-02",
                "temperature_2m_min": 6.0,
                "temperature_2m_max": 10.0,
                "precipitation_sum": 0.0,
            },
        ]
        gdd = gdd_for_model(rows, start="2026-11-01", end="2026-11-02", crop_type="小麦")
        assert gdd is not None
        self.assertEqual(gdd["base_c"], 0)
        self.assertEqual(gdd["period"]["sum"], "16.0")
        self.assertNotIn("since_sowing", gdd)
        weather = weather_period_for_model(rows, start="2026-11-01", end="2026-11-02")
        assert weather is not None
        self.assertEqual(weather["precipitation_mm"], 3.0)
        self.assertEqual(weather["min_c"], 4.0)

    def test_missing_weather_returns_none(self) -> None:
        self.assertIsNone(weather_period_for_model([]))
        self.assertIsNone(gdd_for_model([], start="2026-11-01", end="2026-11-02"))


if __name__ == "__main__":
    unittest.main()
