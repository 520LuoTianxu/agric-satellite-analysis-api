"""天气历史窗口和定时任务窗口的边界测试。"""

from datetime import date

import pytest

from agric_satellite_analysis_common.scheduled_land_filter import scheduled_date_window
from agric_satellite_analysis_common.weather_window import (
    resolve_historical_weather_window,
)


def test_explicit_window_ends_at_yesterday_when_date_to_is_today():
    window = resolve_historical_weather_window(
        today=date(2026, 9, 22),
        date_from="2024-01-01",
        date_to="2026-09-22",
    )

    assert window.start == date(2024, 1, 1)
    assert window.end == date(2026, 9, 21)


def test_years_window_uses_calendar_years():
    window = resolve_historical_weather_window(
        today=date(2026, 9, 22),
        years=2,
    )

    assert window.start == date(2024, 9, 22)
    assert window.end == date(2026, 9, 21)


def test_days_window_is_inclusive():
    window = resolve_historical_weather_window(
        today=date(2026, 9, 22),
        days=3,
    )

    assert (window.start, window.end, window.days) == (
        date(2026, 9, 19),
        date(2026, 9, 21),
        3,
    )


def test_scheduled_window_matches_incremental_remote_sensing_window():
    window = scheduled_date_window(
        date(2026, 9, 1),
        today=date(2026, 9, 22),
    )

    assert window == (date(2026, 9, 2), date(2026, 9, 22))


def test_invalid_or_too_large_windows_are_rejected():
    with pytest.raises(ValueError, match="date_to"):
        resolve_historical_weather_window(
            today=date(2026, 9, 22),
            date_to="2026-09-23",
        )
    with pytest.raises(ValueError, match="exceeds"):
        resolve_historical_weather_window(
            today=date(2026, 9, 22),
            days=3661,
        )
