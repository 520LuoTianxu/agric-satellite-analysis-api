"""天气历史回填的统一日期窗口规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


# 遥感历史窗口最多支持 10 年；天气接口也使用同一上限，避免两套数据的
# 可用范围不一致。前端当前开放到 5 年，API 保留更大的服务端余量。
MAX_WEATHER_HISTORY_DAYS = 3660
DEFAULT_WEATHER_BACKFILL_DAYS = 60 * 30


@dataclass(frozen=True)
class WeatherWindow:
    """天气历史任务实际向 Open-Meteo 请求的闭区间。"""

    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def _as_date(value: date | str, *, name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD") from exc


def date_years_ago(day: date, years: int) -> date:
    """按日历年回溯，处理 2 月 29 日落到 2 月 28 日。"""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def resolve_historical_weather_window(
    *,
    today: date | None = None,
    days: int | None = None,
    years: int | None = None,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
    default_days: int = DEFAULT_WEATHER_BACKFILL_DAYS,
) -> WeatherWindow:
    """Resolve an explicit date window or a relative historical window.

    Open-Meteo archive data is treated as historical data, so the effective end
    date cannot be later than yesterday. The UI can still query today and the
    forecast separately through the weather read API.
    """
    reference_day = today or date.today()
    requested_end = _as_date(date_to, name="date_to") if date_to else reference_day
    if requested_end > reference_day:
        raise ValueError("date_to cannot be later than today")

    effective_end = min(requested_end, reference_day - timedelta(days=1))
    if date_from is not None:
        start = _as_date(date_from, name="date_from")
    elif years is not None:
        start = date_years_ago(requested_end, int(years))
    else:
        requested_days = int(days or default_days)
        if requested_days < 1:
            raise ValueError("days must be at least 1")
        start = effective_end - timedelta(days=requested_days - 1)

    if start > effective_end:
        raise ValueError("date_from must be no later than the historical end date")
    window = WeatherWindow(start=start, end=effective_end)
    if window.days > MAX_WEATHER_HISTORY_DAYS:
        raise ValueError(
            f"weather date range exceeds {MAX_WEATHER_HISTORY_DAYS} days"
        )
    return window


__all__ = [
    "DEFAULT_WEATHER_BACKFILL_DAYS",
    "MAX_WEATHER_HISTORY_DAYS",
    "WeatherWindow",
    "date_years_ago",
    "resolve_historical_weather_window",
]
