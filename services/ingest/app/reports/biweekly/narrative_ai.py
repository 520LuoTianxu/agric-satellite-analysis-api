"""模型撰写双周报里「往好 / 往差」的文字说明。

数值、日期和升高、降低由程序算好后传给模型。模型只写判断和说明，
不能改数字，也不能新增事实里没有的数字。代码不根据涨跌或生育期决定好坏。
模型名、地址和密钥只从 BAILIAN_MODEL、BAILIAN_BASE_URL、BAILIAN_API_KEY 读取。
补全长度、等待和是否思考从 BAILIAN_MAX_TOKENS、BAILIAN_TIMEOUT_S、
BAILIAN_ENABLE_THINKING 读取。DeepSeek 混合思考默认关闭，给 JSON 正文留额度。
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any

import structlog

from agric_satellite_analysis_common.biweekly.metric_specs import (
    METRIC_SPECS,
    metric_abbr,
)
from agric_satellite_analysis_common.biweekly.phase_policy import STAGE_BACKGROUND

logger = structlog.get_logger("biweekly.narrative")

DEFAULT_BASE_URL = (
    "https://llm-7cudikcfvgf9l1hy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_MODEL = "qwen3.7-flash"
# 百炼 deepseek-v4.1-flash-1017（2026-11 文档）：
# 最大输出 393216。max_tokens 同时限制思维链和正文。thinking_budget 对该快照
# 标为不支持。它是混合思考模型，默认开启思考，HTTP 正文顶层 enable_thinking
# 可关闭；reasoning_effort 仅 low/high/max，默认 high。官方 DeepSeek 的
# thinking.type 不在这条百炼兼容接口的参数表里，这里不发送。
# 生产 2026-11-24 P15：max_tokens=8192、finish_reason=length、completion_tokens=8192、
# content_chars=0、reasoning_chars=14791。思考随预算变长，把上限改成 16384
# 不能单独证明正文会出现。默认关闭思考，正文预算 16384，上限用模型的 393216。
# 非 DeepSeek 模型不发送 enable_thinking，输出保护上限 32768。
# 一次报告最多两次模型调用。超时、连接中断、429 和 5xx 会再试一次。
# 超时未设置时按预算估算，夹在 30–600 秒，两次调用仍低于 Celery 软限制 1500 秒。
DEEPSEEK_OUTPUT_MAX = 393216
OTHER_OUTPUT_MAX = 32768
DEFAULT_MAX_TOKENS = 16384
MIN_MAX_TOKENS = 1024
MIN_TIMEOUT_S = 30
MAX_TIMEOUT_S = 600
MAX_MODEL_CALLS = 2
_RETRYABLE_HTTP = {429, 500, 502, 503, 504}
JUDGMENTS = {"better", "worse", "unclear"}
# 结论里用这几个说法，不把英文缩写交给模型复读。
_PLAIN_METRIC = {
    "ndvi": "长势",
    "evi": "绿度",
    "ndre": "叶绿素",
    "ndmi": "叶片含水",
}
_PLAIN_MEANING = {
    "ndvi": "看这块地的庄稼盖得密不密、长得旺不旺",
    "evi": "看庄稼绿不绿；长得很密时，这个数比长势更不容易看满",
    "ndre": "看叶子里的叶绿素够不够，庄稼长密以后更有用",
    "ndmi": "看叶子里的水多不多",
}
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

SYSTEM_PROMPT = """你在写农业遥感双周报的正文。读者是种植户，不是遥感人员。用户消息里有 parameter_guide 和本期参数。parameter_guide 说明每个参数是什么意思，其余字段是程序已经算好的参数值。

请直接根据这些参数发挥，写成给种植户看的报告。篇幅按内容来，该写到的都写到，不要为了短而省略。用种植户听得懂的说法把事情讲明白：指标用 name 里的叫法（长势、绿度、叶绿素、叶片含水），并说明这个数在看什么、升高或降低对这块地意味着什么。不要只堆 NDVI、EVI、NDRE、NDMI、EXG、ET0、格点、像元、百分位、中位数和公式。自己组织句子，不要每条都写成“由A升到B，提高C”这一种句式，也不要只写“全部上升、方向一致”。

参数里有的指标、日期、田内部位、未采用观测、冷热和有效降雨、去年同期、已填写的农事、土壤底子和周边对照都要写到，并用白话说明白。某个参数为空就不要编，也不要用“缺失”凑字。不要写施肥、打药。结论里要用白话判断本期偏干还是偏湿：偏干就说要不要留意浇水，偏湿或有积水迹象就说要不要留意排水。依据只能是已经给出的有效降雨、叶子含水、土壤能存的水和 water_risk_lines。证据不够就写现在还不能判断，不要写成没有风险，也不要写灌多少毫米。降雨一律写成「有效降雨」，不要写「雨水账」「雨水」或「账」。农事是人工填写的背景，说清做了哪件、哪一天、填写里人能看懂的内容，不要抄英文字段名和编号，也不要把它说成卫星测到的。有土壤底子就讲清土质、酸碱和能存多少水，不要判断缺肥。

infield 和 surrounding 是指标变化前面的两章，各自成章，篇幅按内容来。infield 讲地里哪一块和整块地不一样、偏弱的那一片在看什么。surrounding 讲这块地比周围农田更绿还是更弱，并说明周围的地种的不一定一样。不要写中位数、像元、百分位、超绿、EXG 和公式。

