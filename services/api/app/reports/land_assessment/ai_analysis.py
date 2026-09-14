# -*- coding: utf-8 -*-
"""Bailian AI layer for land-assessment PDFs.

Python owns all scores/charts/facts. AI only interprets (structured JSON).
Soft-fails to 「AI 分析失败」 placeholders — never recalculates scores.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx

from app.reports.land_assessment.soil_labels import (
    soil_awc_indicator,
    soil_display_fields,
    translate_soil_jargon,
)

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
5. 遥感长势禁止单指数下结论；须结合生育阶段+天气+水分+土壤，并给出排序可能原因；天气 vs 人为管理可排序时须写明。
6. 空间异常须写时间连续性 caveat：单景不能定论。无显著空间异质时 watch_zones 可为 []，并在 why 说明「全田同步、未见斑块」。
7. 土壤：指标→田间影响→管理方向；指标名必须中文（黏壤土/排水良好/根系层有效持水量(mm)），严禁 clay loam、well drained、Rootzone AWC 等英文；严禁具体 kg/亩施肥量。
8. 经营分析：无价格/成本/产量模型时不得写金额，写「数据不足」或省略金额块。
9. 禁止产品升级/平台介绍/未来功能宣传。
10. 输出必须是合法 JSON（见各分节说明）。
11. 散文严禁英文字段名/JSON 键；土壤与异常描述用农户能懂的话。"""

# Per-section schemas for parallel Bailian calls (soft-fail independently).
SECTION_SPECS: list[tuple[str, str, str]] = [
    (
        "overall",
        "overall",
        '只输出 JSON：{"overall":{"evaluation":"80-140字","strengths":[],"main_risks":[],"core_advice":["含WHY引用程序事实"]}}',
    ),
    (
        "portrait",
        "portrait",
        '只输出 JSON：{"portrait":{"regional_ag_traits":"","crop_fit":"","limits":[]}}',
    ),
    (
        "score_explain",
        "score_explain",
        '只输出 JSON：{"score_explain":{"high_dims":[],"low_dims":[],"biggest_drivers":[],"how_to_improve":[]}}；必须引用程序分数。',
    ),
    (
        "rs_growth",
        "rs_growth",
        '只输出 JSON：{"rs_growth":{"phenology_normality":"",'
        '"anomalies":[{"event_id":"E1","problem":"问题是什么（一句话）",'
        '"likely_cause":"天气|水分渍涝|播种出苗管理|养分|其他",'
        '"basis":"判断依据（引用程序 NDVI/天气/阶段等）","confidence":"高|中|低"}],'
        '"ranked_causes":[{"rank":1,"cause":"","evidence":""}]}}。'
        "对 risk.events 中每个事件各写一张 anomalies 卡片；禁止单指数定论；"
        "likely_cause 只能取给定五类之一。",
    ),
    (
        "spatial",
        "spatial",
        '只输出 JSON：{"spatial":{"watch_zones":["区域简述或空数组"],'
        '"why":["原因"],"temporal_caveat":"单景不足定论…","no_hotspot":false}}。'
        "若程序未见空间异质斑块：watch_zones=[]，no_hotspot=true，why 说明全田同步。",
    ),
    (
        "soil",
        "soil",
        '只输出 JSON：{"soil":{"indicators_to_farm":[{"indicator":"中文指标",'
        '"farm_impact":"","management":""}]}}。'
        "indicator 必须中文：如 黏壤土、排水良好、根系层有效持水量(mm) 164；禁止英文。",
    ),
    (
        "climate",
        "climate",
        '只输出 JSON：{"climate":{"risk_present":[],"disaster_occurred":[],"notes":""}}；'
        "disaster_occurred 仅硬证据，否则 []。",
    ),
    (
        "management",
        "management",
        '只输出 JSON：{"management":{"variety_direction":"","planting_focus":[],'
        '"water_fertility_watch":[],"scouting":[]}}',
    ),
    (
        "yield_potential",
        "yield_potential",
        '只输出 JSON：{"yield_potential":{"level":"高|中|低|null","rationale":""}}；禁止亩产数字。',
    ),
    (
        "business",
        "business",
        '只输出 JSON：{"business":{"available":false,"note":"数据不足"},"evidence_gaps":[]}',
    ),
]

