"""Facts printed under 结论 even when the model text is only about indices.

Numbers come from weather, the key-node payload and the soil archive.
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


def _weather_lines(facts: dict[str, Any]) -> list[str]:
    period = facts.get("weather_period") or {}
    low, high, mean = (
        _temp(period.get("min_c")),
        _temp(period.get("max_c")),
        _temp(period.get("mean_c")),
    )
    if not any((low, high, mean)):
        return []
    bits = []
    if low:
        bits.append(f"最低{low}℃")
    if high:
        bits.append(f"最高{high}℃")
    if mean:
        bits.append(f"日均{mean}℃")
    days = period.get("temperature_days")
    day_text = f"，有记录{days}天" if days else ""
    lines = [f"本期格点气温：{'，'.join(bits)}{day_text}。不是田间实测。"]
    prior = facts.get("weather_same_period_prior_year") or {}
    prior_mean = _temp(prior.get("mean_c"))
    if prior_mean and mean:
        delta = float(period["mean_c"]) - float(prior["mean_c"])
        lines.append(
            f"去年同期（{prior.get('start')}至{prior.get('end')}）日均{prior_mean}℃，"
            f"本期日均相差{delta:+.1f}℃。"
        )
    return lines


def _node_lines(facts: dict[str, Any]) -> list[str]:
    payload = facts.get("key_node_records") or {}
    status = str(payload.get("status") or "")
    if status == "source_unavailable":
        return ["本季关键节点填写没有读到，结论不含人工填报。"]
    if status in {"query_failed", "season_unmapped", "land_invalid"}:
        return ["本季关键节点填写没有读到，结论不含人工填报。"]
    period = facts.get("period") or {}
    stage = ""
    phase = facts.get("phase") or {}
    stages = phase.get("observation_stages") or phase.get("planned_days") or []
    names = [
        str(row.get("stage_code") or "") for row in stages if isinstance(row, dict)
    ]
    stage = "、".join(dict.fromkeys(name for name in names if name))
    selected = select_key_nodes(
        list(payload.get("nodes") or []),
        period_start=str(period.get("start") or "")[:10],
        period_end=str(period.get("end_inclusive") or period.get("end") or "")[:10],
        stage_text=stage,
    )
    if not selected:
        if status == "empty":
            return ["本季没有已填写的地块关键节点。"]
        if payload.get("nodes"):
            return ["本期窗口和当前生育阶段没有对应的已填写关键节点。"]
        return []
    lines = []
    for node in selected[:4]:
        bits = [
            f"{item['label']} {item['value']}"
            for item in (node.get("answers") or [])[:4]
        ]
        detail = "；".join(bits)
        when = node.get("submitted_on") or "未记提交日"
        relevance = "本期内" if node.get("relevance") == "in_period" else "当前阶段"
        photo = (
            f"，附照片{node.get('photo_count')}张" if node.get("photo_count") else ""
        )
        lines.append(
            f"关键节点「{node.get('task_name')}」（{relevance}，{when}，{node.get('task_status')}）"
            f"：{detail}{photo}。"
        )
    return lines


def _soil_line(facts: dict[str, Any]) -> str:
    soil = facts.get("soil_context") or {}
    if soil.get("status") != "ready":
        return ""
    bits = []
    if soil.get("texture"):
        bits.append(f"质地{soil['texture']}")
    if soil.get("ph") is not None:
        bits.append(f"pH {soil['ph']}")
    if soil.get("som_g_kg") is not None:
        bits.append(f"有机质{soil['som_g_kg']}克/千克")
    if soil.get("rootzone_awc_mm") is not None:
        bits.append(f"根区可持水{soil['rootzone_awc_mm']}毫米")
    if not bits:
        return ""
    return "土壤本底：" + "，".join(bits) + "。这是地块档案，不是本期测土。"


def _photo_line(facts: dict[str, Any]) -> str:
    review = facts.get("photo_review") or {}
    count = int(review.get("count") or 0)
    if count <= 0:
        return ""
    status = review.get("status")
    if status == "reviewed" and review.get("note"):
        return f"现场照片{count}张，画面检查：{review['note']}"
    if status == "failed":
        return f"现场照片{count}张，画面检查没有完成，不描述照片内容。"
    return f"现场照片{count}张。未做画面检查，不描述照片内容。"


def build_conclusion_context(facts: dict[str, Any]) -> dict[str, Any]:
    lines = _weather_lines(facts)
    lines.extend(line for line in _node_lines(facts) if line)
    soil = _soil_line(facts)
    if soil:
        lines.append(soil)
    photo = _photo_line(facts)
    if photo:
        lines.append(photo)
    return {"lines": lines}


def attach_conclusion_context(facts: dict[str, Any]) -> dict[str, Any]:
    facts["conclusion_context"] = build_conclusion_context(facts)
    return facts
