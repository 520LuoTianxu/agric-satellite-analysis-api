"""模型撰写双周报里「往好 / 往差」的文字说明。

数值、日期和升高、降低由程序算好后传给模型。模型只写判断和说明，
不能改数字，也不能新增事实里没有的数字。代码不根据涨跌或生育期决定好坏。
模型名、地址和密钥只从 BAILIAN_MODEL、BAILIAN_BASE_URL、BAILIAN_API_KEY 读取。
补全长度和等待时间从 BAILIAN_MAX_TOKENS、BAILIAN_TIMEOUT_S 读取，并夹在固定上下界里。
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
# deepseek-v4.1-flash-1017 把思考算进 completion tokens。隔离测量里
# max_tokens=1800 得到 HTTP 200、finish_reason=length、completion_tokens=1800、
# content 为空、reasoning_content 约 4249 字，思考用尽预算后没有 JSON。
# 8192 是同一次测量里结论、核查、变化和气象能返回的预算，用作默认值，
# 不是生产 P18 的回放。BAILIAN_MAX_TOKENS 可改，但夹在 4096–16384：
# 下限高于这次失败的 1800，给思考之后的 JSON 留余量；上限避免误配成无界补全。
# 这次空正文是 length，不是超时。更大的补全需要更长的客户端等待。
# BAILIAN_TIMEOUT_S 默认 120 秒，也是那次测量里请求能完成的值，夹在 30–300。
DEFAULT_MAX_TOKENS = 8192
MIN_MAX_TOKENS = 4096
MAX_MAX_TOKENS = 16384
DEFAULT_TIMEOUT_S = 120
MIN_TIMEOUT_S = 30
MAX_TIMEOUT_S = 300
JUDGMENTS = {"better", "worse", "unclear"}
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

SYSTEM_PROMPT = """你在写农业遥感双周报的正文。用户消息里有 parameter_guide 和本期参数。parameter_guide 说明每个参数是什么意思，其余字段是程序已经算好的参数值。

请直接根据这些参数发挥，写成给种植户看的报告。自己组织句子，不要每条都写成“由A升到B，提高C”这一种句式，也不要只写“全部上升、方向一致”。参数里有的指标、日期、田内部位、未采用观测、气象和周边对照都要写到；某个参数为空就不要编。不要写灌溉、施肥、打药这类处方。

数值升高或降低只表示大小，不等于好坏。哪一句、哪个数字、哪个部位是重点，由你决定，再给那几个字加粗或上色。不要规定成某几个固定词，也不要每句末尾都补同一个词。

正文是雅黑 10 磅。你可以用这些标记，标记里面放你选中的字，不要把整句都包进去：
- 加粗：<b>…</b>
- 你认为往好处、值得注意的字：<font face="YaHei-Bold" color="#1F4D38" backColor="#D9F2E3">…</font>
- 你认为往差处、需要留意的字：<font face="YaHei-Bold" color="#9C2B1B" backColor="#F8D6D2">…</font>
- 你认为还不能下判断的字：<font face="YaHei-Bold" color="#8A5A12" backColor="#F8E8C8">…</font>
没有把握的地方保持正文。不要用别的颜色，不要用 Markdown。