数值升高或降低只表示大小，不等于好坏。哪一句、哪个数字是重点，由你决定，再给那几个字加粗或上色。不要规定成某几个固定词，也不要每句末尾都补同一个词。

正文是雅黑 10 磅。你可以用这些标记，标记里面放你选中的字，不要把整句都包进去：
- 加粗：<b>…</b>
- 你认为往好处、值得注意的字：<font face="YaHei-Bold" color="#1F4D38" backColor="#D9F2E3">…</font>
- 你认为往差处、需要留意的字：<font face="YaHei-Bold" color="#9C2B1B" backColor="#F8D6D2">…</font>
- 你认为还不能下判断的字：<font face="YaHei-Bold" color="#8A5A12" backColor="#F8E8C8">…</font>
没有把握的地方保持正文。不要用别的颜色，不要用 Markdown。

metric_notes 是「指标说明」：每个指标按本期数值写成种植户能看懂的说明，讲清这个数在看什么、本期说明了什么。不要复读定义，不要写公式和英文缩写，每个指标的写法不要相同，重点由你标。篇幅不限。
conclusion 是给种植户看的结论。把遥感变化、冷热和有效降雨、农事和土壤底子写在一起，用通俗说法说明白。降雨写成「有效降雨」，不要写「雨水账」。context_lines 还会再印很短的农事和土壤底子，不要把那些原句再抄一遍。重点由你用上面的标记标出来。
数字和日期只能照抄参数里已经出现的写法。不要写「低值」「参考线」「预警线」「达标」「确诊」「灾损」「产量」「好转」「变差」，也不要提到模型或 AI。
field_pattern_lines 和 water_risk_lines 已经印在报告里，不要原句再抄一遍。缺肥、病虫害和产量不要下结论。旱涝和要不要浇水、排水写在结论里，按已有水分证据用白话说明；证据不足时不要改成无风险。
不要输出思考过程，只输出 JSON：
{"summary":"一段话","changes":[{"metric":"ndvi","judgment":"better|worse|unclear","sentence":"带标记的句子"}],"conclusion":["带标记的句子"],"field_notes":["带标记的句子"],"metric_notes":["带标记的句子"],"infield":[{"metric":"ndre","text":"带标记的句子"}],"surrounding":["带标记的句子"],"weather":["带标记的句子"]}
changes 和 infield 的 metric 只能是 metrics 里的键。judgment 只能是 better、worse、unclear。句中的颜色由你选，不必和某个固定词对应。metric_notes 覆盖 metrics 里的每一个指标。
"""

# 跟参数一起交给模型，只解释含义，不另给可以引用的数字。
PARAMETER_GUIDE = {
    "metrics": "每个指标的全田结果。name 是写给种植户的叫法。meaning 只说明这个数在看什么，不要照抄。latest 是最近一次有效观测的数值，baseline 是拿来对比的上一次，delta 是 latest 减 baseline。value_direction 只表示数值升高、降低或持平。role 是本期主要指标还是辅助指标。",
    "used_observations": "本期采用的观测日期。",
    "rejected_observations": "卫星过境但没有采用的日期。reason 是未采用的原因，cloud_pct 是地块云量的百分比。",
    "infield": "地里哪一块和整块地不一样。median 是整块地的一般水平，p10 是偏低的一头，p90 是偏高的一头。regions.name 是哪一块，share_percent 是这一块占偏弱地方的百分比。写成给种植户看的说明，说清部位和这个数在看什么，不要说像元、百分位、中位数。",
    "surrounding": "这块地和周围大约一公里农田比，谁更绿。field_minus_nearby 是这块地减去周围，正数表示这块地更绿。写成给种植户看的说明，说清更绿或更弱，以及周围种的不一定一样。不要说超绿、EXG。",
    "weather": "地块附近的天气，不是田间实测。写的时候说冷热、比去年高还是低、有效降雨多还是少，不要说格点、ET0、雨水账、雨水或账。min_c、max_c、mean_c 是气温摄氏度，precipitation_mm 是有效降雨的毫米数。prior_year 是去年同一段日期。",
    "key_nodes": "人工填写的农事。用白话说明做了哪件、哪一天、answers 里人能看懂的内容。没有记录就不要写农事，也不要写“没有关键节点”。",
    "soil": "地块原来的土壤底子，不是这次新测的土。有 reader_sentence 就用这句的意思改写。status 不是 ready 就不要写土壤。不要据此判断缺肥。",
    "photo_review": "现场照片的画面检查。status 不是 reviewed 时，不要写照片。",
    "context_lines": "结论下面还会印的短句。不要原句复述。",
    "field_pattern_lines": "已经写好的持续偏弱、均匀度和历史同期句子。报告会印出，不要复述，也不要改数字。",
    "water_risk_lines": "已经写好的水分观测和推算，结论里要用这些数判断偏干还是偏湿，以及要不要留意浇水或排水。不要原句复述。原文里的“不判定”不要当成省略这个判断的理由。证据不够就说现在看不出来。",
    "stage": "种植方案里的计划生育阶段，不是田间实测的物候。",
    "stage_background": "这个阶段通常的背景，只供参考，不是必须套用的公式，也不是对本田的结论。",
    "gap_days": "最近一次观测和对比基准之间相隔的天数。",
}


def bailian_settings() -> dict[str, str]:
    return {
        "api_key": (os.environ.get("BAILIAN_API_KEY") or "").strip(),
        "base_url": (os.environ.get("BAILIAN_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        ),
        "model": (os.environ.get("BAILIAN_MODEL") or DEFAULT_MODEL).strip()
        or DEFAULT_MODEL,
    }


def _bounded_int(name: str, default: int, floor: int, ceiling: int) -> tuple[int, bool]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default, False
    try:
        value = int(raw)
    except ValueError:
        return default, True
    applied = min(ceiling, max(floor, value))
    return applied, applied != value


def _is_deepseek(model: str) -> bool:
    return "deepseek" in (model or "").lower()


def output_ceiling(model: str) -> int:
    """Documented completion cap for this model name. Not a universal provider limit."""
    if _is_deepseek(model):
        return DEEPSEEK_OUTPUT_MAX
    return OTHER_OUTPUT_MAX


def completion_budget(model: str) -> tuple[int, bool]:
    """Tokens requested for one completion, after the floor and the model ceiling."""
    ceiling = output_ceiling(model)
    default = min(DEFAULT_MAX_TOKENS, ceiling)
    floor = min(MIN_MAX_TOKENS, ceiling)
    return _bounded_int("BAILIAN_MAX_TOKENS", default, floor, ceiling)


def thinking_requested(model: str) -> bool | None:
    """DeepSeek hybrid thinking switch. None means do not send the field.

    Bailian documents enable_thinking for deepseek-v4.1-flash. The 1017 snapshot
    is that hybrid model and thinks by default. thinking_budget is not supported
    on the snapshot, so the switch is how the JSON keeps the completion budget.
    """
    if not _is_deepseek(model):
        return None
    raw = (os.environ.get("BAILIAN_ENABLE_THINKING") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def reasoning_effort() -> str:
    """Used only while thinking is on. Bailian accepts low, high, and max."""
    raw = (os.environ.get("BAILIAN_REASONING_EFFORT") or "low").strip().lower()
    if raw in {"low", "high", "max"}:
        return raw
    return "low"


def completion_timeout_s(
    explicit: float | None = None, tokens: int = DEFAULT_MAX_TOKENS
) -> tuple[float, bool]:
    """Client wait. Unset env scales with the completion budget, capped at 600s."""
    if explicit is not None:
        try:
            value = int(explicit)
        except (TypeError, ValueError):
            return 180.0, True
        applied = min(MAX_TIMEOUT_S, max(MIN_TIMEOUT_S, value))
        return float(applied), applied != value
    raw = (os.environ.get("BAILIAN_TIMEOUT_S") or "").strip()
    if not raw:
        scaled = min(MAX_TIMEOUT_S, max(180, 30 + int(tokens) // 64))
        return float(scaled), False
    seconds, clamped = _bounded_int(
        "BAILIAN_TIMEOUT_S", 180, MIN_TIMEOUT_S, MAX_TIMEOUT_S
    )
    return float(seconds), clamped


def _next_budget(current: int, ceiling: int) -> int:
    return min(ceiling, max(current * 2, current + 8192))


def _content_text(content: Any) -> str:
    """Assistant content only. Reasoning parts are not text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type") or "text")
        if kind == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts)


