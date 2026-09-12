"""Normalize growing-season windows for RS pull / decloud / harvest.

Final window shape:
  {start_date, end_date, crops: [1..2 keys], label?}

Accepts legacy months[] / start_month-end_month / crop (singular).
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime
from typing import Any

MAX_CROPS_PER_WINDOW = 2
MAX_DISTINCT_CROPS = 2


def _parse_iso_date(value: Any) -> date | None:
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


def _clamp_month(m: Any) -> int | None:
    try:
        mi = int(m)
    except (TypeError, ValueError):
        return None
    if 1 <= mi <= 12:
        return mi
    return None


def _months_from_range(sm: int, em: int) -> list[int]:
    if sm <= em:
        return list(range(sm, em + 1))
    return list(range(sm, 13)) + list(range(1, em + 1))


def months_from_window(window: dict[str, Any] | None) -> set[int]:
    """Extract calendar months covered by one raw or normalized window."""
    if not isinstance(window, dict):
        return set()
    out: set[int] = set()
    for m in window.get("months") or []:
        mi = _clamp_month(m)
        if mi is not None:
            out.add(mi)
    sm, em = _clamp_month(window.get("start_month")), _clamp_month(window.get("end_month"))
    if sm is not None and em is not None:
        out.update(_months_from_range(sm, em))
    sd = _parse_iso_date(window.get("start_date"))
    ed = _parse_iso_date(window.get("end_date"))
    if sd and ed:
        cur = date(sd.year, sd.month, 1)
        end_m = date(ed.year, ed.month, 1)
        # Cap iteration to avoid runaway on bad data
        for _ in range(36):
            out.add(cur.month)
            if cur >= end_m:
                break
            if cur.month == 12:
                cur = date(cur.year + 1, 1, 1)
            else:
                cur = date(cur.year, cur.month + 1, 1)
    elif sd:
        out.add(sd.month)
    elif ed:
        out.add(ed.month)
    return out


def _normalize_crops(window: dict[str, Any]) -> list[str]:
    crops: list[str] = []
    raw_list = window.get("crops")
    if isinstance(raw_list, list):
        for c in raw_list:
            s = str(c).strip() if c is not None else ""
            if s and s not in crops:
                crops.append(s)
    singular = window.get("crop")
    if singular is not None:
        s = str(singular).strip()
        if s and s not in crops:
            crops.append(s)
    return crops


def _dates_from_months(
    months: list[int],
    *,
    year: int,
) -> tuple[date, date] | None:
    if not months:
        return None
    months_u = sorted({m for m in months if 1 <= m <= 12})
    if not months_u:
        return None
    # Contiguous? else use min..max in calendar order within year (wrap if needed)
    if months_u[-1] - months_u[0] + 1 == len(months_u):
        sm, em = months_u[0], months_u[-1]
        start = date(year, sm, 1)
        end = date(year, em, monthrange(year, em)[1])
        return start, end
    # Wrap (e.g. 10,11,12,1,2,3): start at first gap-crossing month
    # Prefer starting at the highest run that includes months > mid-year
    high = [m for m in months_u if m >= 8]
    if high and any(m <= 6 for m in months_u):
        sm = min(high)
        em = max(m for m in months_u if m <= 6)
        start = date(year, sm, 1)
        end_year = year + 1
        end = date(end_year, em, monthrange(end_year, em)[1])
        return start, end
    sm, em = months_u[0], months_u[-1]
    start = date(year, sm, 1)
    end = date(year, em, monthrange(year, em)[1])
    return start, end


def _resolve_date_bounds(
    window: dict[str, Any],
    *,
    year: int,
) -> tuple[date, date] | None:
    sd = _parse_iso_date(window.get("start_date"))
    ed = _parse_iso_date(window.get("end_date"))
    if sd and ed:
        if ed < sd:
            sd, ed = ed, sd
        return sd, ed
    months: list[int] = []
    for m in window.get("months") or []:
        mi = _clamp_month(m)
        if mi is not None:
            months.append(mi)
    sm, em = _clamp_month(window.get("start_month")), _clamp_month(window.get("end_month"))
    if sm is not None and em is not None:
        months.extend(_months_from_range(sm, em))
    if sd and not ed:
        months.append(sd.month)
    if ed and not sd:
        months.append(ed.month)
    return _dates_from_months(months, year=year)


def validate_growing_seasons_crops(
    windows: list[dict[str, Any]],
    *,
    max_per_window: int = MAX_CROPS_PER_WINDOW,
    max_distinct: int = MAX_DISTINCT_CROPS,
) -> None:
    """Raise ValueError if crop limits exceeded."""
    distinct: set[str] = set()
    for i, w in enumerate(windows):
        crops = list(w.get("crops") or [])
        if len(crops) > max_per_window:
            raise ValueError(
                f"growing_seasons[{i}] has {len(crops)} crops; max {max_per_window} per window"
            )
        distinct.update(crops)
    if len(distinct) > max_distinct:
        raise ValueError(
            f"growing_seasons uses {len(distinct)} distinct crops; max {max_distinct}"
        )


def normalize_growing_seasons(
    raw: list[Any] | None,
    *,
    year: int | None = None,
    validate_crop_limits: bool = True,
) -> list[dict[str, Any]]:
    """Normalize raw windows → ``[{start_date, end_date, crops, label?}, ...]``.

    Skips windows that cannot resolve to a date range. Empty input → [].
    """
    if not raw:
        return []
    y = int(year) if year else date.today().year
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        bounds = _resolve_date_bounds(item, year=y)
        if not bounds:
            continue
        start, end = bounds
        crops = _normalize_crops(item)
        entry: dict[str, Any] = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "crops": crops,
        }
        label = item.get("label")
        if label is not None and str(label).strip():
            entry["label"] = str(label).strip()
        # Keep months for consumers that still union months
        months = sorted(months_from_window({**item, **entry}))
        if months:
            entry["months"] = months
            entry["start_month"] = months[0]
            entry["end_month"] = months[-1] if months[0] <= months[-1] else months[-1]
            # For wrap, start_month/end_month from original if present
            osm = _clamp_month(item.get("start_month"))
            oem = _clamp_month(item.get("end_month"))
            if osm is not None and oem is not None:
                entry["start_month"] = osm
                entry["end_month"] = oem
            elif start.month != end.month or start.year != end.year:
                entry["start_month"] = start.month
                entry["end_month"] = end.month
        out.append(entry)
    if validate_crop_limits:
        validate_growing_seasons_crops(out)
    return out


def union_season_months(windows: list[dict[str, Any]] | None) -> tuple[int, ...]:
    """Union of months across windows (for decloud / high-cloud filters)."""
    months: set[int] = set()
    for w in windows or []:
        months |= months_from_window(w)
    return tuple(sorted(months))
