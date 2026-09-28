"""生育期报告日期窗口的统一校验规则。"""

from __future__ import annotations

from datetime import date

from agric_satellite_analysis_common.weather_window import MAX_WEATHER_HISTORY_DAYS


def normalize_season_growth_window(
    start_date: date | str,
    end_date: date | str,
) -> tuple[date, date, int]:
    """规范并限制生育期报告窗口，返回起止日和含首尾的天数。

    报告会驱动天气与遥感回填；在 API、worker 和报告计算入口复用同一上限，
    避免超长任务扩大分片数，或在下载机上等待无效数据。
    """
    try:
        start = start_date if isinstance(start_date, date) else date.fromisoformat(str(start_date)[:10])
        end = end_date if isinstance(end_date, date) else date.fromisoformat(str(end_date)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError("start_date and end_date must be YYYY-MM-DD") from exc

    if end < start:
        raise ValueError("end_date must be >= start_date")
    if end > date.today():
        raise ValueError("end_date cannot be later than today")

    # 日期窗口按首尾闭区间计数，与数据库筛选和天气回填的口径一致。
    days = (end - start).days + 1
    if days > MAX_WEATHER_HISTORY_DAYS:
        raise ValueError(
            f"season-growth report window exceeds {MAX_WEATHER_HISTORY_DAYS} days"
        )
    return start, end, days
