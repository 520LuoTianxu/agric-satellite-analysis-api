"""Unit tests for season-growth facts builder (synthetic series, no DB)."""

from __future__ import annotations

import unittest

from app.reports.season_growth.facts import (
    _drought_summary,
    _flood_summary,
    _peak,
    _series_mean,
    facts_for_llm,
)


def _s2_series():
    # Mid-season NDVI rise then mild drought-ish NDMI drop
    rows = []
    for d, ndvi, ndmi, official in [
        ("2026-06-05", 0.32, 0.18, True),
        ("2026-06-20", 0.45, 0.20, True),
        ("2026-07-05", 0.58, 0.22, True),
        ("2026-07-20", 0.66, 0.15, True),
        ("2026-08-05", 0.70, 0.05, True),
        ("2026-08-20", 0.55, -0.05, True),
        ("2026-09-05", 0.40, 0.02, True),
    ]:
        rows.append(
            {
                "date": d,
                "scene_id": f"S2-{d}",
                "ndvi_avg": ndvi,
                "ndmi_avg": ndmi,
                "official": official,
                "decloud_quality": "good",
                "cloud_cover": 5.0,
                "parcel_cloud_cover_pct": 5.0,
                "clear": True,
            }
        )
    return rows


class SeasonGrowthFactsTests(unittest.TestCase):
    def test_peak_and_mean(self) -> None:
        pts = [{"date": r["date"], "value": r["ndvi_avg"]} for r in _s2_series()]
        peak = _peak(pts)
        self.assertIsNotNone(peak)
        assert peak is not None
        self.assertEqual(peak["date"], "2026-08-05")
        self.assertAlmostEqual(peak["value"], 0.70, places=3)
        mean = _series_mean(pts)
        self.assertIsNotNone(mean)
        self.assertGreater(mean or 0, 0.4)

    def test_drought_summary_counts(self) -> None:
        drought = _drought_summary(_s2_series(), season_months=(6, 7, 8, 9))
        self.assertIn("counts", drought)
        self.assertIn("drought_scene_count", drought)
        self.assertIsInstance(drought["drought_scene_count"], int)
        # All official in-season → classifications produced
        self.assertGreater(sum(drought["counts"].values()), 0)

    def test_flood_summary_no_s1(self) -> None:
        flood = _flood_summary([])
        self.assertEqual(flood["status"], "no_s1_data")
        self.assertEqual(flood["scene_count"], 0)

    def test_flood_summary_with_vv(self) -> None:
        s1 = [
            {"date": "2026-07-01", "scene_id": "S1A_20260701", "vv_avg": -12.0, "vh_avg": -18.0, "relative_orbit": 10},
            {"date": "2026-07-13", "scene_id": "S1A_20260713", "vv_avg": -11.5, "vh_avg": -17.5, "relative_orbit": 10},
            {"date": "2026-07-25", "scene_id": "S1A_20260725", "vv_avg": -12.2, "vh_avg": -18.1, "relative_orbit": 10},
            {"date": "2026-08-06", "scene_id": "S1A_20260806", "vv_avg": -11.8, "vh_avg": -17.8, "relative_orbit": 10},
        ]
        flood = _flood_summary(s1)
        self.assertEqual(flood["status"], "ok")
        self.assertEqual(flood["scene_count"], 4)
        self.assertIsNotNone(flood["vv_median"])
        self.assertIn("counts", flood)

    def test_facts_for_llm_truncates(self) -> None:
        series = [{"date": f"2026-06-{(i % 28) + 1:02d}", "value": 0.4 + i * 0.001} for i in range(80)]
        facts = {
            "field": {"field_name": "测试"},
            "window": {"start_date": "2026-06-01", "end_date": "2026-09-30"},
            "ndvi": {"series": series, "mean": 0.5},
            "ndmi": {"series": series[:10]},
            "drought": {"days": [{"date": f"d{i}", "class": "mild"} for i in range(30)], "counts": {}},
            "flood": {},
            "harvest": {},
            "scenes": {},
        }
        compact = facts_for_llm(facts, max_series=40)
        self.assertTrue(compact["ndvi"]["series_truncated"])
        self.assertLessEqual(len(compact["ndvi"]["series"]), 40)
        self.assertLessEqual(len(compact["drought"]["days"]), 20)


if __name__ == "__main__":
    unittest.main()