def interpret_chat_completion(body: Any) -> tuple[str, dict[str, Any]]:
    """Return content text and a diagnostic that never includes reasoning text.

    HTTP 200 is not a successful body. ``reasoning_content`` is counted and
    then discarded. It is not parsed as the report.
    """
    choice: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
        if isinstance(body.get("usage"), dict):
            usage = body["usage"]
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    text = _content_text(message.get("content"))
    reasoning = message.get("reasoning_content")
    tokens = usage.get("completion_tokens")
    return text, {
        "finish_reason": choice.get("finish_reason"),
        "completion_tokens": tokens if isinstance(tokens, int) else None,
        "content_chars": len(text.strip()),
        "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
    }


def _section_has_facts(value: Any) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return False
    if isinstance(value, dict):
        return any(_section_has_facts(item) for item in value.values())
    if isinstance(value, list):
        return any(_section_has_facts(item) for item in value)
    return True


def missing_required_sections(
    normalized: dict[str, Any] | None, payload: dict[str, Any]
) -> list[str]:
    """Sections the PDF would leave blank even though the facts asked for them."""
    narrative = normalized or {}
    missing: list[str] = []
    if not narrative.get("conclusion"):
        missing.append("conclusion")
    if not narrative.get("field_notes"):
        missing.append("field_notes")
    changes = narrative.get("changes") or []
    has_change = any(isinstance(row, dict) and row.get("sentence") for row in changes)
    if payload.get("metrics") and not has_change:
        missing.append("changes")
    if payload.get("metrics") and not narrative.get("metric_notes"):
        missing.append("metric_notes")
    if _section_has_facts(payload.get("weather")) and not narrative.get("weather"):
        missing.append("weather")
    if _section_has_facts(payload.get("surrounding")) and not narrative.get(
        "surrounding"
    ):
        missing.append("surrounding")
    if _section_has_facts(payload.get("infield")) and not narrative.get("infield"):
        missing.append("infield")
    return missing