CAUSE_CATEGORIES = ("天气", "水分渍涝", "播种出苗管理", "养分", "其他")
CONFIDENCE_LEVELS = ("高", "中", "低")


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
                # ranked cause / watch zone / generic object
                cause = (
                    x.get("cause")
                    or x.get("text")
                    or x.get("item")
                    or x.get("zone")
                    or x.get("name")
                    or x.get("area")
                    or x.get("label")
                    or x.get("description")
                    or x.get("problem")
                )
                if cause:
                    note = x.get("note") or x.get("why") or x.get("detail")
                    s = str(cause).strip()
                    if note:
                        s = f"{s}（{str(note).strip()}）"
                    out.append(s)
                continue
            s = str(x).strip()
            if s:
                out.append(s)
        return out
    return []


def _normalize_cause_category(value: Any) -> str:
    s = str(value or "").strip()
    for cat in CAUSE_CATEGORIES:
        if cat in s:
            return cat
    # light English / synonym mapping
    low = s.lower()
    if any(k in low for k in ("weather", "rain", "heat", "气候", "高温", "降水")):
        return "天气"
    if any(k in low for k in ("flood", "wet", "ndwi", "渍", "涝", "水分")):
        return "水分渍涝"
    if any(k in low for k in ("emerg", "plant", "sow", "出苗", "播种", "密度")):
        return "播种出苗管理"
    if any(k in low for k in ("nutri", "fert", "养分", "肥")):
        return "养分"
    return "其他" if s else "其他"


def _normalize_confidence(value: Any) -> str:
    s = str(value or "").strip()
    for lvl in CONFIDENCE_LEVELS:
        if lvl in s:
            return lvl
    low = s.lower()
    if low in ("high", "h"):
        return "高"
    if low in ("low", "l"):
        return "低"
    return "中"


