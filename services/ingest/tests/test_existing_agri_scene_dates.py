"""Stdlib tests for agri scene-date coercion (Row unpack / date-not-callable)."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from app.core.date_coerce import coerce_to_date, dates_from_sql_rows


class _FakeRow:
    """Mimics SQLAlchemy Row: indexable, exposes column attr ``date`` as a value."""

    def __init__(self, value):
        self.date = value

    def __getitem__(self, idx: int):
        if idx != 0:
            raise IndexError(idx)
        return self.date


class CoerceToDateTests(unittest.TestCase):
    def test_datetime(self) -> None:
        self.assertEqual(
            coerce_to_date(datetime(2024, 6, 15, 12, 30)),
            date(2024, 6, 15),
        )

    def test_date_passthrough(self) -> None:
        d = date(2024, 1, 2)
        self.assertIs(coerce_to_date(d), d)

    def test_isoformat_string(self) -> None:
        self.assertEqual(coerce_to_date("2024-03-04"), date(2024, 3, 4))
        self.assertEqual(coerce_to_date("2024-03-04T12:00:00"), date(2024, 3, 4))

    def test_bad_string_skipped(self) -> None:
        self.assertIsNone(coerce_to_date("not-a-date"))

    def test_none(self) -> None:
        self.assertIsNone(coerce_to_date(None))


class DatesFromSqlRowsTests(unittest.TestCase):
    def test_rows_with_date_column_do_not_call_date(self) -> None:
        """Regression: Row.date is a date value; must not invoke it as a method."""
        rows = [
            _FakeRow(date(2024, 5, 1)),
            _FakeRow(datetime(2024, 5, 2, 8, 0)),
            _FakeRow("2024-05-03"),
            _FakeRow(None),
        ]
        got = dates_from_sql_rows(rows)
        self.assertEqual(
            got,
            {date(2024, 5, 1), date(2024, 5, 2), date(2024, 5, 3)},
        )

    def test_legacy_hasattr_date_call_would_raise(self) -> None:
        row = _FakeRow(date(2024, 7, 1))
        self.assertFalse(isinstance(row, date))
        self.assertTrue(hasattr(row, "date"))
        with self.assertRaises(TypeError):
            row.date()  # type: ignore[operator]


if __name__ == "__main__":
    unittest.main()
