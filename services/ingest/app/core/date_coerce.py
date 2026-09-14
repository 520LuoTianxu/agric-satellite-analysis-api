"""Normalize SQL/driver date values without calling non-callable Row attrs."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable


def coerce_to_date(value: Any) -> date | None:
    """Normalize SQL/driver values to ``datetime.date`` (or None if unusable)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    date_attr = getattr(value, "date", None)
    if callable(date_attr):
        try:
            got = date_attr()
            if isinstance(got, date):
                return got
        except Exception:
            return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def dates_from_sql_rows(rows: Iterable[Any]) -> set[date]:
    """Collect dates from ``fetchall()`` rows (unpack column 0; never call Row.date).

    SQLAlchemy Row exposes column ``date`` as a non-callable attribute. A naive
    ``hasattr(row, "date")`` then ``row.date()`` raises
    ``TypeError: 'datetime.date' object is not callable``.
    """
    out: set[date] = set()
    for row in rows:
        d = row[0] if row is not None else None
        coerced = coerce_to_date(d)
        if coerced is not None:
            out.add(coerced)
    return out
