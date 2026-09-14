# -*- coding: utf-8 -*-
"""Bailian AI layer for land-assessment PDFs.

Python owns all scores/charts/facts. AI only interprets (structured JSON).
Soft-fails to 「AI 分析失败」 placeholders — never recalculates scores.
"""

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

SYSTEM_PROMPT = """你是资深农学与遥感分析助手，撰写面向农户与农技人员的中文「选地体检」解读。
只能基于用户提供的 JSON 事实撰写；不得编造数值、分数、亩产、金额、肥料用量、灾害结论。

硬性规则：
1. 程序拥有全部数字（评分、NDVI/EVI、土壤、天气、景数）。你只解读，不重算、不发明分数或亩产。
2. 不得编造产量（亩产）、价格、成本、精确施肥量；无模型时产量只可写 高/中/低 或「数据不足」。
3. 语气谨慎：使用 提示/可能/疑似/需进一步确认。禁止虚假因果与无证据的灾害断言。
4. 区分「风险存在」与「灾害已发生」：没有硬证据（如明水面景、成灾记录）不得写已发生洪涝/旱灾。
5. 遥感长势禁止单指数下结论；须结合生育阶段+天气+水分+土壤，并给出排序可能原因。
6. 空间异常须写时间连续性 caveat：单景不能定论。
7. 土壤：指标→田间影响→管理方向；严禁给出具体 kg/亩施肥量（无农艺模型）。
8. 经营分析：无价格/成本/产量模型时不得写金额，写「数据不足」或省略金额块。
9. 禁止产品升级/平台介绍/未来功能宣传。
10. 输出必须是合法 JSON，顶层键：
    version, overall, portrait, score_explain, rs_growth, spatial,
    soil, climate, management, yield_potential, business, evidence_gaps。
11. 字段分工：
    - overall: {evaluation, strengths[], main_risks[], core_advice[]}
      evaluation 80–140字；core_advice 每条须含 WHY（引用程序事实）。
    - portrait: {regional_ag_traits, crop_fit, limits[]}
    - score_explain: {high_dims[], low_dims[], biggest_drivers[], how_to_improve[]}
      必须引用程序分数/数据，禁止空泛夸赞。
    - rs_growth: {phenology_normality, anomalies[], ranked_causes[{rank,cause,evidence}]}
    - spatial: {watch_zones[], why[], temporal_caveat}
    - soil: {indicators_to_farm[{indicator,farm_impact,management}]}
    - climate: {risk_present[], disaster_occurred[], notes}
      disaster_occurred 仅在有硬证据时填写，否则 []。
    - management: {variety_direction, planting_focus[], water_fertility_watch[], scouting[]}
    - yield_potential: {level: "高"|"中"|"低"|null, rationale} — 禁止亩产数字
    - business: {available: false, note} — 无经营模型时 available=false
    - evidence_gaps: string[]
12. 可在各对象内增加额外键（extensible），但不得覆盖上述必需键语义。
13. 散文严禁英文字段名/JSON 键。"""


def bailian_configured() -> bool:
    return bool((os.environ.get("BAILIAN_API_KEY") or "").strip())


def bailian_settings() -> dict[str, str]:
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
        out: list[str] = []
        for x in value:
            if isinstance(x, dict):
                # ranked cause etc.
                cause = x.get("cause") or x.get("text") or x.get("item")
                if cause:
                    out.append(str(cause).strip())
                continue
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    return []


def _as_str(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    if isinstance(value, list):
        joined = "\n".join(str(x) for x in value if str(x).strip())
        return joined.strip() or default
    s = str(value).strip()
    return s or default


def _clip(text: str | None, max_chars: int) -> str | None:
    if not text:
        return text
    t = text.strip()
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1] + "…"


