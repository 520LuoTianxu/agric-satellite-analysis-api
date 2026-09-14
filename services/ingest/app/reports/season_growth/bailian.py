# -*- coding: utf-8 -*-
"""Alibaba Bailian (DashScope compatible-mode) chat client for season reports."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

DEFAULT_BASE_URL = (
    "https://llm-7cudikcfvgf9l1hy.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_MODEL = "qwen3.7-flash"

AI_FAIL = "AI 分析失败"

SYSTEM_PROMPT = """你是资深农学与遥感分析助手，撰写面向农户与农技人员的中文「生育期长势分析报告」解读。
只能基于用户提供的 JSON 事实撰写；不得编造数值、日期、景数、等级、百分比或田间事实。

硬性规则：
1. 程序拥有全部数字（NDVI/NDMI/EVI/MNDWI/VV/VH、日期、景数、等级、收获）。你只解读，不重算、不发明、不改写程序分数。
2. 不得编造天气、播种、品种、土壤、产量、墒情、成熟度；缺失则写「未提供，需进一步确认」。
3. 语气必须谨慎：使用 提示/可能/疑似/需进一步确认。禁止虚假因果。
4. 不能仅凭 NDVI 推断产量损失或写「生物量积累达标」「生物量达标」；应写冠层绿度。
5. 九月 NDVI 下降与干旱等级共现：只能写「成熟脱水与天气偏干可能同时存在」，缺少土壤/气象资料时不能定量。
6. 收获：若程序置信度为低，必须写「疑似进入成熟后期或收获准备阶段」并强调需田间确认；禁止「立即收割」「收获窗口开启」。
7. 同比：只能写「峰值日期提前/推后 N 天」；禁止「生育进程提前一个月」或推断播种/积温；峰值日期提前≠物候提前相应天数。
8. 严禁散文出现英文字段名/JSON 键（如 flood_scene_count、status=ok、detected、low）。
9. 物候阶段为估计，不得写成实测播种日期。
10. 禁止用语：排水良好、排水条件良好、无渍涝隐患、立即收割、干旱风险提示偏高、生物量达标、温光（无数据时）、降水偏少（无数据时）。
10b. 禁止产品升级/平台介绍/未来功能宣传；只写农学解读与田间建议。
10c. 多因子推理：异常须结合阶段+天气/水分+土壤（若有）排序可能原因；禁止单指数下结论。
11. 输出必须是合法 JSON，至少包含键：
    core_conclusion, synthesis, timeline_bullets, monthly_notes,
    conclusions, factors_strong, factors_mid, factors_weak,
    actions_now, actions_week, actions_next_season, evidence_gaps。
    可增加额外键（extensible），但不得省略上述键，不得用其重算程序数字。
12. 字段分工（禁止互相复读同一段）：
    - core_conclusion：60–90字，一句核心判断（谨慎，引用程序事实）。
    - synthesis：120–180字，综合回答：①当前冠层绿度？②是否提示干旱？③是否提示洪涝？④是否疑似成熟后期/收获准备？⑤与上年峰值日期差？⑥还缺哪些证据？
    - timeline_bullets：3–5条按月短句（6–9月）。
    - monthly_notes：与 facts.timeline 月份对齐，每条≤2行/≤80字。
    - conclusions：3–4条综合结论（勿复述数字清单；勿写排水良好等禁语）。
    - factors_strong：仅可复述程序已观察事实（景数/等级/日期），勿写温光/降水偏少。
    - factors_mid：较可能的解释（谨慎）。
    - factors_weak：暂不能判断（资料不足，不能认定…）。
    - actions_now：田间核查清单；actions_week：7日监测；actions_next_season：下一季农艺（拔节–抽雄/灌浆灌溉、雨季排水），严禁遥感作业建议。
    - evidence_gaps：需要补充的证据。