def _unusable_kind(
    text: str,
    parsed: dict[str, Any] | None,
    normalized: dict[str, Any] | None,
    diag: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[str | None, list[str]]:
    """Name why this completion is not a complete report. None means the sections are filled."""
    length = diag.get("finish_reason") == "length"
    if not text.strip():
        if length:
            return "truncated_response", ["conclusion", "field_notes"]
        if diag.get("reasoning_chars"):
            return "empty_with_reasoning", ["conclusion", "field_notes"]
        return "empty_content", ["conclusion", "field_notes"]
    if parsed is None:
        kind = "truncated_response" if length else "invalid_json"
        return kind, ["conclusion", "field_notes"]
    if not _has_wording(normalized or {}):
        kind = "truncated_response" if length else "empty_fields"
        return kind, missing_required_sections(normalized, payload)
    missing = missing_required_sections(normalized, payload)
    if missing:
        kind = "truncated_response" if length else "incomplete_fields"
        return kind, missing
    return None, []


def _safe_disconnect_reason(exc: BaseException, secret: str = "") -> str:
    """保留一句断连原因。去掉密钥、换行和过长原文。"""
    text = " ".join(str(exc).split())
    if secret:
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|authorization)\s*[=:]\s*\S+",
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    return text[:180]


def _disconnect_detail(exc: BaseException, secret: str = "") -> tuple[str, str | None]:
    """异常类型之外，再留底层原因的类型和一句安全说明。"""
    reason = _safe_disconnect_reason(exc, secret)
    cause = exc.__cause__
    if cause is None or cause is exc:
        return reason, None
    extra = _safe_disconnect_reason(cause, secret)
    if extra and extra not in reason:
        reason = (f"{reason} ({extra})" if reason else extra)[:180]
    return reason, type(cause).__name__


def _log_unusable(error: str, diag: dict[str, Any], *, model: str) -> None:
    # 只记长度、异常类型和截断后的断连原因。不记密钥、正文或 reasoning 原文。
    logger.warning(
        "biweekly_narrative_unusable",
        error=error,
        model=model,
        finish_reason=diag.get("finish_reason"),
        completion_tokens=diag.get("completion_tokens"),
        content_chars=diag.get("content_chars"),
        reasoning_chars=diag.get("reasoning_chars"),
        max_tokens=diag.get("max_tokens"),
        max_tokens_clamped=diag.get("max_tokens_clamped"),
        http_status=diag.get("http_status"),
        timeout_s=diag.get("timeout_s"),
        attempts=diag.get("attempts"),
        exception_type=diag.get("exception_type"),
        disconnect_reason=diag.get("disconnect_reason"),
        cause_type=diag.get("cause_type"),
    )


def _soil_for_model(facts: dict[str, Any]) -> dict[str, Any] | None:
    soil = facts.get("soil_context")
    if not isinstance(soil, dict):
        return None
    if soil.get("status") != "ready":
        return {"status": soil.get("status") or "absent"}
    from app.reports.biweekly.conclusion_context import _texture_zh, soil_reader_line

    body = {
        "status": "ready",
        "texture": _texture_zh(soil.get("texture")) or None,
        "ph": soil.get("ph"),
        "som_g_kg": soil.get("som_g_kg"),
        "rootzone_awc_mm": soil.get("rootzone_awc_mm"),
    }
    sentence = soil_reader_line(facts)
    if sentence:
        body["reader_sentence"] = sentence
    return body


def narrative_input(facts: dict[str, Any]) -> dict[str, Any]:
    """Compact, already-computed facts. No imagery and no judgment."""
    from app.reports.biweekly.report_text import period_text, stage_summary

    comparison = facts.get("comparison") or {}
    plan = facts.get("report_metrics") or {}
    stage = stage_summary(facts)
    background = STAGE_BACKGROUND.get(stage.get("latest_stage") or "") or {}
    metrics: dict[str, Any] = {}
    for role, keys in (
        ("primary", plan.get("primary")),
        ("supporting", plan.get("supporting")),
    ):
        for key in keys or []:
            item = (comparison.get("metrics") or {}).get(key) or {}
            spec = METRIC_SPECS.get(key)
            metrics[key] = {
                "name": _PLAIN_METRIC.get(key, spec.name if spec else metric_abbr(key)),
                "meaning": _PLAIN_MEANING.get(key) or (spec.purpose if spec else ""),
                "role": "主要" if role == "primary" else "辅助",
                "latest": _index(item.get("latest")),
                "baseline": _index(item.get("baseline")),
                "delta": _index(item.get("delta")),
                "value_direction": {"up": "升高", "down": "降低", "flat": "持平"}.get(
                    item.get("direction") or ""
                ),
            }
    quality = facts.get("data_quality") or {}
    return {
        "parameter_guide": PARAMETER_GUIDE,
        "period": period_text(facts),
        "stage": stage.get("text"),
        "stage_note": stage.get("source"),
        "stage_background": background.get("text"),
        "latest_date": comparison.get("latest_date"),
        "baseline_date": comparison.get("baseline_date"),
        "gap_days": comparison.get("gap_days"),
        "metrics": metrics,
        "used_observations": quality.get("used_dates"),
        "rejected_observations": _rejected_observations(quality),
        "infield": _infield_facts(facts),
        "surrounding": _surrounding_facts(facts),
        "weather": _weather_facts(facts),
        "key_nodes": _key_nodes_for_model(facts),
        "soil": _soil_for_model(facts),
        "photo_review": {
            "status": (facts.get("photo_review") or {}).get("status"),
            "count": (facts.get("photo_review") or {}).get("count") or 0,
            "note": (facts.get("photo_review") or {}).get("note"),
        },
        "context_lines": list(
            (facts.get("conclusion_context") or {}).get("lines") or []
        ),
        "field_pattern_lines": _pattern_lines(facts),
        "water_risk_lines": list((facts.get("water_risk") or {}).get("lines") or []),
    }


