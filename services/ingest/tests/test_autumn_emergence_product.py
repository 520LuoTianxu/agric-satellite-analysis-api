"""Download-machine daily emergence attach and freeze/backfill behaviour."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from agric_satellite_analysis_common.autumn_emergence import (
    attach_daily_field,
)
from app.tasks.autumn_emergence_product import (
    _compute_window,
    should_compute,
)


class ShouldComputeTests(unittest.TestCase):
    def test_automatic_skips_empty_and_non_wheat(self) -> None:
        day = date(2026, 11, 14)
        self.assertFalse(
            should_compute(
                day=day,
                crop_type="",
                manual=False,
                frozen_season=None,
                emerged=False,
                sowing_date=None,
            )
        )
        self.assertFalse(
            should_compute(
                day=day,
                crop_type="玉米",
                manual=False,
                frozen_season=None,
                emerged=False,
                sowing_date=None,
            )
        )
        self.assertTrue(
            should_compute(
                day=day,
                crop_type="冬小麦",
                manual=False,
                frozen_season=None,
                emerged=False,
                sowing_date=None,
            )
        )

    def test_manual_allows_empty_crop_and_frozen_stops(self) -> None:
        day = date(2026, 11, 14)
        self.assertTrue(
            should_compute(
                day=day,
                crop_type="",
                manual=True,
                frozen_season=None,
                emerged=False,
                sowing_date=date(2026, 10, 20),
            )
        )
        self.assertFalse(
            should_compute(
                day=day,
                crop_type="冬小麦",
                manual=True,
                frozen_season="2026-autumn",
                emerged=True,
                sowing_date=date(2026, 10, 20),
            )
        )


class WindowComputeTests(unittest.TestCase):
    def test_backfill_earlier_date_recomputes_later_unfrozen(self) -> None:
        import numpy as np
        from agric_satellite_analysis_common.autumn_emergence import DateStack as DS

        def stack(day: str, level: float, n: int = 40):
            values = [0.11 + (i % 5) * 0.004 + level for i in range(n)]
            return DS(
                day=date.fromisoformat(day),
                lon=np.array([115.0 + i * 1e-4 for i in range(n)]),
                lat=np.full(n, 34.82),
                ndvi=np.array(values, dtype=float),
            )

        # Backfill inserts the missing 11-02 baseline date; later unfrozen dates recompute.
        days = {
            "2026-10-28": _product("2026-10-28", 0.0),
            "2026-10-30": _product("2026-10-30", 0.0),
            "2026-11-02": _product("2026-11-02", 0.0),
            "2026-11-14": _product("2026-11-14", 0.09),
            "2026-11-24": _product("2026-11-24", 0.12),
        }
        # Official filter needs source/cloud fields; official_stack_from_product
        # is used inside _compute_window. Patch it to use our synthetic stacks.
        stacks = {
            "2026-10-28": stack("2026-10-28", 0.0),
            "2026-10-30": stack("2026-10-30", 0.0),
            "2026-11-02": stack("2026-11-02", 0.0),
            "2026-11-14": stack("2026-11-14", 0.09),
            "2026-11-24": stack("2026-11-24", 0.12),
        }

        def fake_official(product):
            day = str(product.get("date"))[:10]
            return stacks[day], 40

        with patch(
            "app.tasks.autumn_emergence_product.official_stack_from_product",
            fake_official,
        ):
            changed = _compute_window(
                "33052",
                days,
                sowing=date(2026, 10, 20),
            )
        self.assertIn("2026-11-14", changed)
        self.assertIn("2026-11-24", changed)
        field_14 = days["2026-11-14"]["pixel_data"]["autumn_emergence"]
        field_24 = days["2026-11-24"]["pixel_data"]["autumn_emergence"]
        self.assertEqual(field_14["status"], "emerging")
        self.assertFalse(field_14["emerged_flag"])
        self.assertEqual(field_24["status"], "emerged")
        self.assertTrue(field_24["emerged_flag"])
        # Stop after emerged: no extra dates after 11-24 in this set.
        self.assertEqual(field_24["as_of_date"], "2026-11-24")
        self.assertIn("2026-11-02", field_24["obs_dates"])


def _product(day: str, level: float) -> dict:
    return {
        "land_id": "33052",
        "date": day,
        "sensor": "S2",
        "scene_id": f"S2-{day}",
        "source": "stac_direct",
        "cloud_cover": 0,
        "cloud_cover_over_30": False,
        "parcel_cloud_cover_pct": 0,
        "pixel_data": {"source": "stac_direct", "format": "lonlat_v1"},
        "level": level,
    }


if __name__ == "__main__":
    unittest.main()
