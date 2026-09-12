"""Small date helpers for SQL binds (asyncpg expects datetime.date)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def _as_date(value: Any) -> date | None:
    """Coerce ISO string / date / datetime → ``date`` for SQL params.

    Mirrors ``openfarm_common.harvest_detect._parse_date``. Returns None when
    the value cannot be parsed so callers can skip the bound cleanly.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None