def _index(value: Any) -> str | None:
    """Two decimals, same as the evidence table, so the prose does not dump raw floats."""
    if value is None:
        return None
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return None


def _whole(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _rejected_observations(quality: dict[str, Any]) -> list[dict[str, Any]]:
    from app.reports.biweekly.report_text import REJECT_REASONS

    rows = []
    for row in quality.get("rejected") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "date": row.get("date"),
                "reason": REJECT_REASONS.get(row.get("reason") or "", "质量不符合要求"),
                "cloud_pct": _whole(row.get("cloud_pct")),
            }
        )
    return rows


def _infield_facts(facts: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in facts.get("infield") or []:
        stats = item.get("stats") or {}
        relative = item.get("relative") or {}
        regions = []
        for region in relative.get("regions") or []:
            if not region.get("name"):
                continue
            entry: dict[str, Any] = {"name": region["name"]}
            if region.get("share") is not None:
                entry["share_percent"] = _whole(float(region["share"]) * 100)
            regions.append(entry)
        rows.append(
            {
                "metric": item.get("metric"),
                "median": _index(stats.get("median")),
                "p10": _index(stats.get("p10")),
                "p90": _index(stats.get("p90")),
                "regions": regions,
            }
        )
    return rows


def _surrounding_facts(facts: dict[str, Any]) -> dict[str, Any]:
    meta = facts.get("surrounding_comparison_meta") or {}
    rows = []
    for row in meta.get("comparisons") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "date": row.get("date"),
                "field": _index(row.get("field")),
                "nearby_1km": _index(row.get("surrounding_1km")),
                "field_minus_nearby": _index(row.get("difference")),
            }
        )
    cover = meta.get("cover_2km")
    return {
        "comparisons": rows,
        "cover_2km_percent": None if cover is None else _whole(float(cover) * 100),
    }


def _prior_year_weather(summary: Any) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return None
    if summary.get("mean_c") is None and summary.get("precipitation_mm") is None:
        return None
    return {
        "start": summary.get("start"),
        "end": summary.get("end"),
        "min_c": _index(summary.get("min_c")),
        "max_c": _index(summary.get("max_c")),
        "mean_c": _index(summary.get("mean_c")),
        "precipitation_mm": _index(summary.get("precipitation_mm")),
    }


def _key_nodes_for_model(facts: dict[str, Any]) -> list[dict[str, Any]]:
    from app.reports.biweekly.conclusion_context import select_key_nodes

    payload = facts.get("key_node_records") or {}
    period = facts.get("period") or {}
    phase = facts.get("phase") or {}
    names = [
        str(row.get("stage_code") or "")
        for row in (phase.get("observation_stages") or phase.get("planned_days") or [])
        if isinstance(row, dict)
    ]
    selected = select_key_nodes(
        list(payload.get("nodes") or []),
        period_start=str(period.get("start") or "")[:10],
        period_end=str(period.get("end_inclusive") or "")[:10],
        stage_text="、".join(dict.fromkeys(name for name in names if name)),
    )
    compact = []
    for node in selected:
        compact.append(
            {
                "task_name": node.get("task_name"),
                "submitted_on": node.get("submitted_on"),
                "relevance": "本期"
                if node.get("relevance") == "in_period"
                else "当前阶段",
                "answers": [
                    {"label": item.get("label"), "value": item.get("value")}
                    for item in (node.get("answers") or [])
                ],
            }
        )
    return compact


def _pattern_lines(facts: dict[str, Any]) -> list[str]:
    patterns = facts.get("field_patterns") or {}
    lines = []
    lines.extend(patterns.get("persistent_lines") or [])
    lines.extend(patterns.get("uniformity_lines") or [])
    if patterns.get("history_line"):
        lines.append(patterns["history_line"])
    return [str(line) for line in lines if line]