metric_notes 是「指标说明」：根据每个指标的 full_name、meaning 和本期数值，写成这个指标在本期说明了什么。不要复读定义原文，每个指标的写法不要相同，重点由你标。
conclusion 是给种植户看的结论，重点由你用上面的标记标出来。
数字和日期只能照抄参数里已经出现的写法。不要写「低值」「参考线」「预警线」「达标」「确诊」「灾损」「产量」「好转」「变差」，也不要提到模型或 AI。
只输出 JSON：
{"summary":"一段话","changes":[{"metric":"ndvi","judgment":"better|worse|unclear","sentence":"带标记的句子"}],"conclusion":["带标记的句子"],"field_notes":["带标记的句子"],"metric_notes":["带标记的句子"],"infield":[{"metric":"ndre","text":"带标记的句子"}],"surrounding":["带标记的句子"],"weather":["带标记的句子"]}
changes 和 infield 的 metric 只能是 metrics 里的键。judgment 只能是 better、worse、unclear。句中的颜色由你选，不必和某个固定词对应。metric_notes 覆盖 metrics 里的每一个指标。
"""

# 跟参数一起交给模型，只解释含义，不另给可以引用的数字。
PARAMETER_GUIDE = {
    "metrics": "每个指标的全田结果。full_name、meaning 和 formula 说明这个指标是什么、怎么计算。latest 是最近一次有效观测的数值，baseline 是拿来对比的上一次，delta 是 latest 减 baseline。value_direction 只表示数值升高、降低或持平。statistic 是中位数或均值。role 是本期主要指标还是辅助指标。",
    "used_observations": "本期采用的观测日期。",
    "rejected_observations": "卫星过境但没有采用的日期。reason 是未采用的原因，cloud_pct 是地块云量的百分比。",
    "infield": "田块内部差异。median、p10、p90 是有效像元的中位数和第10、第90百分位。flagged_pixels 是明显低于本田整体水平的像元个数，clustered_pixels 是其中连成片的个数。regions.name 是成片像元所在部位，share_percent 是该部位占成片像元的百分比。",
    "surrounding": "本田和周边约一公里农田的超绿指数。difference_exg 是本田减周边，正数表示本田更高。cover_2km_percent 是影像盖住周边两公里范围的百分比。",
    "weather": "地块中心的格点气象，不是田间实测。precipitation_mm 是降水量毫米，et0_mm 是参考作物蒸散量毫米。带 days 的字段是有记录的天数，缺测的天没有算进合计。",
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


def completion_budget() -> tuple[int, bool]:
    """Tokens requested for one completion, after the floor and ceiling."""
    return _bounded_int(
        "BAILIAN_MAX_TOKENS", DEFAULT_MAX_TOKENS, MIN_MAX_TOKENS, MAX_MAX_TOKENS
    )


def completion_timeout_s(explicit: float | None = None) -> tuple[float, bool]:
    """Client wait. An explicit value is clamped; otherwise the env or default is used."""
    if explicit is not None:
        try:
            value = int(explicit)
        except (TypeError, ValueError):
            return float(DEFAULT_TIMEOUT_S), True
        applied = min(MAX_TIMEOUT_S, max(MIN_TIMEOUT_S, value))
        return float(applied), applied != value
    seconds, clamped = _bounded_int(
        "BAILIAN_TIMEOUT_S", DEFAULT_TIMEOUT_S, MIN_TIMEOUT_S, MAX_TIMEOUT_S
    )
    return float(seconds), clamped


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


def _unusable_kind(
    text: str,
    parsed: dict[str, Any] | None,
    normalized: dict[str, Any] | None,
    diag: dict[str, Any],
) -> str | None:
    """Name why this completion cannot be report prose. None means the text is usable."""
    length = diag.get("finish_reason") == "length"
    if not text.strip():
        if length:
            return "truncated_response"
        if diag.get("reasoning_chars"):
            return "empty_with_reasoning"
        return "empty_content"
    if parsed is None:
        return "truncated_response" if length else "invalid_json"
    if not _has_wording(normalized or {}):
        return "truncated_response" if length else "empty_fields"
    return None


def _log_unusable(error: str, diag: dict[str, Any], *, model: str) -> None:
    # 只记长度和原因。不记密钥、正文或 reasoning 原文。
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
    )


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
                "name": spec.abbr if spec else metric_abbr(key),
                "full_name": spec.name if spec else metric_abbr(key),
                "meaning": spec.purpose if spec else "",
                "formula": spec.formula if spec else "",
                "role": "主要" if role == "primary" else "辅助",
                "latest": _index(item.get("latest")),
                "baseline": _index(item.get("baseline")),
                "delta": _index(item.get("delta")),
                "value_direction": {"up": "升高", "down": "降低", "flat": "持平"}.get(
                    item.get("direction") or ""
                ),
                "statistic": (
                    "中位数"
                    if item.get("statistic") == "median"
                    else "均值"
                    if item.get("statistic") == "mean"
                    else None
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
                "valid_percent": _whole((stats.get("valid_fraction") or 0) * 100)
                if stats.get("valid_fraction") is not None
                else None,
                "flagged_pixels": _whole(relative.get("flagged_pixels")),
                "clustered_pixels": _whole(relative.get("clustered_pixels")),
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
                "field_exg": _index(row.get("field")),
                "surrounding_1km_exg": _index(row.get("surrounding_1km")),
                "difference_exg": _index(row.get("difference")),
            }
        )
    cover = meta.get("cover_2km")
    return {
        "comparisons": rows,
        "cover_2km_percent": None if cover is None else _whole(float(cover) * 100),
    }


def _weather_facts(facts: dict[str, Any]) -> dict[str, Any]:
    period = facts.get("weather_period") or {}
    antecedent = facts.get("weather_antecedent") or {}
    return {
        "period_days": period.get("days"),
        "precipitation_mm": _index(period.get("precipitation_mm")),
        "precipitation_days": period.get("precipitation_days"),
        "et0_mm": _index(period.get("et0_mm")),
        "et0_days": period.get("et0_days"),
        "antecedent_days": antecedent.get("days"),
        "antecedent_precipitation_mm": _index(antecedent.get("precipitation_mm")),
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


def _clean_sentence(sentence: str, blob: str, limit: int) -> str:
    text = sentence.strip()
    if (
        not text
        or any(word in text for word in _BANNED)
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


def generate_metric_judgment(
    facts: dict[str, Any],
    *,
    timeout: float | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    """Ask the configured model for wording. A failed body stays empty and named.

    Does not raise. Does not copy reasoning into the report. Does not invent
    a sentence when the body is missing, cut off, or not JSON.
    """
    cfg = bailian_settings()
    payload = narrative_input(facts)
    if not payload["metrics"]:
        return empty_judgment(
            configured=bool(cfg["api_key"]), error="no_metrics", model=cfg["model"]
        )
    if not cfg["api_key"]:
        return empty_judgment(configured=False, error="not_configured", model=None)
    max_tokens, tokens_clamped = completion_budget()
    timeout_s, timeout_clamped = completion_timeout_s(timeout)
    body = {
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
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    own_client = client is None
    import httpx

    http = client or httpx.Client(timeout=timeout_s)
    try:
        response = http.post(
            f"{cfg['base_url']}/chat/completions", headers=headers, json=body
        )
        response.raise_for_status()
        try:
            raw = response.json()
        except ValueError:
            diag = {
                "http_status": response.status_code,
                "max_tokens": max_tokens,
                "max_tokens_clamped": tokens_clamped,
                "timeout_s": timeout_s,
                "timeout_clamped": timeout_clamped,
                "content_chars": 0,
                "reasoning_chars": 0,
            }
            _log_unusable("invalid_json", diag, model=cfg["model"])
            return empty_judgment(
                configured=True,
                error="invalid_json",
                model=cfg["model"],
                diagnostics=diag,
            )
        text, diag = interpret_chat_completion(raw)
        diag["http_status"] = response.status_code
        diag["max_tokens"] = max_tokens
        diag["max_tokens_clamped"] = tokens_clamped
        diag["timeout_s"] = timeout_s
        diag["timeout_clamped"] = timeout_clamped
        parsed = _extract_json(text) if text.strip() else None
        normalized = normalize_judgment(parsed, payload) if parsed is not None else None
        kind = _unusable_kind(text, parsed, normalized, diag)
        if kind:
            _log_unusable(kind, diag, model=cfg["model"])
            return empty_judgment(
                configured=True, error=kind, model=cfg["model"], diagnostics=diag
            )
        if diag.get("finish_reason") == "length":
            diag["truncated"] = True
        return {
            "llm_configured": True,
            "model": cfg["model"],
            **(normalized or {}),
            "error": None,
            "diagnostics": diag,
        }
    except httpx.TimeoutException:
        diag = {
            "max_tokens": max_tokens,
            "max_tokens_clamped": tokens_clamped,
            "timeout_s": timeout_s,
            "timeout_clamped": timeout_clamped,
        }
        _log_unusable("timeout", diag, model=cfg["model"])
        return empty_judgment(
            configured=True, error="timeout", model=cfg["model"], diagnostics=diag
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        diag = {
            "http_status": status,
            "max_tokens": max_tokens,
            "max_tokens_clamped": tokens_clamped,
            "timeout_s": timeout_s,
        }
        _log_unusable("http_error", diag, model=cfg["model"])
        return empty_judgment(
            configured=True, error="http_error", model=cfg["model"], diagnostics=diag
        )
    except Exception as exc:  # noqa: BLE001 — 模型失败时不改用代码判断好坏
        diag = {"exception_type": type(exc).__name__, "max_tokens": max_tokens}
        _log_unusable(type(exc).__name__, diag, model=cfg["model"])
        return empty_judgment(
            configured=True,
            error=type(exc).__name__,
            model=cfg["model"],
            diagnostics=diag,
        )
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
