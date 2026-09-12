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
DEFAULT_MODEL = "qwen3.7flash"

SYSTEM_PROMPT = """你是农业遥感分析助手。你只能基于用户提供的 JSON 事实与材料文本撰写中文解读。
硬性规则：
1. 不得编造任何数值、日期、场景数、等级或百分比；只能引用 facts 中已有的数字。
2. 若某项缺失，明确写「数据不足」或「未提供」，不要猜测。
3. 输出必须是合法 JSON，键为：one_liner, summary, evidence_bullets, interpretation, recommendations。
4. one_liner：一句话总括（≤40字）。summary：2–4句摘要。evidence_bullets：字符串数组，每条引用具体事实。interpretation：生育期长势分析。recommendations：可执行建议（数组或分段文字）。
5. 语气专业、白话、面向农户与农技人员。"""


def bailian_configured() -> bool:
    return bool((os.environ.get("BAILIAN_API_KEY") or "").strip())


def bailian_settings() -> dict[str, str]:
    """Read Bailian config from process env only (secrets never hard-coded)."""
    return {
        "api_key": (os.environ.get("BAILIAN_API_KEY") or "").strip(),
        "base_url": (
            os.environ.get("BAILIAN_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/"),
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


def _normalize_ai(obj: dict[str, Any] | None) -> dict[str, Any]:
    if not obj:
        return {
            "one_liner": None,
            "summary": None,
            "evidence_bullets": [],
            "interpretation": None,
            "recommendations": None,
            "llm_configured": False,
            "error": "empty_response",
        }
    bullets = obj.get("evidence_bullets") or []
    if isinstance(bullets, str):
        bullets = [bullets]
    if not isinstance(bullets, list):
        bullets = []
    rec = obj.get("recommendations")
    if isinstance(rec, list):
        rec = "\n".join(str(x) for x in rec)
    return {
        "one_liner": (str(obj["one_liner"]).strip() if obj.get("one_liner") else None),
        "summary": (str(obj["summary"]).strip() if obj.get("summary") else None),
        "evidence_bullets": [str(b).strip() for b in bullets if str(b).strip()],
        "interpretation": (
            str(obj["interpretation"]).strip() if obj.get("interpretation") else None
        ),
        "recommendations": (str(rec).strip() if rec else None),
        "llm_configured": True,
        "error": None,
    }


def missing_llm_sections() -> dict[str, Any]:
    note = "大模型未配置（缺少 BAILIAN_API_KEY），本报告仅含程序计算的遥感事实与图表。"
    return {
        "one_liner": "遥感事实已生成（AI 摘要未启用）",
        "summary": note,
        "evidence_bullets": [],
        "interpretation": note,
        "recommendations": "请配置 BAILIAN_API_KEY 后重新生成以获得 AI 解读与建议。",
        "llm_configured": False,
        "error": "missing_api_key",
    }


def generate_season_narrative(
    facts: dict[str, Any],
    material_text: str = "",
    *,
    timeout: float = 60.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Call Bailian chat/completions; return normalized AI sections.

    Soft-fails to missing_llm_sections / error payload — never invents numbers.
    """
    cfg = bailian_settings()
    if not cfg["api_key"]:
        return missing_llm_sections()

    user_payload = {
        "facts": facts,
        "materials_excerpt": (material_text or "")[:8000],
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
        "response_format": {"type": "json_object"},
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
        content = (
            ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
            or ""
        )
        parsed = _extract_json(content)
        out = _normalize_ai(parsed)
        if not out.get("summary") and not out.get("one_liner"):
            out["error"] = out.get("error") or "unparseable_response"
            out["raw_excerpt"] = str(content)[:500]
        return out
    except Exception as exc:
        note = f"大模型调用失败：{type(exc).__name__}。报告仍包含程序计算事实。"
        return {
            "one_liner": "遥感事实已生成（AI 调用失败）",
            "summary": note,
            "evidence_bullets": [],
            "interpretation": note,
            "recommendations": None,
            "llm_configured": True,
            "error": str(exc)[:500],
        }
    finally:
        if own_client:
            http.close()