def _weather_facts(facts: dict[str, Any]) -> dict[str, Any]:
    period = facts.get("weather_period") or {}
    antecedent = facts.get("weather_antecedent") or {}
    return {
        "period_days": period.get("days"),
        "min_c": _index(period.get("min_c")),
        "max_c": _index(period.get("max_c")),
        "mean_c": _index(period.get("mean_c")),
        "temperature_days": period.get("temperature_days"),
        "precipitation_mm": _index(period.get("precipitation_mm")),
        "precipitation_days": period.get("precipitation_days"),
        "antecedent_days": antecedent.get("days"),
        "antecedent_precipitation_mm": _index(antecedent.get("precipitation_mm")),
        "prior_year": _prior_year_weather(facts.get("weather_same_period_prior_year")),
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def _fact_blob(payload: dict[str, Any]) -> str:
    """Digits may be copied from parameter values, not from the explanatory guide."""
    values = {key: value for key, value in payload.items() if key != "parameter_guide"}
    return json.dumps(values, ensure_ascii=False)


def _visible_text(sentence: str) -> str:
    return re.sub(r"<[^>]+>", "", sentence)


def _numbers_are_copied(sentence: str, blob: str) -> bool:
    """A digit is allowed when the same value already appears in the facts JSON."""
    visible = _visible_text(sentence)
    known = [float(token) for token in _NUMBER.findall(blob)]
    for token in _NUMBER.findall(visible):
        if token in blob:
            continue
        try:
            value = float(token)
        except ValueError:
            return False
        if not any(
            math.isclose(value, item, rel_tol=0, abs_tol=1e-6) for item in known
        ):
            return False
    return True


_BANNED = (
    "低值",
    "参考线",
    "预警线",
    "达标",
    "确诊",
    "灾损",
    "产量",
    "好转",
    "变差",
    "模型",
    "人工智能",
)


def _asserts_diagnosis(sentence: str) -> bool:
    """Drop a nutrient or pest claim unless the sentence refuses it.

    旱涝和浇水、排水留在结论里，由模型按已有水分证据说明。
    """
    if not any(word in sentence for word in ("病虫害", "缺肥", "积水面积")):
        return False
    return not any(
        word in sentence for word in ("不判定", "不能判定", "不诊断", "不是灾损")
    )


def _clean_sentence(sentence: str, blob: str, limit: int) -> str:
    text = sentence.strip()
    if (
        not text
        or any(word in text for word in _BANNED)
        or _asserts_diagnosis(text)
        or re.search(r"\bAI\b", text, re.IGNORECASE)
        or not _numbers_are_copied(text, blob)
    ):
        return ""
    text = re.sub(r"<[^>]*$", "", text[:limit])
    return text


def _clean_list(rows: Any, blob: str, limit: int = 420) -> list[str]:
    if isinstance(rows, str):
        rows = [rows]
    cleaned = []
    for row in rows or []:
        text = _clean_sentence(str(row or ""), blob, limit)
        if text:
            cleaned.append(text)
    return cleaned[:8]


def normalize_judgment(
    parsed: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """Keep model wording whose digits were copied. Drop anything the facts do not support."""
    blob = _fact_blob(payload)
    allowed = set((payload.get("metrics") or {}).keys())
    changes: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in (parsed or {}).get("changes") or []:
        if not isinstance(row, dict):
            continue
        metric = str(row.get("metric") or "").lower()
        judgment = str(row.get("judgment") or "")
        sentence = str(row.get("sentence") or "").strip()
        if metric not in allowed or metric in seen or judgment not in JUDGMENTS:
            continue
        sentence = _clean_sentence(sentence, blob, 420)
        seen.add(metric)
        changes.append({"metric": metric, "judgment": judgment, "sentence": sentence})
    infield: dict[str, str] = {}
    for row in (parsed or {}).get("infield") or []:
        if not isinstance(row, dict):
            continue
        metric = str(row.get("metric") or "").lower()
        text = _clean_sentence(str(row.get("text") or ""), blob, 420)
        if metric in allowed and metric not in infield and text:
            infield[metric] = text
    summary = _clean_sentence(str((parsed or {}).get("summary") or ""), blob, 700)
    return {
        "summary": summary,
        "changes": changes,
        "conclusion": _clean_list((parsed or {}).get("conclusion"), blob),
        "field_notes": _clean_list((parsed or {}).get("field_notes"), blob),
        "metric_notes": _clean_list((parsed or {}).get("metric_notes"), blob),
        "infield": infield,
        "surrounding": _clean_list((parsed or {}).get("surrounding"), blob),
        "weather": _clean_list((parsed or {}).get("weather"), blob),
    }


_READER_NOTES = {
    "truncated_response": "本期模型文字未完成：输出长度用尽。数值和图表仍是程序计算结果。",
    "empty_content": "本期模型文字未完成：没有返回正文。数值和图表仍是程序计算结果。",
    "empty_with_reasoning": "本期模型文字未完成：没有返回正文。数值和图表仍是程序计算结果。",
    "invalid_json": "本期模型文字未完成：返回内容无法解析。数值和图表仍是程序计算结果。",
    "empty_fields": "本期模型文字未完成：结论为空。数值和图表仍是程序计算结果。",
    "incomplete_fields": "本期模型文字未完成：部分段落缺失。数值和图表仍是程序计算结果。",
    "timeout": "本期模型文字未完成：请求超时。数值和图表仍是程序计算结果。",
    "connection_error": "本期模型文字未完成：模型连接中断。数值和图表仍是程序计算结果。",
    "http_error": "本期模型文字未完成：模型服务暂时没有返回。数值和图表仍是程序计算结果。",
}
_GENERIC_READER_NOTE = "本期模型文字未完成。数值和图表仍是程序计算结果。"
_HIDDEN_ERRORS = {"not_configured", "no_metrics"}


def reader_note(narrative: dict[str, Any] | None) -> str:
    """One fixed sentence for the PDF. Empty when the model text is complete or not configured."""
    if not isinstance(narrative, dict):
        return ""
    error = narrative.get("error")
    if not error or error in _HIDDEN_ERRORS:
        return ""
    stored = narrative.get("reader_note")
    if isinstance(stored, str) and stored.strip():
        return stored.strip()
    return _READER_NOTES.get(str(error), _GENERIC_READER_NOTE)


def _apply_reader_note(judgment: dict[str, Any]) -> dict[str, Any]:
    note = reader_note(judgment)
    if note:
        judgment["reader_note"] = note
    return judgment


def empty_judgment(
    *,
    configured: bool,
    error: str | None = None,
    model: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "llm_configured": configured,
        "model": model,
        "summary": "",
        "changes": [],
        "conclusion": [],
        "field_notes": [],
        "metric_notes": [],
        "infield": {},
        "surrounding": [],
        "weather": [],
        "error": error,
    }
    if diagnostics:
        out["diagnostics"] = diagnostics
    return out


def _filled_score(judgment: dict[str, Any]) -> int:
    score = 0
    for key in ("conclusion", "field_notes", "metric_notes", "weather", "surrounding"):
        score += len(judgment.get(key) or [])
    score += sum(
        1
        for row in judgment.get("changes") or []
        if isinstance(row, dict) and row.get("sentence")
    )
    score += len(judgment.get("infield") or {})
    if judgment.get("summary"):
        score += 1
    return score


def _chat_body(
    cfg: dict[str, str],
    payload: dict[str, Any],
    *,
    max_tokens: int,
    thinking: bool | None,
    send_thinking: bool,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": cfg["model"],
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "下面是参数说明和本期参数。请直接发挥写成报告，只输出 JSON：\n"
                + json.dumps(payload, ensure_ascii=False),
            },
        ],
    }
    # 原始 HTTP，不是 OpenAI SDK，所以 enable_thinking 放在正文顶层。
    # 不发送 thinking_budget：百炼参数表标明该快照不支持。
    if send_thinking and thinking is not None:
        body["enable_thinking"] = thinking
        if thinking:
            body["reasoning_effort"] = reasoning_effort()
    return body