def _normalize_ranked(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, str):
        return [{"rank": 1, "cause": value.strip(), "evidence": ""}]
    out: list[dict[str, Any]] = []
    if isinstance(value, list):
        for i, item in enumerate(value, 1):
            if isinstance(item, dict):
                cause = _as_str(item.get("cause") or item.get("text"))
                if not cause:
                    continue
                out.append(
                    {
                        "rank": int(item.get("rank") or i),
                        "cause": cause,
                        "evidence": _as_str(item.get("evidence")) or "",
                    }
                )
            else:
                s = str(item).strip()
                if s:
                    out.append({"rank": i, "cause": s, "evidence": ""})
    return out


def _normalize_soil_rows(value: Any) -> list[dict[str, str]]:
    if not value:
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            ind = _as_str(item.get("indicator") or item.get("name")) or ""
            impact = _as_str(item.get("farm_impact") or item.get("impact")) or ""
            mgmt = _as_str(item.get("management") or item.get("advice")) or ""
            if ind or impact or mgmt:
                rows.append(
                    {
                        "indicator": ind,
                        "farm_impact": impact,
                        "management": mgmt,
                    }
                )
        elif isinstance(item, str) and item.strip():
            rows.append(
                {
                    "indicator": item.strip(),
                    "farm_impact": "",
                    "management": "",
                }
            )
    return rows


def empty_ai_payload(
    *, error: str | None = None, note: str | None = None
) -> dict[str, Any]:
    """Structured empty/fallback AI JSON (PDF still renders facts)."""
    fail_note = note or AI_FAIL
    return {
        "version": 1,
        "llm_configured": False,
        "error": error,
        "overall": {
            "evaluation": fail_note,
            "strengths": [],
            "main_risks": [],
            "core_advice": [],
        },
        "portrait": {
            "regional_ag_traits": fail_note,
            "crop_fit": fail_note,
            "limits": [],
        },
        "score_explain": {
            "high_dims": [],
            "low_dims": [],
            "biggest_drivers": [],
            "how_to_improve": [],
        },
        "rs_growth": {
            "phenology_normality": fail_note,
            "anomalies": [],
            "ranked_causes": [],
        },
        "spatial": {
            "watch_zones": [],
            "why": [],
            "temporal_caveat": "单景不足以定论，需结合多时相连续观测与田间核实。",
        },
        "soil": {"indicators_to_farm": []},
        "climate": {
            "risk_present": [],
            "disaster_occurred": [],
            "notes": fail_note,
        },
        "management": {
            "variety_direction": fail_note,
            "planting_focus": [],
            "water_fertility_watch": [],
            "scouting": [],
        },
        "yield_potential": {"level": None, "rationale": fail_note},
        "business": {"available": False, "note": "数据不足"},
        "evidence_gaps": [],
    }


def missing_llm_sections() -> dict[str, Any]:
    out = empty_ai_payload(
        error="missing_api_key",
        note="大模型未配置（缺少 BAILIAN_API_KEY），本报告仅含程序计算事实与图表。"
        f"（{AI_FAIL}）",
    )
    out["overall"]["evaluation"] = (
        f"程序评分事实已生成（{AI_FAIL}：未配置 BAILIAN_API_KEY）"
    )
    return out


def failed_llm_sections(detail: str) -> dict[str, Any]:
    out = empty_ai_payload(
        error=detail,
        note=f"{AI_FAIL}：大模型调用失败。报告仍包含程序计算事实。",
    )
    out["llm_configured"] = True
    out["overall"]["evaluation"] = f"程序评分事实已生成（{AI_FAIL}）"
    return out


