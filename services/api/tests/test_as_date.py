"""Tests for SQL-bind date coercion used by harvest_detect_for_land."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from app.core.date_utils import _as_date


class AsDateTests(unittest.TestCase):
    def test_iso_string_becomes_date(self) -> None:
        self.assertEqual(_as_date("2026-06-01"), date(2026, 6, 1))
        self.assertEqual(_as_date("2026-12-31"), date(2026, 12, 31))

    def test_iso_datetime_prefix(self) -> None:
        self.assertEqual(_as_date("2026-06-01T12:00:00"), date(2026, 6, 1))

    def test_date_and_datetime_passthrough(self) -> None:
        self.assertEqual(_as_date(date(2026, 6, 1)), date(2026, 6, 1))
        self.assertEqual(
            _as_date(datetime(2026, 6, 1, 15, 30)),
            date(2026, 6, 1),
        )

    def test_invalid_returns_none(self) -> None:
        self.assertIsNone(_as_date(None))
        self.assertIsNone(_as_date(""))
        self.assertIsNone(_as_date("   "))
        self.assertIsNone(_as_date("not-a-date"))
        self.assertIsNone(_as_date("2026-13-40"))


if __name__ == "__main__":
    unittest.main()
