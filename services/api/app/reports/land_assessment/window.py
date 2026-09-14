# -*- coding: utf-8 -*-
"""Pure helpers for 选地体检 historical date window (no FastAPI deps)."""

from __future__ import annotations

from datetime import date


def resolve_assessment_window(
    *,
    date_from: str | None = None,
    years: int | None = None,
    today: date | None = None,
) -> tuple[str, str, int, int]:
    """Compute (date_from, date_to, weather_days, years_used) for pulls + scoring.

    Prefer explicit ``date_from``; otherwise subtract ``years`` calendar years
    from today (default 3). ``date_to`` is always today.
    """
    end = today or date.today()
    years_used = 3 if years is None else int(years)
    if years_used < 1:
        years_used = 1
    if years_used > 20:
        years_used = 20

    start: date
    if date_from:
        try:
            start = date.fromisoformat(str(date_from)[:10])
        except ValueError as e:
            raise ValueError(f"invalid date_from: {date_from}") from e
        delta_days = max(0, (end - start).days)
        years_used = max(1, min(20, round(delta_days / 365) or 1))
    else:
        try:
            start = end.replace(year=end.year - years_used)
        except ValueError:
            # Feb 29 → Feb 28
            start = end.replace(year=end.year - years_used, day=28)

    if start > end:
        start = end
    weather_days = max(1, (end - start).days)
    return start.isoformat(), end.isoformat(), weather_days, years_used


__all__ = ["resolve_assessment_window"]
