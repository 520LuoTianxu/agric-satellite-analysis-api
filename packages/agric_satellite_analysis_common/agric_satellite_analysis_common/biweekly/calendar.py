"""14-day half-open biweekly periods, Asia/Shanghai calendar days."""

from __future__ import annotations

from datetime import date, timedelta

from .models import ReportError

CADENCE_DAYS = 14
# Safety cap for a single backfill request: current period + prior comparison
# window. Never approach the production default of three calendar years.
MAX_BACKFILL_DAYS = 42


def iso(day: date) -> str:
    return day.isoformat()


def parse_day(value: str) -> date:
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise ReportError("DATE_INVALID") from exc
    if day.isoformat() != value:
        raise ReportError("DATE_INVALID")
    return day


def add_days(value: str, count: int) -> str:
    return iso(parse_day(value) + timedelta(days=count))


def planting_season_label(start: str, end_inclusive: str) -> str:
    a, b = parse_day(start), parse_day(end_inclusive)
    if a > b:
        raise ReportError("DATE_INVALID")
    if a.year == b.year:
        return str(a.year)
    return f"{a.year}/{str(b.year)[2:]}"


def iter_periods(start: str, end_inclusive: str, cadence: int = CADENCE_DAYS) -> list[dict]:
    """Inclusive season end, internal intervals [start, next_start).

    The last period may be shorter than cadence days. Short-window weather
    totals must not be compared directly with a full 14-day period.
    """
    if cadence != CADENCE_DAYS:
        raise ReportError("CADENCE_UNSUPPORTED")
    cursor = parse_day(start)
    close = parse_day(end_inclusive) + timedelta(days=1)
    if cursor >= close:
        raise ReportError("DATE_INVALID")
    periods = []
    index = 1
    while cursor < close:
        nxt = min(cursor + timedelta(days=cadence), close)
        periods.append(
            {
                "period_id": str(index),
                "issue": index,
                "start": iso(cursor),
                "end_exclusive": iso(nxt),
                "end_inclusive": iso(nxt - timedelta(days=1)),
                "days": (nxt - cursor).days,
                "partial": (nxt - cursor).days != cadence,
            }
        )
        cursor = nxt
        index += 1
    return periods


def prior_period(periods: list[dict], period_id: str) -> dict | None:
    ids = [row["period_id"] for row in periods]
    try:
        index = ids.index(str(period_id))
    except ValueError as exc:
        raise ReportError("PERIOD_NOT_FOUND") from exc
    return periods[index - 1] if index else None


def bounded_backfill_window(periods: list[dict], period_id: str) -> dict:
    """Current issue plus the previous comparison window only."""
    ids = {row["period_id"]: row for row in periods}
    current = ids.get(str(period_id))
    if current is None:
        raise ReportError("PERIOD_NOT_FOUND")
    previous = prior_period(periods, period_id)
    date_from = previous["start"] if previous else current["start"]
    date_to_inclusive = current["end_inclusive"]
    span = (parse_day(current["end_exclusive"]) - parse_day(date_from)).days
    if span > MAX_BACKFILL_DAYS or span <= 0:
        raise ReportError("BACKFILL_WINDOW_UNBOUNDED")
    # Guard against accidental three-year default (about 1095 days).
    if span >= 365:
        raise ReportError("BACKFILL_WINDOW_UNBOUNDED")
    return {
        "land_scope": "single_land",
        "date_from": date_from,
        "date_to_inclusive": date_to_inclusive,
        "end_exclusive": current["end_exclusive"],
        "span_days": span,
        "includes_prior_comparison_window": previous is not None,
        "prior_period_id": previous["period_id"] if previous else None,
        "current_period_id": current["period_id"],
        "force": False,
        "years": None,
        "months": None,
        "note": "有界补数：仅本期及上一对照窗。禁止默认三年回填。",
    }
