"""Unit tests for assessment date window (stdlib only)."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

REPORTS = Path(__file__).resolve().parents[1] / "app" / "reports" / "land_assessment"
sys.path.insert(0, str(REPORTS))

from window import resolve_assessment_window  # noqa: E402


class ResolveAssessmentWindowTests(unittest.TestCase):
    def test_default_three_years(self) -> None:
        today = date(2026, 9, 14)
        df, dt, days, years = resolve_assessment_window(today=today)
        self.assertEqual(df, "2023-09-14")
        self.assertEqual(dt, "2026-09-14")
        self.assertEqual(years, 3)
        self.assertEqual(days, (today - date(2023, 9, 14)).days)

    def test_explicit_years(self) -> None:
        today = date(2026, 9, 14)
        df, dt, days, years = resolve_assessment_window(years=5, today=today)
        self.assertEqual(df, "2021-09-14")
        self.assertEqual(dt, "2026-09-14")
        self.assertEqual(years, 5)
        self.assertGreater(days, 1800)

    def test_date_from_overrides_years(self) -> None:
        today = date(2026, 9, 14)
        df, dt, days, years = resolve_assessment_window(
            date_from="2024-01-01", years=5, today=today
        )
        self.assertEqual(df, "2024-01-01")
        self.assertEqual(dt, "2026-09-14")
        self.assertEqual(days, (today - date(2024, 1, 1)).days)
        self.assertGreaterEqual(years, 2)

    def test_feb29_safe(self) -> None:
        today = date(2024, 2, 29)
        df, dt, days, years = resolve_assessment_window(years=1, today=today)
        self.assertEqual(df, "2023-02-28")
        self.assertEqual(dt, "2024-02-29")
        self.assertEqual(years, 1)
        self.assertGreater(days, 0)

    def test_invalid_date_from(self) -> None:
        with self.assertRaises(ValueError):
            resolve_assessment_window(date_from="not-a-date", today=date(2026, 9, 14))


if __name__ == "__main__":
    unittest.main()