def _yield_level(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("null", "none", "未知", "数据不足"):
        return None
    # Strip any invented yield numbers — keep only 高/中/低
    for lvl in ("高", "中", "低"):
        if lvl in s:
            return lvl
    return None


def normalize_ai(obj: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize model JSON into the extensible land-assessment schema."""
    if not obj:
        out = empty_ai_payload(error="empty_response", note=AI_FAIL)
        return out

    base = empty_ai_payload()
    base["llm_configured"] = True
    base["error"] = None
    if obj.get("version") is not None:
        try:
            base["version"] = int(obj["version"])
        except (TypeError, ValueError):
            base["version"] = 1

    ov = obj.get("overall") if isinstance(obj.get("overall"), dict) else {}
    base["overall"] = {
        "evaluation": _clip(_as_str(ov.get("evaluation")), 200) or AI_FAIL,
        "strengths": _as_str_list(ov.get("strengths")),
        "main_risks": _as_str_list(ov.get("main_risks")),
        "core_advice": _as_str_list(ov.get("core_advice")),
    }
    # Keep unknown overall keys (extensible)
    for k, v in ov.items():
        if k not in base["overall"]:
            base["overall"][k] = v

    por = obj.get("portrait") if isinstance(obj.get("portrait"), dict) else {}
    base["portrait"] = {
        "regional_ag_traits": _as_str(por.get("regional_ag_traits")) or AI_FAIL,
        "crop_fit": _as_str(por.get("crop_fit")) or AI_FAIL,
        "limits": _as_str_list(por.get("limits")),
    }
    for k, v in por.items():
        if k not in base["portrait"]:
            base["portrait"][k] = v

    se = obj.get("score_explain") if isinstance(obj.get("score_explain"), dict) else {}
    base["score_explain"] = {
        "high_dims": _as_str_list(se.get("high_dims")),
        "low_dims": _as_str_list(se.get("low_dims")),
        "biggest_drivers": _as_str_list(se.get("biggest_drivers")),
        "how_to_improve": _as_str_list(se.get("how_to_improve")),
    }
    for k, v in se.items():
        if k not in base["score_explain"]:
            base["score_explain"][k] = v

    rg = obj.get("rs_growth") if isinstance(obj.get("rs_growth"), dict) else {}
    base["rs_growth"] = {
        "phenology_normality": _as_str(rg.get("phenology_normality")) or AI_FAIL,
        "anomalies": _as_str_list(rg.get("anomalies")),
        "ranked_causes": _normalize_ranked(rg.get("ranked_causes")),
    }
    for k, v in rg.items():
        if k not in base["rs_growth"]:
            base["rs_growth"][k] = v

    sp = obj.get("spatial") if isinstance(obj.get("spatial"), dict) else {}
    base["spatial"] = {
        "watch_zones": _as_str_list(sp.get("watch_zones")),
        "why": _as_str_list(sp.get("why")),
        "temporal_caveat": _as_str(sp.get("temporal_caveat"))
        or "单景不足以定论，需结合多时相连续观测与田间核实。",
    }
    for k, v in sp.items():
        if k not in base["spatial"]:
            base["spatial"][k] = v

    soil = obj.get("soil") if isinstance(obj.get("soil"), dict) else {}
    base["soil"] = {
        "indicators_to_farm": _normalize_soil_rows(
            soil.get("indicators_to_farm") or soil.get("rows")
        ),
    }
    for k, v in soil.items():
        if k not in base["soil"]:
            base["soil"][k] = v

    cl = obj.get("climate") if isinstance(obj.get("climate"), dict) else {}
    base["climate"] = {
        "risk_present": _as_str_list(cl.get("risk_present")),
        "disaster_occurred": _as_str_list(cl.get("disaster_occurred")),
        "notes": _as_str(cl.get("notes")) or "",
    }
    for k, v in cl.items():
        if k not in base["climate"]:
            base["climate"][k] = v

    mg = obj.get("management") if isinstance(obj.get("management"), dict) else {}
    base["management"] = {
        "variety_direction": _as_str(mg.get("variety_direction")) or AI_FAIL,
        "planting_focus": _as_str_list(mg.get("planting_focus")),
        "water_fertility_watch": _as_str_list(mg.get("water_fertility_watch")),
        "scouting": _as_str_list(mg.get("scouting")),
    }
    for k, v in mg.items():
        if k not in base["management"]:
            base["management"][k] = v

    yp = (
        obj.get("yield_potential")
        if isinstance(obj.get("yield_potential"), dict)
        else {}
    )
    base["yield_potential"] = {
        "level": _yield_level(yp.get("level")),
        "rationale": _as_str(yp.get("rationale")) or AI_FAIL,
    }
    # Never allow numeric yield fields through
    for banned in ("mu_yield", "yield_kg", "亩产", "yield_t_ha"):
        yp.pop(banned, None)
    for k, v in yp.items():
        if k not in base["yield_potential"] and k not in (
            "mu_yield",
            "yield_kg",
            "亩产",
            "yield_t_ha",
        ):
            base["yield_potential"][k] = v

    biz = obj.get("business") if isinstance(obj.get("business"), dict) else {}
    available = bool(biz.get("available")) if "available" in biz else False
    # Strip money amounts unless a real model flagged available
    note = _as_str(biz.get("note")) or ("数据不足" if not available else "")
    if not available:
        note = note or "数据不足"
        # Drop amount-like keys
        for banned in ("revenue", "cost", "profit", "price", "金额", "收益", "成本"):
            biz.pop(banned, None)
    base["business"] = {"available": available, "note": note}
    for k, v in biz.items():
        if k not in base["business"]:
            base["business"][k] = v

    base["evidence_gaps"] = _as_str_list(obj.get("evidence_gaps"))

    # Preserve any top-level extras (extensible)
    known = {
        "version",
        "overall",
        "portrait",
        "score_explain",
        "rs_growth",
        "spatial",
        "soil",
        "climate",
        "management",
        "yield_potential",
        "business",
        "evidence_gaps",
        "llm_configured",
        "error",
        "raw_excerpt",
    }
    for k, v in obj.items():
        if k not in known:
            base[k] = v

    if (
        not base["overall"].get("evaluation")
        or base["overall"]["evaluation"] == AI_FAIL
    ):
        if not any(
            base["overall"].get(x) for x in ("strengths", "main_risks", "core_advice")
        ):
            base["error"] = base.get("error") or "unparseable_response"

    return base


def facts_for_llm(
    *,
    field: dict[str, Any],
    scorecard: dict[str, Any],
    rs: dict[str, Any],
    risk: dict[str, Any],
    soil: dict[str, Any],
    weather_summary: dict[str, Any],
    analysis: dict[str, Any] | None = None,
    flood_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compact program facts for the Bailian prompt (no chart binaries)."""
    analysis = analysis or {}
    ov = (scorecard or {}).get("overall") or {}
    dims = (scorecard or {}).get("dimensions") or []
    slim_dims = [
        {
            "key": d.get("key"),
            "name": d.get("name"),
            "score": d.get("score"),
            "light": d.get("light"),
            "plain": d.get("plain"),
            "weight": d.get("weight"),
        }
        for d in dims
        if isinstance(d, dict)
    ]
    fe = None
    if flood_evidence:
        fe = {
            "absolute_open_water_scenes": flood_evidence.get(
                "absolute_open_water_scenes"
            ),
            "selected_count": flood_evidence.get("selected_count"),
            "analysis": flood_evidence.get("analysis"),
            "all_dates": (flood_evidence.get("all_dates") or [])[:12],
            "scenes": [
                {
                    "date": s.get("date"),
                    "wet_mean": s.get("wet_mean"),
                    "ndvi_mean": s.get("ndvi_mean"),
                    "kind": s.get("kind"),
                    "analysis": s.get("analysis"),
                }
                for s in (flood_evidence.get("scenes") or [])[:6]
            ],
        }
    wh = analysis.get("weather_history") or {}
    # Keep monthly aggregates only
    months = wh.get("months") or wh.get("monthly") or wh.get("by_month")
    if isinstance(months, list) and len(months) > 24:
        months = months[-24:]
    elif isinstance(months, dict) and len(months) > 24:
        keys = sorted(months.keys())[-24:]
        months = {k: months[k] for k in keys}

    return {
        "field": {
            "name": field.get("name"),
            "location": field.get("location"),
            "area_ha": field.get("area_ha"),
            "area_mu": round(float(field.get("area_ha") or 0) * 15, 1),
            "crop_type": field.get("crop_type"),
            "crop_label": field.get("crop_label"),
            "boundary": field.get("boundary"),
            "land_id": field.get("land_id"),
        },
        "scorecard": {
            "overall": {
                "score": ov.get("score"),
                "grade": ov.get("grade"),
                "light": ov.get("light"),
                "one_liner": ov.get("one_liner"),
            },
            "dimensions": slim_dims,
            "confidence": (scorecard or {}).get("confidence"),
        },
        "rs": {
            "peak_ndvi_mean": rs.get("peak_ndvi_mean"),
            "peak_ndvi_max": rs.get("peak_ndvi_max"),
            "season_ndvi_mean": rs.get("season_ndvi_mean"),
            "offseason_ndvi_mean": rs.get("offseason_ndvi_mean"),
            "rs_flood_level": rs.get("rs_flood_level"),
            "rs_drought_level": rs.get("rs_drought_level"),
            "absolute_open_water_scenes": rs.get("absolute_open_water_scenes"),
            "possible_uncropped_years": rs.get("possible_uncropped_years"),
            "counts": rs.get("counts"),
            "year_peak": rs.get("year_peak"),
        },
        "risk": {
            "period": risk.get("period"),
            "n_scenes": risk.get("n_scenes"),
            "n_season_scenes": risk.get("n_season_scenes"),
            "focus": risk.get("focus"),
            "events": (risk.get("events") or [])[:8],
        },
        "soil": {
            "dominant_texture": soil.get("dominant_texture"),
            "avg_ph": soil.get("avg_ph"),
            "drainage_class": soil.get("drainage_class"),
            "rootzone_awc_mm": soil.get("rootzone_awc_mm"),
            "waterlogging_risk": soil.get("waterlogging_risk"),
            "organic_carbon": soil.get("organic_carbon") or soil.get("soc"),
            "cec": soil.get("cec"),
            "nitrogen": soil.get("nitrogen"),
        },
        "weather_summary": weather_summary or {},
        "analysis": {
            "soil_analysis_plain": analysis.get("soil_analysis_plain"),
            "weather_history_plain": analysis.get("weather_history_plain"),
            "ndvi_grade_shares": analysis.get("ndvi_grade_shares"),
            "phenology_stage_summary": analysis.get("phenology_stage_summary"),
            "phenology_year": analysis.get("phenology_year"),
            "narrative_bridge": analysis.get("narrative_bridge"),
            "weather_history_months": months,
        },
        "flood_evidence": fe,
        "business_model": None,
        "yield_model": None,
        "notes": [
            "无产量模型：yield_potential.level 仅可 高/中/低 或 null，禁止亩产数字。",
            "无经营模型：business.available=false，禁止金额。",
        ],
    }


def generate_land_assessment_narrative(
    facts: dict[str, Any],
    *,
    timeout: float = 120.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Call Bailian; return normalized AI sections. Soft-fails never invent scores."""
    cfg = bailian_settings()
    if not cfg["api_key"]:
        return missing_llm_sections()

    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "请根据以下 JSON 程序事实撰写选地体检解读，仅输出 JSON：\n"
                    + json.dumps(facts, ensure_ascii=False, default=str)
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
        out = normalize_ai(parsed)
        if out.get("error") == "unparseable_response" or (
            not parsed and not out.get("overall", {}).get("strengths")
        ):
            out["error"] = out.get("error") or "unparseable_response"
            out["raw_excerpt"] = str(content)[:500]
            if not parsed:
                # Keep structured shell but mark fail for PDF
                fail = failed_llm_sections("unparseable_response")
                fail["raw_excerpt"] = str(content)[:500]
                return fail
        return out
    except Exception as exc:
        detail = str(exc)[:500]
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            detail = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        elif isinstance(exc, httpx.TimeoutException):
            detail = f"timeout after {timeout}s: {type(exc).__name__}"
        return failed_llm_sections(detail)
    finally:
        if own_client:
            http.close()