def _base_diag(
    *,
    max_tokens: int,
    tokens_clamped: bool,
    timeout_s: float,
    timeout_clamped: bool,
    thinking: bool | None,
    send_thinking: bool,
) -> dict[str, Any]:
    return {
        "max_tokens": max_tokens,
        "max_tokens_clamped": tokens_clamped,
        "timeout_s": timeout_s,
        "timeout_clamped": timeout_clamped,
        "enable_thinking": thinking if send_thinking else None,
        "thinking_field_sent": send_thinking and thinking is not None,
    }


def generate_metric_judgment(
    facts: dict[str, Any],
    *,
    timeout: float | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Ask the configured model for wording. A failed body stays empty and named.

    Does not raise. Does not copy reasoning into the report. At most two calls:
    the second turns thinking off, raises the budget after a length stop, or
    repeats once after timeout, a dropped connection, 429, or 5xx. It does not
    invent sentences.
    """
    cfg = bailian_settings()
    payload = narrative_input(facts)
    if not payload["metrics"]:
        return empty_judgment(
            configured=bool(cfg["api_key"]), error="no_metrics", model=cfg["model"]
        )
    if not cfg["api_key"]:
        return empty_judgment(configured=False, error="not_configured", model=None)
    configured_tokens, tokens_clamped = completion_budget(cfg["model"])
    tokens = configured_tokens
    ceiling = output_ceiling(cfg["model"])
    thinking = thinking_requested(cfg["model"])
    send_thinking = thinking is not None
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    own_client = client is None
    import httpx

    # 客户端等待按本次预算上限准备，第二次加预算时不会被第一次的超时截断。
    http = client or httpx.Client(timeout=completion_timeout_s(timeout, ceiling)[0])
    best: dict[str, Any] | None = None
    best_score = -1
    try:
        for attempt in range(1, MAX_MODEL_CALLS + 1):
            timeout_s, timeout_clamped = completion_timeout_s(timeout, tokens)
            body = _chat_body(
                cfg,
                payload,
                max_tokens=tokens,
                thinking=thinking,
                send_thinking=send_thinking,
            )
            # 加预算的第二次请求不是“被上限截断”。只有请求值被夹到模型上限时才记 clamped。
            at_model_cap = tokens == ceiling and configured_tokens > ceiling
            diag = _base_diag(
                max_tokens=tokens,
                tokens_clamped=tokens_clamped or at_model_cap,
                timeout_s=timeout_s,
                timeout_clamped=timeout_clamped,
                thinking=thinking,
                send_thinking=send_thinking,
            )
            diag["attempts"] = attempt
            try:
                response = http.post(
                    f"{cfg['base_url']}/chat/completions", headers=headers, json=body
                )
                response.raise_for_status()
            except httpx.TimeoutException:
                _log_unusable("timeout", diag, model=cfg["model"])
                result = _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error="timeout",
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
                if _filled_score(result) > best_score:
                    best, best_score = result, _filled_score(result)
                if attempt >= MAX_MODEL_CALLS:
                    break
                continue
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else None
                diag["http_status"] = status
                if status == 400 and send_thinking and attempt < MAX_MODEL_CALLS:
                    # 该模型不接受 enable_thinking 时，去掉字段再试一次，不换模型。
                    send_thinking = False
                    diag["thinking_param_rejected"] = True
                    _log_unusable("http_error", diag, model=cfg["model"])
                    continue
                _log_unusable("http_error", diag, model=cfg["model"])
                result = _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error="http_error",
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
                if attempt >= MAX_MODEL_CALLS or status not in _RETRYABLE_HTTP:
                    best = result
                    break
                continue
            except (httpx.NetworkError, httpx.ProtocolError, httpx.ProxyError) as exc:
                # 对端在完整响应前断开。再请求一次；仍失败就写明连接中断，不改用代码写结论。
                reason, cause_type = _disconnect_detail(exc, cfg["api_key"])
                diag["exception_type"] = type(exc).__name__
                diag["disconnect_reason"] = reason
                if cause_type:
                    diag["cause_type"] = cause_type
                _log_unusable("connection_error", diag, model=cfg["model"])
                result = _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error="connection_error",
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
                if _filled_score(result) > best_score:
                    best, best_score = result, _filled_score(result)
                if attempt >= MAX_MODEL_CALLS:
                    break
                continue
            except Exception as exc:  # noqa: BLE001 — 模型失败时不改用代码判断好坏
                diag["exception_type"] = type(exc).__name__
                _log_unusable(type(exc).__name__, diag, model=cfg["model"])
                return _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error=type(exc).__name__,
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
            try:
                raw = response.json()
            except ValueError:
                diag["http_status"] = response.status_code
                diag["content_chars"] = 0
                diag["reasoning_chars"] = 0
                _log_unusable("invalid_json", diag, model=cfg["model"])
                result = _apply_reader_note(
                    empty_judgment(
                        configured=True,
                        error="invalid_json",
                        model=cfg["model"],
                        diagnostics=diag,
                    )
                )
                if _filled_score(result) > best_score:
                    best, best_score = result, _filled_score(result)
                if attempt >= MAX_MODEL_CALLS:
                    break
                continue
            text, parsed_diag = interpret_chat_completion(raw)
            diag.update(parsed_diag)
            diag["http_status"] = response.status_code
            parsed = _extract_json(text) if text.strip() else None
            normalized = (
                normalize_judgment(parsed, payload) if parsed is not None else None
            )
            kind, missing = _unusable_kind(text, parsed, normalized, diag, payload)
            if missing:
                diag["missing_sections"] = missing
            if kind is None:
                if diag.get("finish_reason") == "length":
                    diag["truncated"] = True
                diag["recovered"] = attempt > 1
                return {
                    "llm_configured": True,
                    "model": cfg["model"],
                    **(normalized or {}),
                    "error": None,
                    "diagnostics": diag,
                }
            _log_unusable(kind, diag, model=cfg["model"])
            result = _apply_reader_note(
                empty_judgment(
                    configured=True,
                    error=kind,
                    model=cfg["model"],
                    diagnostics=diag,
                )
            )
            if normalized:
                for key, value in normalized.items():
                    result[key] = value
                result = _apply_reader_note(result)
            score = _filled_score(result)
            if score > best_score:
                best, best_score = result, score
            if attempt >= MAX_MODEL_CALLS:
                break
            if thinking is True and send_thinking:
                thinking = False
                continue
            if tokens < ceiling and (
                diag.get("finish_reason") == "length" or kind == "incomplete_fields"
            ):
                tokens = _next_budget(tokens, ceiling)
                continue
        chosen = best or empty_judgment(
            configured=True, error="empty_content", model=cfg["model"]
        )
        chosen.setdefault("diagnostics", {})["attempts"] = attempt
        return _apply_reader_note(chosen)
    finally:
        if own_client:
            http.close()


_FONT_OPEN = re.compile(
    r"<\s*font\b[^>]*color\s*=\s*['\"]#?(1F4D38|9C2B1B|8A5A12)['\"][^>]*>",
    re.IGNORECASE,
)
_BOLD_FACE = re.compile(
    r"<\s*font\b(?![^>]*\bcolor\s*=)[^>]*face\s*=\s*['\"]YaHei-Bold['\"][^>]*>",
    re.IGNORECASE,
)
_FONT_CLOSE = re.compile(r"<\s*/\s*font\s*>", re.IGNORECASE)
_BOLD_OPEN = re.compile(r"<\s*b\s*>", re.IGNORECASE)
_BOLD_CLOSE = re.compile(r"<\s*/\s*b\s*>", re.IGNORECASE)


def _has_wording(narrative: dict[str, Any]) -> bool:
    return bool(
        narrative.get("summary")
        or narrative.get("changes")
        or narrative.get("conclusion")
        or narrative.get("field_notes")
        or narrative.get("metric_notes")
        or narrative.get("infield")
        or narrative.get("surrounding")
        or narrative.get("weather")
    )


def section_lines(narrative: dict[str, Any] | None, key: str) -> list[str]:
    """Model sentences for one report section. Empty when the model did not write them."""
    return [_present(item) for item in (narrative or {}).get(key) or []]


def infield_line(narrative: dict[str, Any] | None, metric: str) -> list[str]:
    text = ((narrative or {}).get("infield") or {}).get(metric)
    return [_present(text)] if text else []


def judgment_paragraphs(narrative: dict[str, Any] | None) -> list[str]:
    """Render the sentences the model wrote, including the emphasis it chose."""
    if not narrative:
        return []
    items: list[str] = []
    summary = str(narrative.get("summary") or "").strip()
    if summary:
        items.append(_present(summary))
    for row in narrative.get("changes") or []:
        sentence = str(row.get("sentence") or "").strip()
        if sentence:
            items.append(_present(sentence))
    return items


def _present(value: str) -> str:
    """Keep bold and the three palette colors the model applied. Do not choose the words."""
    slots: list[str] = []

    def stash(html: str) -> str:
        slots.append(html)
        return f"\u0000{len(slots) - 1}\u0000"

    text = _FONT_OPEN.sub(lambda match: stash(match.group(0)), value)
    text = _BOLD_FACE.sub(lambda match: stash(match.group(0)), text)
    text = _FONT_CLOSE.sub(lambda _match: stash("</font>"), text)
    text = _BOLD_OPEN.sub(lambda _match: stash("<b>"), text)
    text = _BOLD_CLOSE.sub(lambda _match: stash("</b>"), text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\u0000(\d+)\u0000", lambda match: slots[int(match.group(1))], text)
