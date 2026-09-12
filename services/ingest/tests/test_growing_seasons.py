"""Ingest-side smoke tests for growing season helpers."""

from __future__ import annotations

import unittest

from app.core.decloud import _months_from_window, normalize_season_months
from app.core.growing_seasons import normalize_growing_seasons


class GrowingSeasonsIngestTests(unittest.TestCase):
    def test_decloud_months_from_date_window(self) -> None:
        ms = _months_from_window(
            {"start_date": "2025-04-01", "end_date": "2025-08-15", "crops": ["corn"]}
        )
        self.assertEqual(ms, {4, 5, 6, 7, 8})

    def test_normalize_season_months_uses_date_windows(self) -> None:
        months = normalize_season_months(
            growing_seasons=[
                {"start_date": "2025-04-01", "end_date": "2025-08-31", "crops": ["corn"]},
            ],
            crop_type="corn",
        )
        self.assertEqual(months, (4, 5, 6, 7, 8))

    def test_custom_window_overrides_corn_default(self) -> None:
        # Without windows corn defaults to 6-9; with spring window → 4-8
        default = normalize_season_months(crop_type="corn")
        self.assertEqual(default, (6, 7, 8, 9))
        custom = normalize_season_months(
            growing_seasons=[{"start_month": 4, "end_month": 8, "crops": ["corn"]}],
            crop_type="corn",
        )
        self.assertEqual(custom, (4, 5, 6, 7, 8))

    def test_normalize_helper_legacy_crop(self) -> None:
        out = normalize_growing_seasons(
            [{"crop": "corn", "months": [6, 7, 8, 9]}], year=2025
        )
        self.assertEqual(out[0]["crops"], ["corn"])


if __name__ == "__main__":
    unittest.main()
