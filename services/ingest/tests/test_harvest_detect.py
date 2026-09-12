"""Synthetic NDVI series tests for harvest detection."""

from __future__ import annotations

import unittest

from app.core.harvest_detect import HarvestThresholds, detect_harvest


def _pts(series: list[tuple[str, float]], *, official: bool = True):
    return [
        {
            "date": d,
            "ndvi_avg": v,
            "scene_id": f"S2-{d}",
            "official": official,
            "decloud_quality": "good" if official else "bad",
        }
        for d, v in series
    ]


class HarvestDetectTests(unittest.TestCase):
    def test_detects_earliest_drop(self) -> None:
        series = [
            ("2025-06-01", 0.25),
            ("2025-06-20", 0.45),
            ("2025-07-05", 0.62),
            ("2025-07-20", 0.68),
            ("2025-08-05", 0.65),
            ("2025-08-25", 0.30),  # drop
            ("2025-09-10", 0.22),
        ]
        thr = HarvestThresholds(
            grow_min=0.35, drop_frac=0.35, lookback_k=3, confirm_m=1, min_clear_points=4
        )
        res = detect_harvest(
            _pts(series),
            window={"start_date": "2025-06-01", "end_date": "2025-09-30", "crops": ["corn"]},
            thresholds=thr,
        )
        self.assertEqual(res.status, "detected")
        self.assertEqual(res.harvest_date, "2025-08-25")
        self.assertEqual(res.scene_id, "S2-2025-08-25")
        self.assertIn(res.confidence, {"high", "medium", "low"})

    def test_ignores_bad_decloud_points(self) -> None:
        good = _pts(
            [
                ("2025-06-01", 0.4),
                ("2025-06-20", 0.55),
                ("2025-07-10", 0.7),
                ("2025-07-25", 0.68),
            ]
        )
        bad_drop = {
            "date": "2025-08-01",
            "ndvi_avg": 0.1,
            "scene_id": "BAD",
            "official": False,
            "decloud_quality": "bad",
        }
        after = _pts([("2025-08-20", 0.25), ("2025-09-05", 0.2)])
        thr = HarvestThresholds(
            grow_min=0.35, drop_frac=0.35, lookback_k=3, confirm_m=1, min_clear_points=4
        )
        res = detect_harvest(good + [bad_drop] + after, thresholds=thr)
        # Should not pick BAD fake drop as harvest if filtered; may be uncertain or later
        self.assertNotEqual(res.scene_id, "BAD")
        if res.harvest_date:
            self.assertNotEqual(res.harvest_date, "2025-08-01")

    def test_uncertain_too_few_points(self) -> None:
        res = detect_harvest(_pts([("2025-07-01", 0.5), ("2025-08-01", 0.2)]))
        self.assertEqual(res.status, "uncertain")
        self.assertIsNone(res.harvest_date)

    def test_no_growth(self) -> None:
        series = [(f"2025-06-{d:02d}", 0.15) for d in (1, 10, 20)] + [
            ("2025-07-01", 0.18),
            ("2025-07-15", 0.12),
        ]
        thr = HarvestThresholds(min_clear_points=4, grow_min=0.35)
        res = detect_harvest(_pts(series), thresholds=thr)
        self.assertEqual(res.status, "no_growth")
        self.assertIsNone(res.harvest_date)

    def test_no_interpolated_dates(self) -> None:
        series = [
            ("2025-06-01", 0.4),
            ("2025-06-20", 0.6),
            ("2025-07-10", 0.7),
            ("2025-07-30", 0.65),
            ("2025-09-01", 0.25),
            ("2025-09-20", 0.2),
        ]
        thr = HarvestThresholds(lookback_k=3, confirm_m=1, min_clear_points=4)
        res = detect_harvest(_pts(series), thresholds=thr)
        self.assertEqual(res.status, "detected")
        # Must be an actual scene date, not something between Jul 30 and Sep 1
        self.assertEqual(res.harvest_date, "2025-09-01")


if __name__ == "__main__":
    unittest.main()
