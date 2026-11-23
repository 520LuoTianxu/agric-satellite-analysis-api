"""模型撰写双周报里「往好 / 往差」的文字说明。

数值、日期和升高、降低由程序算好后传给模型。模型只写判断和说明，
不能改数字，也不能新增事实里没有的数字。代码不根据涨跌或生育期决定好坏。
模型名、地址和密钥只从 BAILIAN_MODEL、BAILIAN_BASE_URL、BAILIAN_API_KEY 读取。
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Any

from agric_satellite_analysis_common.biweekly.metric_specs import (
    METRIC_SPECS,
    metric_abbr,
)
from agric_satellite_analysis_common.biweekly.phase_policy import STAGE_BACKGROUND

DEFAULT_BASE_URL = (
    "https://llm-7cudikcfvgf9l1hy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_MODEL = "qwen3.7-flash"
JUDGMENTS = {"better", "worse", "unclear"}
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

SYSTEM_PROMPT = """你在写农业遥感双周报的正文。用户消息里有 parameter_guide 和本期参数。parameter_guide 说明每个参数是什么意思，其余字段是程序已经算好的参数值。

请直接根据这些参数发挥，写成给种植户看的报告。自己组织句子，不要套“若升高就写某一句”的填空，也不要只写“全部上升、方向一致”就把参数丢掉。参数里有的指标、日期、田内部位、未采用观测、气象和周边对照，都写进对应段落；某个参数为空就不要编。

数值升高或降低只表示大小，不等于好坏。往好、往差、不足以判断由你阅读参数后决定。

版式：正文是雅黑 10 磅。需要强调时只用下面几种：
- 指标名和这句里的关键数值加粗，保持黑色：<b>…</b>（雅黑粗体）
- 往好变化：<font color='#1F4D38'><b>往好变化</b></font>
- 往差变化：<font color='#9C2B1B'><b>往差变化</b></font>
- 不足以判断：<font color='#4A4A4A'><b>不足以判断</b></font>
不要用别的颜色，不要写出字体名称，不要用 Markdown。

数字和日期只能照抄参数里已经出现的写法。不要写「低值」「参考线」「预警线」「达标」「确诊」「灾损」「产量」「好转」「变差」，也不要提到模型或 AI。
只输出 JSON：
{"summary":"一段话","changes":[{"metric":"ndvi","judgment":"better|worse|unclear","sentence":"你写的句子"}],"conclusion":["你写的句子"],"field_notes":["你写的句子"],"infield":[{"metric":"ndre","text":"你写的句子"}],"surrounding":["你写的句子"],"weather":["你写的句子"]}
changes 和 infield 的 metric 只能是 metrics 里的键。judgment 只能是 better、worse、unclear，并和句中的颜色用语一致。
"""

# 跟参数一起交给模型，只解释含义，不另给可以引用的数字。
PARAMETER_GUIDE = {
    "metrics": "每个指标的全田结果。full_name 和 meaning 说明这个指标反映什么。latest 是最近一次有效观测的数值，baseline 是拿来对比的上一次，delta 是 latest 减 baseline。value_direction 只表示数值升高、降低或持平。statistic 是中位数或均值。role 是本期主要指标还是辅助指标。",
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
        "infield": infield,
        "surrounding": _clean_list((parsed or {}).get("surrounding"), blob),
        "weather": _clean_list((parsed or {}).get("weather"), blob),
    }


def empty_judgment(
    *, configured: bool, error: str | None = None, model: str | None = None
) -> dict[str, Any]:
    return {
        "llm_configured": configured,
        "model": model,
        "summary": "",
        "changes": [],
        "conclusion": [],
        "field_notes": [],
        "infield": {},
        "surrounding": [],
        "weather": [],
        "error": error,
    }


def generate_metric_judgment(
    facts: dict[str, Any],
    *,
    timeout: float = 60.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Ask the configured model for better/worse wording. No key or failure leaves it empty."""
    cfg = bailian_settings()
    payload = narrative_input(facts)
    if not payload["metrics"]:
        return empty_judgment(
            configured=bool(cfg["api_key"]), error="no_metrics", model=cfg["model"]
        )
    if not cfg["api_key"]:
        return empty_judgment(configured=False, error="not_configured", model=None)
    body = {
        "model": cfg["model"],
        "temperature": 0.4,
        "max_tokens": 1800,
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

    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.post(
            f"{cfg['base_url']}/chat/completions", headers=headers, json=body
        )
        response.raise_for_status()
        content = (
            (response.json().get("choices") or [{}])[0].get("message") or {}
        ).get("content") or ""
        normalized = normalize_judgment(_extract_json(content), payload)
        if not _has_wording(normalized):
            return empty_judgment(
                configured=True, error="unparseable_response", model=cfg["model"]
            )
        return {
            "llm_configured": True,
            "model": cfg["model"],
            **normalized,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — 模型失败时不改用代码判断好坏
        return empty_judgment(
            configured=True, error=type(exc).__name__, model=cfg["model"]
        )
    finally:
        if own_client:
            http.close()


_LABELS = {
    "better": ("往好变化", "#1F4D38"),
    "worse": ("往差变化", "#9C2B1B"),
    "unclear": ("不足以判断", "#4A4A4A"),
}
_FONT_OPEN = re.compile(
    r"<\s*font\s+color\s*=\s*['\"]#?(1F4D38|9C2B1B|4A4A4A)['\"]\s*>",
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
    """Show the model's own sentences. Add the color only when the model left it out."""
    if not narrative:
        return []
    items: list[str] = []
    summary = str(narrative.get("summary") or "").strip()
    if summary:
        items.append(_present(summary))
    for row in narrative.get("changes") or []:
        label, color = _LABELS.get(row.get("judgment"), _LABELS["unclear"])
        sentence = str(row.get("sentence") or "").strip()
        if label not in sentence:
            name = metric_abbr(str(row.get("metric") or ""))
            mark = f"<font color='{color}'><b>{label}</b></font>"
            sentence = f"<b>{name}</b> {mark}。{sentence}".strip()
        items.append(_present(sentence))
    return items


def _present(value: str) -> str:
    """Keep the bold and the three report colors the model was told to use."""
    slots: list[str] = []

    def stash(html: str) -> str:
        slots.append(html)
        return f"\u0000{len(slots) - 1}\u0000"

    text = _FONT_OPEN.sub(
        lambda match: stash(f"<font color='#{match.group(1).upper()}'>"), value
    )
    text = _FONT_CLOSE.sub(lambda _match: stash("</font>"), text)
    text = _BOLD_OPEN.sub(lambda _match: stash("<b>"), text)
    text = _BOLD_CLOSE.sub(lambda _match: stash("</b>"), text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\u0000(\d+)\u0000", lambda match: slots[int(match.group(1))], text)
    for phrase, color in _LABELS.values():
        wrapped = f"<font color='{color}'><b>{phrase}</b></font>"
        parts = text.split(wrapped)
        fixed = []
        for part in parts:
            part = part.replace(f"<b>{phrase}</b>", wrapped)
            chunks = part.split(wrapped)
            part = wrapped.join(chunk.replace(phrase, wrapped) for chunk in chunks)
            fixed.append(part)
        text = wrapped.join(fixed)
    return text
