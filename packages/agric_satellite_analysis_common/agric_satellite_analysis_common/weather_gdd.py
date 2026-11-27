"""Crop-aware accumulated temperature (积温) and compact weather for LLM prompts.

Weather_daily.gdd_daily / gdd_cumulative are stored with the task default of
10°C. Autumn wheat days often sit below that, so those stored totals would
read as zero. Report and AI paths recompute from daily min/max: wheat uses
0°C, maize and other crops use 10°C. Missing days are skipped, not treated
as zero. These numbers are for model context only and are not written back
to weather_daily.
"""

from __future__ import annotations

from datetime import date
from typing import Any

WHEAT_TOKENS = ("小麦", "wheat")
MAIZE_TOKENS = ("玉米", "maize", "corn")
WHEAT_STAGES = {
    "播种期",
    "出苗期",
    "分蘖期",
    "越冬期",
    "返青期",
    "拔节孕穗期",
    "抽穗期",
    "灌浆期",
    "成熟期",
}
HEAT_STRESS_C = 33.0


def iso_day(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) < 10 or text[4] != "-" or text[7] != "-":
        return None
    try:
        date.fromisoformat(text[:10])
    except ValueError:
        return None
    return text[:10]


def gdd_base_c(crop_type: str | None = None, stage_names: list[str] | None = None) -> float:
    """Wheat emergence uses 0°C. Maize and other crops use the weather-task 10°C."""
    crop = str(crop_type or "").lower()
    if any(token in crop for token in MAIZE_TOKENS):
        return 10.0
    if any(token in crop for token in WHEAT_TOKENS):
        return 0.0
    if any(name in WHEAT_STAGES for name in (stage_names or [])):
        return 0.0
    return 10.0


def daily_minmax_c(row: dict[str, Any]) -> tuple[float, float] | None:
    """Read min/max °C from report rows (min_c) or weather_daily columns."""
    if not isinstance(row, dict):
        return None
    minimum = row.get("min_c")
    maximum = row.get("max_c")
    if minimum is None:
        minimum = row.get("temperature_2m_min")
    if maximum is None:
        maximum = row.get("temperature_2m_max")
    if minimum is None or maximum is None:
        return None
    try:
        lo = float(minimum)
        hi = float(maximum)
    except (TypeError, ValueError):
        return None
    if lo != lo or hi != hi:
        return None
    return lo, hi


def daily_gdd(row: dict[str, Any], base: float) -> float | None:
    pair = daily_minmax_c(row)
    if pair is None:
        return None
    mean = (pair[0] + pair[1]) / 2.0
    return max(0.0, mean - base)


def gdd_between(
    rows: list[Any] | None,
    start: str | None,
    end: str | None,
    base: float,
) -> dict[str, Any] | None:
    start_day = iso_day(start)
    end_day = iso_day(end)
    if not start_day or not end_day:
        return None
    total = 0.0
    days = 0
    first: str | None = None
    last: str | None = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day = iso_day(row.get("date"))
        if not day or day < start_day or day > end_day:
            continue
        value = daily_gdd(row, base)
        if value is None:
            continue
        total += value
        days += 1
        if first is None or day < first:
            first = day
        if last is None or day > last:
            last = day
    if days == 0 or first is None or last is None:
        return None
    return {
        "sum": f"{total:.1f}",
        "days": days,
        "from": first,
        "to": last,
    }


def gdd_for_model(
    rows: list[Any] | None,
    *,
    start: str | None,
    end: str | None,
    crop_type: str | None = None,
    stage_names: list[str] | None = None,
    sowing_date: str | None = None,
    latest: str | None = None,
) -> dict[str, Any] | None:
    """Window 积温, plus since-sowing when a sowing date is known.

    Either part can stand alone. Returns None only when both are missing.
    """
    base = gdd_base_c(crop_type, stage_names)
    period = gdd_between(rows, start, end, base) if start and end else None
    since = None
    since_end = iso_day(latest) or iso_day(end)
    sown = iso_day(sowing_date)
    if sown and since_end:
        since = gdd_between(rows, sown, since_end, base)
    if not period and not since:
        return None
    body: dict[str, Any] = {"base_c": int(base)}
    if period:
        body["period"] = period
    if since and sown:
        body["since_sowing"] = {
            **since,
            "covers_sowing_day": since["from"] <= sown,
        }
    return body


def _num(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:
            return number
    return None


def weather_period_for_model(
    rows: list[Any] | None,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any] | None:
    """Compact period weather. Returns None when there is no temperature or rain."""
    start_day = iso_day(start)
    end_day = iso_day(end)
    mins: list[float] = []
    maxs: list[float] = []
    means: list[float] = []
    precip = 0.0
    precip_days = 0
    frost = 0
    heat = 0
    first: str | None = None
    last: str | None = None
    used = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day = iso_day(row.get("date"))
        if not day:
            continue
        if start_day and day < start_day:
            continue
        if end_day and day > end_day:
            continue
        pair = daily_minmax_c(row)
        mean = _num(row, "mean_c", "temperature_2m_mean")
        if mean is None and pair is not None:
            mean = (pair[0] + pair[1]) / 2.0
        rain = _num(row, "precipitation_mm", "precipitation_sum")
        if pair is None and mean is None and rain is None:
            continue
        used += 1
        if first is None or day < first:
            first = day
        if last is None or day > last:
            last = day
        if pair is not None:
            mins.append(pair[0])
            maxs.append(pair[1])
            if pair[0] <= 0:
                frost += 1
            if pair[1] >= HEAT_STRESS_C:
                heat += 1
        if mean is not None:
            means.append(mean)
        if rain is not None:
            precip += rain
            if rain > 0:
                precip_days += 1
    if used == 0:
        return None
    body: dict[str, Any] = {
        "from": first,
        "to": last,
        "days": used,
    }
    if mins:
        body["min_c"] = round(min(mins), 1)
    if maxs:
        body["max_c"] = round(max(maxs), 1)
    if means:
        body["mean_c"] = round(sum(means) / len(means), 1)
    if any(
        isinstance(row, dict)
        and _num(row, "precipitation_mm", "precipitation_sum") is not None
        for row in (rows or [])
    ):
        body["precipitation_mm"] = round(precip, 1)
        body["precipitation_days"] = precip_days
    if mins:
        body["frost_days"] = frost
    if maxs:
        body["heat_stress_days"] = heat
    return body