13. actions_next_season 必须是实用农事建议；严禁无人机/多源卫星/云量/补测频次等遥感作业改进。
14. 不要输出 one_liner / summary / evidence_bullets 等重复块。"""

_AI_LIST_KEYS = (
    "timeline_bullets",
    "monthly_notes",
    "conclusions",
    "factors_strong",
    "factors_mid",
    "factors_weak",
    "evidence_gaps",
)
_AI_STR_KEYS = (
    "core_conclusion",
    "synthesis",
    "actions_now",
    "actions_week",
    "actions_next_season",
)

# Legacy keys still accepted as fallbacks when the model returns the old schema.
_LEGACY_MAP = {
    "one_liner": "core_conclusion",
    "summary": "synthesis",
    "interpretation": "synthesis",
    "moisture_analysis": "synthesis",
    "recommendations": "actions_now",
    "follow_up": "evidence_gaps",
    "causes_ranked": "factors_mid",
    "timeline_notes": "timeline_bullets",
}


def bailian_configured() -> bool:
    return bool((os.environ.get("BAILIAN_API_KEY") or "").strip())


def bailian_settings() -> dict[str, str]:
    """Read Bailian config from process env only (secrets never hard-coded)."""
    return {
        "api_key": (os.environ.get("BAILIAN_API_KEY") or "").strip(),
        "base_url": (os.environ.get("BAILIAN_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        ),
        "model": (os.environ.get("BAILIAN_MODEL") or DEFAULT_MODEL).strip()
        or DEFAULT_MODEL,
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [s] if s else []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, dict):
        # month -> note
        out: list[str] = []
        for k in sorted(value.keys(), key=lambda x: str(x)):
            v = value.get(k)
            if v is None:
                continue
            s = str(v).strip()
            if s:
                out.append(s if str(k) in s else f"{k} {s}")
        return out
    return []


def _as_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        joined = "\n".join(str(x) for x in value if str(x).strip())
        return joined.strip() or None
    s = str(value).strip()
    return s or None


def _clip(text: str | None, max_chars: int) -> str | None:
    if not text:
        return text
    t = text.strip()
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1] + "…"


def _program_next_season_from_facts(facts: dict[str, Any] | None) -> str:
    """Build agronomic next-season placeholder from program facts (soft-fail safe)."""
    try:
        from app.reports.season_growth.facts import program_next_season_actions

        facts = facts or {}
        return program_next_season_actions(
            drought=facts.get("drought")
            if isinstance(facts.get("drought"), dict)
            else {},
            flood=facts.get("flood") if isinstance(facts.get("flood"), dict) else {},
            harvest=facts.get("harvest")
            if isinstance(facts.get("harvest"), dict)
            else None,
        )
    except Exception:
        return (
            "基于本季遥感干旱/水分格局，下一季宜提前准备灌溉与排水能力，"
            "并在拔节–抽雄、灌浆等关键阶段安排墒情检查；记录播种日期、品种与产量以便解读"
            "（需结合当地确认）。"
        )


def _looks_like_remote_ops_local(t: str | None) -> bool:
    """Minimal fallback when facts module cannot import."""
    s = (t or "").strip()
    if not s:
        return False
    keys = ("无人机", "多源卫星", "补测频次", "云量", "遥感作业", "卫星补测")
    return any(k in s for k in keys)


def _sanitize_next_season(text: str | None, facts: dict[str, Any] | None) -> str | None:
    """Drop remote-sensing-ops advice; fall back to program agronomy."""
    try:
        from app.reports.season_growth.facts import looks_like_remote_ops_advice
    except Exception:
        looks_like_remote_ops_advice = _looks_like_remote_ops_local  # type: ignore

    raw = str(text).strip() if text is not None else ""
    if not raw or looks_like_remote_ops_advice(raw):
        return _program_next_season_from_facts(facts)
    # Also reject if any line is remote-ops (mixed cards).
    for ln in raw.splitlines():
        if looks_like_remote_ops_advice(ln):
            return _program_next_season_from_facts(facts)
    return raw


def _empty_ai_fields() -> dict[str, Any]:
    return {
        "core_conclusion": None,
        "synthesis": None,
        "timeline_bullets": [],
        "monthly_notes": [],
        "conclusions": [],
        "factors_strong": [],
        "factors_mid": [],
        "factors_weak": [],
        "actions_now": None,
        "actions_week": None,
        "actions_next_season": None,
        "evidence_gaps": [],
        # kept so older callers/tests do not KeyError
        "one_liner": None,
        "summary": None,
        "evidence_bullets": [],
        "moisture_analysis": None,
        "interpretation": None,
        "causes_ranked": [],
        "recommendations": None,
        "follow_up": [],
        "timeline_notes": None,
    }


def _merge_legacy(obj: dict[str, Any]) -> dict[str, Any]:
    merged = dict(obj)
    for old, new in _LEGACY_MAP.items():
        if merged.get(new) in (None, "", []):
            if obj.get(old) not in (None, "", []):
                merged[new] = obj.get(old)
    return merged


def _normalize_ai(obj: dict[str, Any] | None) -> dict[str, Any]:
    if not obj:
        out = _empty_ai_fields()
        out["llm_configured"] = False
        out["error"] = "empty_response"
        return out
    obj = _merge_legacy(obj)
    out = _empty_ai_fields()
    for k in _AI_STR_KEYS:
        out[k] = _as_str_or_none(obj.get(k))
    for k in _AI_LIST_KEYS:
        out[k] = _as_str_list(obj.get(k))
    out["core_conclusion"] = _clip(out.get("core_conclusion"), 90)
    # keep a little headroom over 250 for punctuation
    if out.get("synthesis") and len(out["synthesis"]) > 200:
        out["synthesis"] = _clip(out["synthesis"], 180)
    # legacy mirrors for any leftover callers
    out["one_liner"] = out.get("core_conclusion")
    out["summary"] = out.get("synthesis")
    out["interpretation"] = out.get("synthesis")
    out["recommendations"] = out.get("actions_now")
    out["follow_up"] = list(out.get("evidence_gaps") or [])
    out["causes_ranked"] = list(out.get("factors_mid") or [])
    out["timeline_notes"] = "\n".join(out.get("timeline_bullets") or []) or None
    out["llm_configured"] = True
    out["error"] = None
    return out


def missing_llm_sections() -> dict[str, Any]:
    note = "大模型未配置（缺少 BAILIAN_API_KEY），本报告仅含程序计算的遥感事实与图表。"
    out = _empty_ai_fields()
    out.update(
        {
            "core_conclusion": f"遥感事实已生成（{AI_FAIL}：未配置）",
            "synthesis": note,
            "actions_now": "请配置 BAILIAN_API_KEY 后重新生成以获得 AI 解读与建议。",
            "actions_week": "未来7天结合田间墒情与植株状态安排农事，不宜仅凭遥感定夺。",
            "actions_next_season": (
                "基于本季遥感干旱/水分格局，下一季宜提前准备灌溉与排水能力，"
                "并在拔节–抽雄、灌浆等关键阶段安排墒情检查；记录播种日期、品种与产量以便解读"
                "（需结合当地确认）。"
            ),
            "one_liner": f"遥感事实已生成（{AI_FAIL}：未配置）",
            "summary": note,
            "interpretation": note,
            "recommendations": "请配置 BAILIAN_API_KEY 后重新生成以获得 AI 解读与建议。",
            "llm_configured": False,
            "error": "missing_api_key",
        }
    )
    return out


def generate_season_narrative(
    facts: dict[str, Any],
    material_text: str = "",
    *,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Call Bailian chat/completions; return normalized AI sections.

    Soft-fails to missing_llm_sections / error payload — never invents numbers.
    """
    cfg = bailian_settings()
    if not cfg["api_key"]:
        out = missing_llm_sections()
        out["actions_next_season"] = _sanitize_next_season(
            out.get("actions_next_season"), facts
        )
        return out

    user_payload = {
        "facts": facts,
        "materials_excerpt": (material_text or "")[:6000],
    }
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "请根据以下 JSON 事实撰写生育期长势解读，仅输出 JSON：\n"
                    + json.dumps(user_payload, ensure_ascii=False)
                ),
            },
        ],
        "temperature": 0.2,
    }
    url = f"{cfg['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    own_client = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get(
            "content"
        ) or ""
        parsed = _extract_json(content)
        out = _normalize_ai(parsed)
        out["actions_next_season"] = _sanitize_next_season(
            out.get("actions_next_season"), facts
        )
        if not out.get("core_conclusion") and not out.get("synthesis"):
            out["error"] = out.get("error") or "unparseable_response"
            out["raw_excerpt"] = str(content)[:500]
        return out
    except Exception as exc:
        detail = str(exc)[:500]
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            detail = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        elif isinstance(exc, httpx.TimeoutException):
            detail = f"timeout after {timeout}s: {type(exc).__name__}"
        note = f"{AI_FAIL}：大模型调用失败（{type(exc).__name__}）。报告仍包含程序计算事实。"
        out = _empty_ai_fields()
        out.update(
            {
                "core_conclusion": f"遥感事实已生成（{AI_FAIL}）",
                "synthesis": note,
                "actions_next_season": _program_next_season_from_facts(facts),
                "one_liner": f"遥感事实已生成（{AI_FAIL}）",
                "summary": note,
                "interpretation": note,
                "recommendations": None,
                "llm_configured": True,
                "error": detail,
            }
        )
        return out
    finally:
        if own_client:
            http.close()
