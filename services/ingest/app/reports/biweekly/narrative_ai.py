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

import httpx

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

SYSTEM_PROMPT = """你是农业遥感双周报的撰写人。只根据用户给出的 JSON 事实写说明。用具体、不重复的句子，不要套“若升高则写某句、若降低则写某句”的固定句式。

要求：
- 往好、往差、不足以判断只放在 changes.judgment。阶段背景只供参考，不是必须套用的公式。
- 不得改写、重算或新增事实里的数值、日期、地块和指标。句子里的数字只能照抄事实 JSON 里已经出现的数字。
- 不得使用「低值参考线」「预警线」「达标」「确诊」「灾损」「产量」「好转」「变差」。
- 某一类事实为空时，对应数组留空，不要编造田间位置、气象或缺测。
- 只输出 JSON，不要 Markdown。结构为：
  {"summary":"指标变化的一段话","changes":[{"metric":"ndvi","judgment":"better|worse|unclear","sentence":"不超过90字"}],"conclusion":["结论短句"],"field_notes":["可到田核对的现象，不写处方"],"infield":[{"metric":"ndre","text":"这张田内图说明什么"}],"surrounding":["周边对照说明"],"weather":["气象说明，缺测照实写"]}
- changes 和 infield 的 metric 只能是事实 metrics 里出现的键。judgment 只能是 better、worse、unclear。
"""


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
                "role": "主要" if role == "primary" else "辅助",
                "latest": item.get("latest"),
                "baseline": item.get("baseline"),
                "delta": item.get("delta"),
                "arithmetic_direction": item.get("direction"),
                "statistic": item.get("statistic"),
            }
    quality = facts.get("data_quality") or {}
    return {
        "period": period_text(facts),
        "stage": stage.get("text"),
        "stage_note": stage.get("source"),
        "stage_background": background.get("text"),
        "latest_date": comparison.get("latest_date"),
        "baseline_date": comparison.get("baseline_date"),
        "metrics": metrics,
        "used_observations": quality.get("used_dates"),
        "rejected_observations": quality.get("rejected"),
        "infield": _infield_facts(facts),
        "surrounding": _surrounding_facts(facts),
        "weather": _weather_facts(facts),
    }


def _infield_facts(facts: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in facts.get("infield") or []:
        stats = item.get("stats") or {}
        relative = item.get("relative") or {}
        rows.append(
            {
                "metric": item.get("metric"),
                "median": stats.get("median"),
                "p10": stats.get("p10"),
                "p90": stats.get("p90"),
                "valid_fraction": stats.get("valid_fraction"),
                "flagged_pixels": relative.get("flagged_pixels"),
                "clustered_pixels": relative.get("clustered_pixels"),
                "regions": [
                    region.get("name")
                    for region in relative.get("regions") or []
                    if region.get("name")
                ],
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
                "field_exg": row.get("field"),
                "surrounding_1km_exg": row.get("surrounding_1km"),
                "difference_exg": row.get("difference"),
            }
        )
    return {"comparisons": rows, "cover_2km": meta.get("cover_2km")}


def _weather_facts(facts: dict[str, Any]) -> dict[str, Any]:
    period = facts.get("weather_period") or {}
    antecedent = facts.get("weather_antecedent") or {}
    return {
        "period_days": period.get("days"),
        "precipitation_mm": period.get("precipitation_mm"),
        "precipitation_days": period.get("precipitation_days"),
        "et0_mm": period.get("et0_mm"),
        "et0_days": period.get("et0_days"),
        "antecedent_days": antecedent.get("days"),
        "antecedent_precipitation_mm": antecedent.get("precipitation_mm"),
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


def _numbers_are_copied(sentence: str, blob: str) -> bool:
    """A digit is allowed when the same value already appears in the facts JSON."""
    known = [float(token) for token in _NUMBER.findall(blob)]
    for token in _NUMBER.findall(sentence):
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


def _clean_sentence(sentence: str, blob: str, limit: int) -> str:
    text = sentence.strip()
    if not text or "低值参考线" in text or not _numbers_are_copied(text, blob):
        return ""
    return text[:limit]


def _clean_list(rows: Any, blob: str, limit: int = 160) -> list[str]:
    if isinstance(rows, str):
        rows = [rows]
    cleaned = []
    for row in rows or []:
        text = _clean_sentence(str(row or ""), blob, limit)
        if text:
            cleaned.append(text)
    return cleaned[:6]


def normalize_judgment(
    parsed: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """Keep model wording whose digits were copied. Drop anything the facts do not support."""
    blob = json.dumps(payload, ensure_ascii=False)
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
        if "低值参考线" in sentence or not _numbers_are_copied(sentence, blob):
            sentence = ""
        seen.add(metric)
        changes.append(
            {"metric": metric, "judgment": judgment, "sentence": sentence[:90]}
        )
    infield: dict[str, str] = {}
    for row in (parsed or {}).get("infield") or []:
        if not isinstance(row, dict):
            continue
        metric = str(row.get("metric") or "").lower()
        text = _clean_sentence(str(row.get("text") or ""), blob, 160)
        if metric in allowed and metric not in infield and text:
            infield[metric] = text
    summary = _clean_sentence(str((parsed or {}).get("summary") or ""), blob, 240)
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
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "请只根据这些事实撰写报告说明，仅输出 JSON：\n"
                + json.dumps(payload, ensure_ascii=False),
            },
        ],
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    own_client = client is None
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
    "unclear": ("不足以判断好坏", "#4A4A4A"),
}


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
    return [_paint(item) for item in (narrative or {}).get(key) or []]


def infield_line(narrative: dict[str, Any] | None, metric: str) -> list[str]:
    text = ((narrative or {}).get("infield") or {}).get(metric)
    return [_paint(text)] if text else []


def judgment_paragraphs(narrative: dict[str, Any] | None) -> list[str]:
    """Presentation only: color and bold follow the model's judgment, not a new rule."""
    if (
        not narrative
        or not narrative.get("changes")
        and not (narrative or {}).get("summary")
    ):
        return []
    items: list[str] = []
    summary = str((narrative or {}).get("summary") or "").strip()
    if summary:
        items.append(_paint(summary))
    for row in (narrative or {}).get("changes") or []:
        label, color = _LABELS.get(row.get("judgment"), _LABELS["unclear"])
        name = metric_abbr(str(row.get("metric") or ""))
        sentence = _paint(str(row.get("sentence") or ""))
        text = f"<font color='{color}'><b>{name} {label}</b></font>"
        if sentence:
            text += f"：{sentence}"
        items.append(text)
    return items


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _paint(value: str) -> str:
    """Bold and color the words the model already chose. This does not decide better or worse."""
    text = _escape(value)
    text = text.replace("往好变化", "<font color='#1F4D38'><b>往好变化</b></font>")
    text = text.replace("往差变化", "<font color='#9C2B1B'><b>往差变化</b></font>")
    return text
