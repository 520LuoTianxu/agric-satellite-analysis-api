"""Tests for growing season window normalization."""

from __future__ import annotations

import unittest

from openfarm_common.growing_seasons import (
    MAX_WINDOWS,
    months_from_window,
    normalize_growing_seasons,
    union_season_months,
    validate_growing_seasons_crops,
)


class NormalizeGrowingSeasonsTests(unittest.TestCase):
    def test_start_end_dates(self) -> None:
        out = normalize_growing_seasons(
            [{"start_date": "2025-04-01", "end_date": "2025-08-31", "crops": ["corn"], "label": "春玉米"}]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["start_date"], "2025-04-01")
        self.assertEqual(out[0]["end_date"], "2025-08-31")
        self.assertEqual(out[0]["crops"], ["corn"])
        self.assertEqual(out[0]["label"], "春玉米")
        self.assertEqual(set(out[0]["months"]), {4, 5, 6, 7, 8})

    def test_legacy_crop_to_crops(self) -> None:
        out = normalize_growing_seasons(
            [{"crop": "wheat", "months": [3, 4, 5, 6]}],
            year=2025,
        )
        self.assertEqual(out[0]["crops"], ["wheat"])
        self.assertEqual(out[0]["start_date"], "2025-03-01")
        self.assertEqual(out[0]["end_date"], "2025-06-30")

    def test_start_end_month(self) -> None:
        out = normalize_growing_seasons(
            [{"start_month": 6, "end_month": 9, "crops": ["corn"]}],
            year=2024,
        )
        self.assertEqual(out[0]["start_date"], "2024-06-01")
        self.assertEqual(out[0]["end_date"], "2024-09-30")

    def test_intercrop_two_crops(self) -> None:
        out = normalize_growing_seasons(
            [
                {
                    "start_date": "2025-06-01",
                    "end_date": "2025-09-30",
                    "crops": ["rice", "soybean"],
                    "label": "米豆间作",
                }
            ]
        )
        self.assertEqual(out[0]["crops"], ["rice", "soybean"])

    def test_max_two_crops_per_window(self) -> None:
        with self.assertRaises(ValueError):
            normalize_growing_seasons(
                [
                    {
                        "start_date": "2025-01-01",
                        "end_date": "2025-06-30",
                        "crops": ["a", "b", "c"],
                    }
                ],
            )

    def test_three_crops_in_window_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_growing_seasons(
                [
                    {
                        "start_date": "2025-01-01",
                        "end_date": "2025-06-30",
                        "crops": ["rice", "bean", "corn"],
                    }
                ],
            )

    def test_max_distinct_across_list(self) -> None:
        with self.assertRaises(ValueError):
            normalize_growing_seasons(
                [
                    {"start_date": "2025-03-01", "end_date": "2025-05-31", "crops": ["wheat"]},
                    {"start_date": "2025-06-01", "end_date": "2025-09-30", "crops": ["corn"]},
                    {"start_date": "2025-10-01", "end_date": "2025-11-30", "crops": ["rice"]},
                ]
            )

    def test_rotation_two_windows_ok(self) -> None:
        out = normalize_growing_seasons(
            [
                {"start_date": "2025-03-01", "end_date": "2025-05-31", "crops": ["wheat"]},
                {"start_date": "2025-06-01", "end_date": "2025-09-30", "crops": ["corn"]},
            ]
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(union_season_months(out), (3, 4, 5, 6, 7, 8, 9))

    def test_months_from_window_dates(self) -> None:
        ms = months_from_window({"start_date": "2025-04-15", "end_date": "2025-08-10"})
        self.assertEqual(ms, {4, 5, 6, 7, 8})


class ValidateCropsTests(unittest.TestCase):
    def test_validate_ok(self) -> None:
        validate_growing_seasons_crops([{"crops": ["corn", "soybean"]}])



class MaxWindowsTests(unittest.TestCase):
    def test_max_three_windows(self) -> None:
        self.assertEqual(MAX_WINDOWS, 3)
        raw = [
            {"start_date": f"2025-{m:02d}-01", "end_date": f"2025-{m:02d}-28", "crops": ["corn"]}
            for m in (4, 6, 8, 10)
        ]
        with self.assertRaises(ValueError):
            normalize_growing_seasons(raw)
        ok = normalize_growing_seasons(raw[:3])
        self.assertEqual(len(ok), 3)


if __name__ == "__main__":
    unittest.main()
