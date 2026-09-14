"""Unit tests for 图一 NDVI day-grade pixel aggregation."""

from __future__ import annotations

import unittest

from app.core.ndvi_day_grade import (
    classify_ndvi_day_grade,
    compute_pixel_ndvi_day_grade_shares,
)


class ClassifyNdviDayGradeTests(unittest.TestCase):
    def test_bands(self) -> None:
        self.assertEqual(classify_ndvi_day_grade(0.1), "红")
        self.assertEqual(classify_ndvi_day_grade(0.25), "橙")
        self.assertEqual(classify_ndvi_day_grade(0.34), "橙")
        self.assertEqual(classify_ndvi_day_grade(0.35), "黄")
        self.assertEqual(classify_ndvi_day_grade(0.49), "黄")
        self.assertEqual(classify_ndvi_day_grade(0.5), "绿")
        self.assertEqual(classify_ndvi_day_grade(0.8), "绿")


class ComputePixelSharesTests(unittest.TestCase):
    def test_prefer_clear_pixels(self) -> None:
        pixels = [
            {"lon": 1, "lat": 1, "clear": 0, "NDVI": 0.1},
            {"lon": 1, "lat": 2, "clear": 1, "NDVI": 0.6},
            {"lon": 1, "lat": 3, "clear": 1, "ndvi": 0.4},
        ]
        share = compute_pixel_ndvi_day_grade_shares(pixels)
        assert share is not None
        self.assertEqual(share["n"], 2)
        self.assertEqual(share["counts"]["绿"], 1)
        self.assertEqual(share["counts"]["黄"], 1)
        self.assertEqual(share["counts"]["红"], 0)
        self.assertAlmostEqual(share["mean"], 0.5)

    def test_all_unclear_when_no_clear_flag(self) -> None:
        pixels = [
            {"lon": 1, "lat": 1, "NDVI": 0.2},
            {"lon": 1, "lat": 2, "NDVI": 0.7},
        ]
        share = compute_pixel_ndvi_day_grade_shares(pixels)
        assert share is not None
        self.assertEqual(share["n"], 2)
        self.assertEqual(share["counts"]["红"], 1)
        self.assertEqual(share["counts"]["绿"], 1)
        self.assertEqual(share["pct"]["红"], 50.0)
        self.assertEqual(share["pct"]["绿"], 50.0)

    def test_empty(self) -> None:
        self.assertIsNone(compute_pixel_ndvi_day_grade_shares([]))
        self.assertIsNone(compute_pixel_ndvi_day_grade_shares(None))


if __name__ == "__main__":
    unittest.main()
