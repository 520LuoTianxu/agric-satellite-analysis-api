"""Short notes printed under 结论.

The model writes the plain-language conclusion. These lines only name the
farm work already filed and the soil archive, so the gray block stays short.
This module does not call a model and does not invent a measurement.
"""

from __future__ import annotations

from typing import Any

from agric_satellite_analysis_common.biweekly.field_records import select_key_nodes


def _temp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return None


def _selected_nodes(facts: dict[str, Any]) -> list[dict[str, Any]]:
    payload = facts.get("key_node_records") or {}
    period = facts.get("period") or {}
    phase = facts.get("phase") or {}
    stages = phase.get("observation_stages") or phase.get("planned_days") or []
    names = [
        str(row.get("stage_code") or "") for row in stages if isinstance(row, dict)
    ]
    stage = "、".join(dict.fromkeys(name for name in names if name))
    return select_key_nodes(
        list(payload.get("nodes") or []),
        period_start=str(period.get("start") or "")[:10],
        period_end=str(period.get("end_inclusive") or period.get("end") or "")[:10],
        stage_text=stage,
    )


def _acidity(ph: Any) -> str | None:
    number = _temp(ph)
    if number is None:
        return None
    value = float(ph)
    if value < 6.5:
        word = "偏酸"
    elif value <= 7.5:
        word = "中性"
    else:
        word = "偏碱"
    return f"酸碱{word}（{number}）"


def soil_reader_line(facts: dict[str, Any]) -> str:
    """One plain sentence for the soil archive. Empty when it was not loaded."""
    soil = facts.get("soil_context") or {}
    if soil.get("status") != "ready":
        return ""
    bits = []
    if soil.get("texture"):
        bits.append(f"土质{soil['texture']}")
    acidity = _acidity(soil.get("ph"))
    if acidity:
        bits.append(acidity)
    if soil.get("som_g_kg") is not None:
        bits.append(f"有机质{soil['som_g_kg']}克/千克")
    if soil.get("rootzone_awc_mm") is not None:
        bits.append(f"大约能存{soil['rootzone_awc_mm']}毫米水")
    if not bits:
        return ""
    return "土壤底子：" + "，".join(bits) + "。这是地块原来的档案，不是这次新测的土。"


def _farmwork_line(facts: dict[str, Any]) -> str:
    """Name the filed tasks. Do not print form fields or internal codes."""
    bits = []
    for node in _selected_nodes(facts)[:3]:
        name = str(node.get("task_name") or "").strip()
        if not name:
            continue
        when = str(node.get("submitted_on") or "")[:10]
        bits.append(f"{name}（{when}）" if when else name)
    if not bits:
        return ""
    return "已填写的农事：" + "、".join(bits) + "。这是人工记录，不是卫星测到的。"


def _photo_line(facts: dict[str, Any]) -> str:
    review = facts.get("photo_review") or {}
    count = int(review.get("count") or 0)
    note = str(review.get("note") or "").strip()
    if count <= 0 or review.get("status") != "reviewed" or not note:
        return ""
    return f"现场照片{count}张：{note}"


def build_conclusion_context(facts: dict[str, Any]) -> dict[str, Any]:
    # 气温、去年同期由模型写进结论。这里不再把原数和空状态再印成灰色长段。
    lines = [
        line
        for line in (
            _farmwork_line(facts),
            soil_reader_line(facts),
            _photo_line(facts),
        )
        if line
    ]
    return {"lines": lines}


def attach_conclusion_context(facts: dict[str, Any]) -> dict[str, Any]:
    facts["conclusion_context"] = build_conclusion_context(facts)
    return facts
