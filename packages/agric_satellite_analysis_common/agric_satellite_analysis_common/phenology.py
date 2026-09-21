"""从有效观测识别冠层生长窗口；不把日历预设当成实测生育期。"""

from __future__ import annotations

from datetime import date
from math import isfinite
from statistics import median
from typing import Any

METHOD = "ndvi-relative-amplitude-v1"
MAX_GAP_DAYS = 35


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if isfinite(result) else None


def infer_phenology(points: list[dict], *, start: date, end: date) -> dict:
    """仅使用日期窗内、调用方已审核为有效的日观测，返回可解释的候选窗口。

    相对振幅阈值用于适应地块冠层差异；缺景超过 35 天不跨越推断，边界
    未被前后低值夹住时保持未知。结果是遥感冠层物候，不是播种/收获实测日。
    """
    by_day: dict[date, list[float]] = {}
    for point in points:
        if point.get("official") is not True:
            continue
        try:
            day = date.fromisoformat(str(point.get("date"))[:10])
        except ValueError:
            continue
        value = number(point.get("ndvi", point.get("ndvi_avg")))
        if start <= day <= end and value is not None and -1 <= value <= 1:
            by_day.setdefault(day, []).append(value)
    series = sorted((day, median(values)) for day, values in by_day.items())
    result = {
        "method": METHOD,
        "status": "insufficient_data",
        "observation_count": len(series),
        "max_gap_days": max(
            ((b[0] - a[0]).days for a, b in zip(series, series[1:])), default=0
        ),
        "threshold": None,
        "windows": [],
        "note": "需至少 6 个有效观测日并覆盖 45 天；无法推断时请补充历史影像或人工选窗。",
    }
    if len(series) < 6 or (series[-1][0] - series[0][0]).days < 45:
        return result

    # 中值平滑只用相邻且间隔合理的观测，避免把长时间云遮插成生长曲线。
    smooth = []
    for i, (day, value) in enumerate(series):
        nearby = [
            v for d, v in series[max(0, i - 1) : i + 2] if abs((d - day).days) <= 20
        ]
        smooth.append(median(nearby) if len(nearby) == 3 else value)
    ordered = sorted(smooth)
    base = ordered[int((len(ordered) - 1) * 0.2)]
    peak = max(smooth)
    amplitude = peak - base
    result.update(
        status="no_distinct_cycle",
        note="未检出清晰季节起伏，不代表未种植；常绿作物或观测不完整也会出现此结果。",
    )
    if amplitude < 0.15 or peak < 0.4:
        return result
    threshold = max(0.3, base + amplitude * 0.25)
    result["threshold"] = round(threshold, 4)

    runs: list[list[int]] = []
    active: list[int] = []
    for i, value in enumerate(smooth):
        gap = i > 0 and (series[i][0] - series[i - 1][0]).days > MAX_GAP_DAYS
        if active and (gap or value < threshold):
            runs.append(active)
            active = []
        if value >= threshold:
            active.append(i)
    if active:
        runs.append(active)

    for run in runs:
        first, last = run[0], run[-1]
        if len(run) < 3 or (series[last][0] - series[first][0]).days < 20:
            continue
        left = (
            first > 0
            and smooth[first - 1] < threshold
            and (series[first][0] - series[first - 1][0]).days <= MAX_GAP_DAYS
        )
        right = (
            last + 1 < len(series)
            and smooth[last + 1] < threshold
            and (series[last + 1][0] - series[last][0]).days <= MAX_GAP_DAYS
        )
        peak_i = max(run, key=lambda i: smooth[i])
        # 完整周期也只给中置信度：经验阈值尚需本地样本校准，不能包装成精确农学阶段。
        window = {
            "start_date": series[first][0].isoformat() if left else None,
            "end_date": series[last][0].isoformat() if right else None,
            "observed_start": series[first][0].isoformat(),
            "observed_end": series[last][0].isoformat(),
            "peak_date": series[peak_i][0].isoformat(),
            "peak_ndvi": round(smooth[peak_i], 4),
            "start_interval": [
                series[first - 1][0].isoformat(),
                series[first][0].isoformat(),
            ]
            if left
            else None,
            "end_interval": [
                series[last][0].isoformat(),
                series[last + 1][0].isoformat(),
            ]
            if right
            else None,
            "confidence": "medium" if left and right and len(run) >= 5 else "low",
            "status": "complete"
            if left and right
            else "open_end"
            if left and last == len(series) - 1
            else "partial",
            "observation_count": len(run),
        }
        result["windows"].append(window)
    if result["windows"]:
        result.update(
            status="detected",
            note="候选窗口来自冠层绿度变化；起止区间受卫星过境和云遮影响，不等于精确播种、成熟或收获日期。",
        )
    return result


def window_months(windows: list[dict]) -> tuple[int, ...]:
    """按真实日期窗口取月份，支持跨年冬作；缺边界使用已观测范围。"""
    months: set[int] = set()
    for window in windows:
        first = window.get("start_date") or window.get("observed_start")
        last = window.get("end_date") or window.get("observed_end")
        if not first or not last:
            continue
        day, end = date.fromisoformat(first), date.fromisoformat(last)
        while day <= end:
            months.add(day.month)
            day = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    return tuple(sorted(months))


def infer_index_rows(indices: list[dict]) -> dict:
    """适配旧评估输入：质量未经确认的统计行不能成为自动推断依据。"""
    points = []
    for row in indices:
        if str(row.get("layer_type") or row.get("layer") or "").upper() != "NDVI":
            continue
        quality = number(row.get("quality_score", row.get("quality")))
        official = row.get("official") is True or (
            quality is not None and quality >= 0.7
        )
        try:
            day = date.fromisoformat(str(row.get("date"))[:10])
        except ValueError:
            continue
        points.append(
            {"date": day.isoformat(), "ndvi": row.get("mean"), "official": official}
        )
    days = [date.fromisoformat(p["date"]) for p in points]
    return infer_phenology(
        points,
        start=min(days) if days else date(2015, 1, 1),
        end=max(days) if days else date(2015, 1, 1),
    )
