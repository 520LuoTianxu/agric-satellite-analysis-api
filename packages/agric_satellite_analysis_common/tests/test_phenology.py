"""验证春作、跨年冬作、轮作和缺景边界，不用固定日历制造结果。"""

import unittest
from datetime import date, timedelta

from agric_satellite_analysis_common.phenology import infer_phenology, window_months


def points(start="2025-03-01", values=None, step=10):
    values = values or [
        0.18,
        0.2,
        0.23,
        0.4,
        0.52,
        0.7,
        0.78,
        0.72,
        0.56,
        0.38,
        0.2,
        0.18,
    ]
    first = date.fromisoformat(start)
    return [
        {
            "date": (first + timedelta(days=i * step)).isoformat(),
            "ndvi": value,
            "official": True,
        }
        for i, value in enumerate(values)
    ]


class PhenologyTests(unittest.TestCase):
    def infer(self, source, end=date(2026, 12, 31)):
        return infer_phenology(source, start=date(2024, 1, 1), end=end)

    def test_spring_crop_is_not_forced_into_summer(self):
        result = self.infer(points())
        self.assertEqual(result["status"], "detected")
        self.assertLess(result["windows"][0]["start_date"], "2025-06-01")
        self.assertEqual(result["windows"][0]["status"], "complete")
        self.assertEqual(result["windows"][0]["confidence"], "medium")

    def test_cross_year_and_rotation(self):
        winter = self.infer(points("2024-11-01"))
        self.assertEqual(winter["windows"][0]["start_date"][:4], "2024")
        self.assertEqual(winter["windows"][0]["end_date"][:4], "2025")
        rotation = self.infer(points() + points("2025-07-01"))
        self.assertEqual(len(rotation["windows"]), 2)
        self.assertEqual(
            window_months([{"start_date": "2024-11-01", "end_date": "2025-02-15"}]),
            (1, 2, 11, 12),
        )

    def test_no_future_observations_and_open_end(self):
        source = points()
        cutoff = date.fromisoformat(source[7]["date"])
        full = self.infer(source, end=cutoff)
        clipped = self.infer(source[:8], end=cutoff)
        self.assertEqual(full, clipped)
        self.assertIsNone(full["windows"][0]["end_date"])
        self.assertEqual(full["windows"][0]["status"], "open_end")

    def test_sparse_cloudy_and_duplicate_dates_are_not_seasons(self):
        source = points()
        self.assertEqual(self.infer(source[:3] * 5)["status"], "insufficient_data")
        self.assertEqual(
            self.infer([{**p, "official": False} for p in source])["status"],
            "insufficient_data",
        )
        sparse = self.infer(points(step=50))
        self.assertFalse(sparse["windows"])

    def test_flat_green_series_is_not_declared_unplanted(self):
        result = self.infer(points(values=[0.7] * 12))
        self.assertEqual(result["status"], "no_distinct_cycle")
        self.assertFalse(result["windows"])

    def test_invalid_values_do_not_add_evidence(self):
        result = self.infer(points(values=[float("nan"), float("inf"), 9, -0.3]))
        self.assertEqual(result["observation_count"], 1)


if __name__ == "__main__":
    unittest.main()