def _normalize_anomaly_cards(value: Any) -> list[dict[str, str]]:
    """Farmer-readable anomaly cards; keep program event_id when present."""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for i, item in enumerate(value, 1):
        if isinstance(item, str) and item.strip():
            out.append(
                {
                    "event_id": f"E{i}",
                    "problem": item.strip(),
                    "likely_cause": "其他",
                    "basis": "",
                    "confidence": "低",
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        problem = _as_str(
            item.get("problem")
            or item.get("what")
            or item.get("summary")
            or item.get("text")
            or item.get("cause")
        )
        if not problem:
            continue
        eid = _as_str(item.get("event_id") or item.get("id")) or f"E{i}"
        basis = (
            _as_str(
                item.get("basis")
                or item.get("judgment_basis")
                or item.get("evidence")
                or item.get("依据")
            )
            or ""
        )
        out.append(
            {
                "event_id": eid,
                "problem": problem,
                "likely_cause": _normalize_cause_category(
                    item.get("likely_cause")
                    or item.get("cause_category")
                    or item.get("更可能原因")
                ),
                "basis": basis,
                "confidence": _normalize_confidence(
                    item.get("confidence") or item.get("把握")
                ),
            }
        )
    return out


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
            ind = translate_soil_jargon(
                _as_str(item.get("indicator") or item.get("name")) or ""
            )
            impact = translate_soil_jargon(
                _as_str(item.get("farm_impact") or item.get("impact")) or ""
            )
            mgmt = translate_soil_jargon(
                _as_str(item.get("management") or item.get("advice")) or ""
            )
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
            "anomaly_lines": [],
            "ranked_causes": [],
        },
        "spatial": {
            "watch_zones": [],
            "why": [],
            "temporal_caveat": "单景不足以定论，需结合多时相连续观测与田间核实。",
            "no_hotspot": False,
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
    anomaly_cards = _normalize_anomaly_cards(rg.get("anomalies"))
    base["rs_growth"] = {
        "phenology_normality": _as_str(rg.get("phenology_normality")) or AI_FAIL,
        "anomalies": anomaly_cards,
        # legacy string list for older PDF paths
        "anomaly_lines": [
            f"{c['event_id']}：{c['problem']}（更可能：{c['likely_cause']}，把握{c['confidence']}）"
            for c in anomaly_cards
        ],
        "ranked_causes": _normalize_ranked(rg.get("ranked_causes")),
    }
    for k, v in rg.items():
        if k not in base["rs_growth"]:
            base["rs_growth"][k] = v

    sp = obj.get("spatial") if isinstance(obj.get("spatial"), dict) else {}
    zones = _as_str_list(sp.get("watch_zones"))
    why = _as_str_list(sp.get("why"))
    no_hotspot = bool(sp.get("no_hotspot")) or (not zones and bool(why))
    base["spatial"] = {
        "watch_zones": zones,
        "why": why,
        "temporal_caveat": _as_str(sp.get("temporal_caveat"))
        or "单景不足以定论，需结合多时相连续观测与田间核实。",
        "no_hotspot": no_hotspot,
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
        "soil": (
            lambda s: {
                "dominant_texture": s.get("dominant_texture"),
                "avg_ph": s.get("avg_ph"),
                "drainage_class": s.get("drainage_class"),
                "rootzone_awc_mm": s.get("rootzone_awc_mm"),
                "rootzone_awc_label": soil_awc_indicator(soil.get("rootzone_awc_mm")),
                "waterlogging_risk": s.get("waterlogging_risk"),
                "organic_carbon": soil.get("organic_carbon") or soil.get("soc"),
                "cec": soil.get("cec"),
                "nitrogen": soil.get("nitrogen"),
                "npk": soil.get("npk"),
                "氮_全氮_g_kg": (soil.get("npk") or {}).get("tn_g_kg")
                if isinstance(soil.get("npk"), dict)
                else soil.get("nitrogen"),
                "磷_有效磷_mg_kg": (soil.get("npk") or {}).get("ap_mg_kg")
                if isinstance(soil.get("npk"), dict)
                else None,
                "钾_速效钾_mg_kg": (soil.get("npk") or {}).get("ak_mg_kg")
                if isinstance(soil.get("npk"), dict)
                else None,
            }
        )(soil_display_fields(soil)),
        "weather_summary": weather_summary or {},
        "analysis": {
            "soil_analysis_plain": translate_soil_jargon(
                analysis.get("soil_analysis_plain") or ""
            )
            or analysis.get("soil_analysis_plain"),
            "weather_history_plain": analysis.get("weather_history_plain"),
            "ndvi_grade_shares": analysis.get("ndvi_grade_shares"),
            "phenology_stage_summary": analysis.get("phenology_stage_summary"),
            "phenology_year": analysis.get("phenology_year"),
            "emergence": analysis.get("emergence"),
            "emergence_by_year": analysis.get("emergence_by_year"),
            "narrative_bridge": analysis.get("narrative_bridge"),
            "weather_history_months": months,
            "risk_events_evidence": (analysis.get("risk_events_evidence") or [])[:8],
        },
        "flood_evidence": fe,
        "business_model": None,
        "yield_model": None,
        "notes": [
            "无产量模型：yield_potential.level 仅可 高/中/低 或 null，禁止亩产数字。",
            "无经营模型：business.available=false，禁止金额。",
        ],
    }


def _bailian_chat(
    *,
    system: str,
    user: str,
    timeout: float,
    client: httpx.Client | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """POST one chat completion; return (parsed_json|None, error|None)."""
    cfg = bailian_settings()
    if not cfg["api_key"]:
        return None, "missing_api_key"
    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
    }
    url = f"{cfg['base_url']}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    own = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        resp = http.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get(
            "content"
        ) or ""
        parsed = _extract_json(content)
        if not parsed:
            return None, f"unparseable_response:{str(content)[:200]}"
        return parsed, None
    except Exception as exc:
        detail = str(exc)[:500]
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            detail = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        elif isinstance(exc, httpx.TimeoutException):
            detail = f"timeout after {timeout}s: {type(exc).__name__}"
        return None, detail
    finally:
        if own:
            http.close()


def _section_facts(facts: dict[str, Any], section_key: str) -> dict[str, Any]:
    """Shrink payload per section to reduce latency / truncation risk."""
    base = {
        "field": facts.get("field"),
        "notes": facts.get("notes"),
    }
    if section_key in (
        "overall",
        "portrait",
        "score_explain",
        "management",
        "yield_potential",
    ):
        base["scorecard"] = facts.get("scorecard")
        base["rs"] = facts.get("rs")
        base["soil"] = facts.get("soil")
        base["weather_summary"] = facts.get("weather_summary")
        base["analysis"] = {
            k: (facts.get("analysis") or {}).get(k)
            for k in (
                "soil_analysis_plain",
                "weather_history_plain",
                "phenology_stage_summary",
                "emergence",
                "narrative_bridge",
            )
        }
    if section_key == "rs_growth":
        base["rs"] = facts.get("rs")
        base["risk"] = facts.get("risk")
        base["soil"] = facts.get("soil")
        base["weather_summary"] = facts.get("weather_summary")
        base["analysis"] = {
            k: (facts.get("analysis") or {}).get(k)
            for k in (
                "phenology_stage_summary",
                "phenology_year",
                "emergence",
                "emergence_by_year",
                "risk_events_evidence",
                "weather_history_months",
            )
        }
    if section_key == "spatial":
        base["rs"] = facts.get("rs")
        base["risk"] = facts.get("risk")
        base["analysis"] = {
            k: (facts.get("analysis") or {}).get(k)
            for k in ("phenology_stage_summary", "emergence", "risk_events_evidence")
        }
        base["flood_evidence"] = facts.get("flood_evidence")
    if section_key == "soil":
        base["soil"] = facts.get("soil")
        base["analysis"] = {
            "soil_analysis_plain": (facts.get("analysis") or {}).get(
                "soil_analysis_plain"
            )
        }
        base["field"] = facts.get("field")
    if section_key == "climate":
        base["weather_summary"] = facts.get("weather_summary")
        base["rs"] = {
            k: (facts.get("rs") or {}).get(k)
            for k in (
                "rs_flood_level",
                "rs_drought_level",
                "absolute_open_water_scenes",
            )
        }
        base["flood_evidence"] = facts.get("flood_evidence")
        base["analysis"] = {
            k: (facts.get("analysis") or {}).get(k)
            for k in ("weather_history_plain", "weather_history_months")
        }
    if section_key == "business":
        base["business_model"] = facts.get("business_model")
        base["yield_model"] = facts.get("yield_model")
        base["scorecard"] = facts.get("scorecard")
    return base


def generate_land_assessment_narrative(
    facts: dict[str, Any],
    *,
    timeout: float = 180.0,
    client: httpx.Client | None = None,
    max_workers: int = 6,
    parallel: bool = True,
) -> dict[str, Any]:
    """Call Bailian (parallel per section by default). Soft-fails never invent scores.

    Root cause note (spatial 「AI 分析失败」 with why text present):
    a single mega-prompt often returned empty ``watch_zones`` (or dict objects
    that the old parser dropped) while ``why`` was filled; PDF ``bullets()``
    treated empty list as hard fail. Parallel section calls + richer watch_zone
    parsing + no_hotspot empty-state fix that.
    """
    cfg = bailian_settings()
    if not cfg["api_key"]:
        return missing_llm_sections()

    section_timeout = float(timeout)
    # When parallel, each section gets the full timeout budget (wall ≈ max).
    # Monolithic fallback uses the same timeout for one big call.
    merged: dict[str, Any] = {"version": 1}
    errors: list[str] = []

    def _run_section(
        spec: tuple[str, str, str],
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        section_id, top_key, schema_hint = spec
        slim = _section_facts(facts, section_id)
        user = f"分节={section_id}。{schema_hint}\n程序事实 JSON：\n" + json.dumps(
            slim, ensure_ascii=False, default=str
        )
        # shared client is not thread-safe; each worker uses own client unless provided
        # and max_workers==1
        use_client = client if (client is not None and max_workers <= 1) else None
        parsed, err = _bailian_chat(
            system=SYSTEM_PROMPT,
            user=user,
            timeout=section_timeout,
            client=use_client,
        )
        return top_key, parsed, err

    if parallel and max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [pool.submit(_run_section, spec) for spec in SECTION_SPECS]
            for fut in as_completed(futs):
                top_key, parsed, err = fut.result()
                if err or not parsed:
                    errors.append(f"{top_key}:{err or 'empty'}")
                    continue
                # Accept either {"overall": {...}} or bare section object
                if top_key in parsed and isinstance(parsed.get(top_key), (dict, list)):
                    merged[top_key] = parsed[top_key]
                elif top_key == "business":
                    if "business" in parsed:
                        merged["business"] = parsed["business"]
                    if "evidence_gaps" in parsed:
                        merged["evidence_gaps"] = parsed["evidence_gaps"]
                else:
                    # model returned the object itself
                    merged[top_key] = parsed
    else:
        # Monolithic single-call fallback (tests / debugging)
        body_hint = (
            "请根据以下 JSON 程序事实撰写选地体检解读，仅输出 JSON，顶层键："
            "version, overall, portrait, score_explain, rs_growth, spatial, "
            "soil, climate, management, yield_potential, business, evidence_gaps。\n"
            + json.dumps(facts, ensure_ascii=False, default=str)
        )
        parsed, err = _bailian_chat(
            system=SYSTEM_PROMPT,
            user=body_hint,
            timeout=section_timeout,
            client=client,
        )
        if err or not parsed:
            return failed_llm_sections(err or "empty_response")
        merged = parsed

    out = normalize_ai(merged)
    if errors:
        out["section_errors"] = errors
        # Soft-fail: keep successful sections; only mark global error if almost nothing worked
        got = sum(
            1
            for k in (
                "overall",
                "portrait",
                "score_explain",
                "rs_growth",
                "spatial",
                "soil",
                "climate",
                "management",
                "yield_potential",
            )
            if merged.get(k)
        )
        if got == 0:
            fail = failed_llm_sections("; ".join(errors)[:500])
            fail["section_errors"] = errors
            return fail
        out["error"] = None
    return out
