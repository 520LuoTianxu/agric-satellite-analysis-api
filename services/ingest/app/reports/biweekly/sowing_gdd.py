"""播种日期和积温，给双周报结论用。

精量播种表单里的「播种日期」才是播种当天；填写日期可能晚几天。
积温用已经拿到的逐日最低、最高气温现算，不另查库，也不把缺测日当成 0。

小麦出苗用 0℃ 基数。天气任务入库的积温默认按 10℃，秋天日均温常低于 10℃，
那样会把小麦仍在累积的温度算成 0，结论就会一律说积温低。这里的数只进报告，
不回写 weather_daily。
"""

from __future__ import annotations

from datetime import date
from typing import Any

# 出苗判断最多往前看这么多天的气温。再早的播期只覆盖有记录的那一段。
MAX_SOWING_LOOKBACK_DAYS = 150
_SOWING_CODES = {"PRECISION_SOWING"}
_WHEAT_STAGES = {
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


def _iso_day(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) < 10 or text[4] != "-" or text[7] != "-":
        return None
    try:
        date.fromisoformat(text[:10])
    except ValueError:
        return None
    return text[:10]


def _answer(node: dict[str, Any], label: str) -> str | None:
    for item in node.get("answers") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("label") or "") == label and str(item.get("value") or "").strip():
            return str(item["value"]).strip()
    return None


def _is_sowing(node: dict[str, Any]) -> bool:
    code = str(node.get("task_code") or "").upper()
    name = str(node.get("task_name") or "")
    if "播种前" in name:
        return False
    if code in _SOWING_CODES or "精量播种" in name:
        return True
    return "播种" in name and "补种" not in name


def sowing_event(facts: dict[str, Any]) -> dict[str, Any] | None:
    """最近一次能确定日期的播种。有「播种日期」时不用表单填写日代替。"""
    best: dict[str, Any] | None = None
    nodes = (facts.get("key_node_records") or {}).get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict) or not _is_sowing(node):
            continue
        sown = _iso_day(_answer(node, "播种日期"))
        recorded = _iso_day(node.get("available_on")) or _iso_day(
            node.get("submitted_on")
        )
        anchor = sown or recorded
        if not anchor:
            continue
        candidate = {
            "task_name": str(node.get("task_name") or "播种"),
            "anchor": anchor,
            "sown_on": sown,
            "date_source": "播种日期" if sown else (
                "可执行日期" if _iso_day(node.get("available_on")) else "填写日期"
            ),
            "variety": _answer(node, "品种"),
        }
        if best is None or anchor > str(best["anchor"]):
            best = candidate
        elif anchor == best["anchor"] and sown and not best.get("sown_on"):
            best = candidate
    return best


def sowing_anchor(facts: dict[str, Any]) -> str | None:
    event = sowing_event(facts)
    if not event:
        return None
    return str(event["anchor"])


def antecedent_days_for(facts: dict[str, Any], period_start: str) -> int:
    """气温窗口至少盖住播种日，便于算出播后积温。"""
    from app.reports.biweekly.weather_fetch import ANTECEDENT_DAYS

    sown = sowing_anchor(facts)
    start = _iso_day(period_start)
    if not sown or not start:
        return ANTECEDENT_DAYS
    gap = (date.fromisoformat(start) - date.fromisoformat(sown)).days
    if gap <= 0:
        return ANTECEDENT_DAYS
    return min(max(gap, ANTECEDENT_DAYS), MAX_SOWING_LOOKBACK_DAYS)


def earliest_temperature_day(facts: dict[str, Any]) -> str | None:
    days = [
        day
        for row in (facts.get("weather_daily") or [])
        if isinstance(row, dict)
        and row.get("min_c") is not None
        and row.get("max_c") is not None
        and (day := _iso_day(row.get("date")))
    ]
    return min(days) if days else None


def needs_sowing_temperatures(facts: dict[str, Any]) -> bool:
    """播种日早于现有气温时，还要再拉一段，否则播后积温对不齐。"""
    sown = sowing_anchor(facts)
    if not sown:
        return False
    earliest = earliest_temperature_day(facts)
    return earliest is None or earliest > sown


def _stage_names(facts: dict[str, Any]) -> list[str]:
    phase = facts.get("phase") or {}
    rows = phase.get("observation_stages") or phase.get("planned_days") or []
    return [
        str(row.get("stage_code") or "")
        for row in rows
        if isinstance(row, dict) and row.get("stage_code")
    ]


def gdd_base_c(facts: dict[str, Any]) -> float:
    """小麦出苗按 0℃。玉米和其余作物按天气任务的 10℃。"""
    crop = str(facts.get("crop_type") or "").lower()
    if any(token in crop for token in ("玉米", "maize", "corn")):
        return 10.0
    if any(token in crop for token in ("小麦", "wheat")):
        return 0.0
    if any(name in _WHEAT_STAGES for name in _stage_names(facts)):
        return 0.0
    return 10.0


def _daily_gdd(row: dict[str, Any], base: float) -> float | None:
    if row.get("min_c") is None or row.get("max_c") is None:
        return None
    try:
        mean = (float(row["min_c"]) + float(row["max_c"])) / 2.0
    except (TypeError, ValueError):
        return None
    if mean != mean:
        return None
    return max(0.0, mean - base)


def gdd_between(
    rows: list[Any], start: str, end: str, base: float
) -> dict[str, Any] | None:
    total = 0.0
    days = 0
    first: str | None = None
    last: str | None = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        day = _iso_day(row.get("date"))
        if not day or day < start or day > end:
            continue
        value = _daily_gdd(row, base)
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


def _latest_day(facts: dict[str, Any]) -> str | None:
    comparison = facts.get("comparison") or {}
    period = facts.get("period") or {}
    return _iso_day(comparison.get("latest_date")) or _iso_day(
        period.get("end_inclusive") or period.get("end")
    )


def sowing_for_model(facts: dict[str, Any]) -> dict[str, Any] | None:
    event = sowing_event(facts)
    if not event:
        return None
    latest = _latest_day(facts)
    anchor = str(event["anchor"])
    days = None
    if latest and latest >= anchor:
        days = (date.fromisoformat(latest) - date.fromisoformat(anchor)).days
    body: dict[str, Any] = {
        "task_name": event["task_name"],
        "date_source": event["date_source"],
        "days_to_latest": days,
    }
    if event.get("sown_on"):
        body["sown_on"] = event["sown_on"]
    else:
        body["recorded_on"] = anchor
    if event.get("variety"):
        body["variety"] = event["variety"]
    return body


def gdd_for_model(facts: dict[str, Any]) -> dict[str, Any] | None:
    base = gdd_base_c(facts)
    rows = list(facts.get("weather_daily") or [])
    period = facts.get("period") or {}
    start = _iso_day(period.get("start"))
    end = _iso_day(period.get("end_inclusive") or period.get("end"))
    period_sum = gdd_between(rows, start, end, base) if start and end else None
    event = sowing_event(facts)
    since = None
    # 播后积温算到最近一次观测，和「过了几天」用同一天。
    since_end = _latest_day(facts) or end
    if event and since_end:
        since = gdd_between(rows, str(event["anchor"]), since_end, base)
    if not period_sum and not since:
        return None
    body: dict[str, Any] = {"base_c": int(base)}
    if period_sum:
        body["period"] = period_sum
    if since and event:
        body["since_sowing"] = {
            **since,
            "covers_sowing_day": since["from"] <= str(event["anchor"]),
        }
    return body
