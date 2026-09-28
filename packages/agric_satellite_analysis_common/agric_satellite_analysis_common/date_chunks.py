"""遥感历史任务按闭区间日期窗口生成有界分片。"""

from __future__ import annotations

from datetime import date, timedelta

from agric_satellite_analysis_common.weather_window import MAX_WEATHER_HISTORY_DAYS


def split_inclusive_date_range(
    start: date,
    end: date,
    chunk_days: int,
    *,
    max_days: int = MAX_WEATHER_HISTORY_DAYS,
) -> list[tuple[date, date]]:
    """把含首尾的日期窗口拆成不重叠、数量有上限的区间。

    遥感编排器会为每个分片创建独立任务；统一验证步长、日期顺序和总跨度，
    并用闭区间循环保留单日窗口及最后一天，避免漏算或生成超大任务列表。
    """
    if chunk_days < 1:
        raise ValueError("chunk_days must be at least 1")
    if end < start:
        raise ValueError("end date must be >= start date")
    if max_days < 1:
        raise ValueError("max_days must be at least 1")

    window_days = (end - start).days + 1
    if window_days > max_days:
        raise ValueError(f"date range exceeds {max_days} days")

    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks
