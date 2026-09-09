# -*- coding: utf-8 -*-
"""Season-aware land-assessment scoring (白话选地体检).

Rules:
- Vigor by bound crop season (from ``app.core.crops``), not annual NDVI avg.
- Flood/drought: no red without hard evidence; relative wetness/NDVI dips
  are reminders only.
- Area reported in 亩 (1 ha = 15 亩) by callers.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

import numpy as np

SEASON_MONTHS = {6, 7, 8, 9}
PEAK_MONTHS = {7, 8}
UNCROPPED_NDVI = 0.25
PEAK_GOOD = 0.65
PEAK_OK = 0.50
PEAK_WEAK = 0.35

WEIGHTS = {
    "crop": 0.25,
    "soil": 0.20,
    "vigor": 0.20,
    "weather": 0.15,
    "wet_safety": 0.10,
    "drought_safety": 0.10,
}


def _month(d: str) -> int:
    return int(d[5:7])


def _year(d: str) -> int:
    return int(d[:4])


def _light(score: float) -> str:
    if score >= 70:
        return "绿"
    if score >= 55:
        return "黄"
    return "红"


def _cluster(dates_list: list[str], typ: str) -> list[dict[str, Any]]:
    if not dates_list:
        return []
    dates_list = sorted(dates_list)
    out: list[dict[str, Any]] = []
    start = prev = dates_list[0]
    for d in dates_list[1:]:
        if (datetime.fromisoformat(d) - datetime.fromisoformat(prev)).days <= 16:
            prev = d
        else:
            out.append(
                {
                    "start": start,
                    "end": prev,
                    "type": typ,
                    "days": (
                        datetime.fromisoformat(prev) - datetime.fromisoformat(start)
                    ).days
                    + 1,
                    "severity": "中",
                    "confidence": "中",
                    "check": "下田看密度/渍害/缺肥；若整片极低绿度也可能当年未种",
                }
            )
            start = prev = d
    out.append(
        {
            "start": start,
            "end": prev,
            "type": typ,
            "days": (datetime.fromisoformat(prev) - datetime.fromisoformat(start)).days
            + 1,
            "severity": "中",
            "confidence": "中",
            "check": "下田看密度/渍害/缺肥；若整片极低绿度也可能当年未种",
        }
    )
    return out


def _pick_crop_suit(suit: dict[str, Any] | None, crop_key: str) -> dict[str, Any]:
    """Prefer suitability row matching the field's bound crop."""
    suit = suit or {}
    key = (crop_key or "corn").lower()
    bound = suit.get("field_crop_suitability") or {}
    if (bound.get("crop") or "").lower() == key:
        return bound
    for c in suit.get("crops") or []:
        name = (c.get("crop") or c.get("name") or "").lower()
        if name == key or key in name:
            return c
    return bound or {
        "crop": key,
        "score": 72.0,
        "rating": "fair",
        "limiting_factors": [],
    }


def compute_assessment(
    indices: list[dict[str, Any]],
    soil: dict[str, Any],
    weather_summary: dict[str, Any],
    weather_stress: dict[str, Any] | None = None,
    suitability: dict[str, Any] | None = None,
    field_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute scorecard / rs / risk payloads from index rows + soil/weather.

    ``indices`` rows: {date, layer_type|layer, mean, median?, p10?, p90?, quality_score?}
    """
    weather_stress = weather_stress or {}
    field_meta = field_meta or {}

    from app.core.crops import (
        crop_name_zh,
        get_crop_season,
        normalize_crop_key,
    )

    crop_key = normalize_crop_key(field_meta.get("crop_type")) or "corn"
    season = get_crop_season(crop_key)
    season_months = set(season.season_months)
    peak_months = set(season.peak_months)
    crop_label = crop_name_zh(crop_key)

    by: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    qualities: list[float] = []
    for r in indices:
        layer = r.get("layer_type") or r.get("layer")
        date = str(r.get("date"))
        if not layer or not date:
            continue
        mean = r.get("mean")
        if mean is None:
            continue
        row = {
            "mean": float(mean),
            "median": float(r["median"])
            if r.get("median") is not None
            else float(mean),
            "p10": float(r["p10"]) if r.get("p10") is not None else float(mean),
            "p90": float(r["p90"]) if r.get("p90") is not None else float(mean),
            "quality": float(r.get("quality_score") or r.get("quality") or 0.5),
        }
        by[date][str(layer).upper()] = row
        qualities.append(row["quality"])

    dates = sorted(by)
    qmean = float(np.mean(qualities)) if qualities else 0.5

    # Prefer NDWI; fall back to MNDWI from agri products.
    def _wet_layer(d: str) -> dict[str, Any] | None:
        return by[d].get("NDWI") or by[d].get("MNDWI")

    season_dates = [d for d in dates if _month(d) in season_months and "NDVI" in by[d]]
    peak_dates = [d for d in dates if _month(d) in peak_months and "NDVI" in by[d]]
    off_dates = [d for d in dates if _month(d) not in season_months and "NDVI" in by[d]]

    ndvi_s = [by[d]["NDVI"]["mean"] for d in season_dates]
    ndvi_p = [by[d]["NDVI"]["mean"] for d in peak_dates]
    ndvi_off = [by[d]["NDVI"]["mean"] for d in off_dates]
    ndwi_s = [_wet_layer(d)["mean"] for d in season_dates if _wet_layer(d) is not None]

    years = sorted({_year(d) for d in peak_dates})
    year_peak: dict[str, dict[str, Any]] = {}
    uncropped_years: list[int] = []
    for y in years:
        ys = [by[d]["NDVI"]["mean"] for d in peak_dates if _year(d) == y]
        if not ys:
            continue
        mx, mn, mean_y = max(ys), min(ys), float(np.mean(ys))
        year_peak[str(y)] = {
            "n": len(ys),
            "mean": round(mean_y, 3),
            "max": round(mx, 3),
            "min": round(mn, 3),
        }
        if mx < UNCROPPED_NDVI:
            uncropped_years.append(y)

    peak_mean = float(np.mean(ndvi_p)) if ndvi_p else 0.0
    peak_max = float(np.max(ndvi_p)) if ndvi_p else 0.0
    season_mean = float(np.mean(ndvi_s)) if ndvi_s else 0.0
    off_mean = float(np.mean(ndvi_off)) if ndvi_off else 0.0

    ndvi_s_arr = np.array(ndvi_s) if ndvi_s else np.array([0.2])
    evi_s = [by[d]["EVI"]["mean"] for d in season_dates if "EVI" in by[d]]
    ndwi_s_arr = np.array(ndwi_s) if ndwi_s else np.array([-0.3])
    ndvi_p30 = float(np.percentile(ndvi_s_arr, 30))
    ndvi_p50 = float(np.percentile(ndvi_s_arr, 50))
    evi_p30 = float(np.percentile(evi_s, 30)) if evi_s else 0.3
    ndwi_p85 = float(np.percentile(ndwi_s_arr, 85))

    growth_flags: list[str] = []
    wet_flags: list[str] = []
    for d in season_dates:
        n = by[d]["NDVI"]["mean"]
        e = by[d].get("EVI", {}).get("mean")
        wlayer = _wet_layer(d)
        w = wlayer["mean"] if wlayer else None
        likely_bare = _month(d) in PEAK_MONTHS and n < UNCROPPED_NDVI
        if likely_bare:
            continue
        if n < ndvi_p30 and (e is None or e < evi_p30):
            growth_flags.append(d)
        if w is not None and w > ndwi_p85 and n < ndvi_p50:
            wet_flags.append(d)

    evs: list[dict[str, Any]] = []
    for i, e in enumerate(
        _cluster(growth_flags, "生育期长势偏弱")
        + _cluster(wet_flags, "生育期相对偏湿"),
        1,
    ):
        e = dict(e)
        e["id"] = f"E{i}"
        e["secondary"] = None
        evs.append(e)

    seasons = []
    for y, info in year_peak.items():
        seasons.append(
            {
                "start": f"{y}-06-01",
                "peak": f"{y}-08-01",
                "end": f"{y}-09-30",
                "days": 122,
                "peak_ndvi": info["max"],
                "confidence": "中" if info["n"] >= 2 else "低",
            }
        )

    month_hits: dict[str, int] = defaultdict(int)
    for d in growth_flags + wet_flags:
        month_hits[str(_month(d))] += 1

    focus = f"生育期长势（{crop_label}）"
    if growth_flags and wet_flags:
        focus = "生育期长势低谷与相对偏湿并存"
    elif wet_flags:
        focus = "生育期相对偏湿"
    elif uncropped_years:
        focus = "部分年份峰值绿度极低（可能大面积未种植）"

    risk = {
        "period": f"{dates[0]} — {dates[-1]}" if dates else "",
        "n_scenes": len(dates),
        "n_season_scenes": len(season_dates),
        "n_peak_scenes": len(peak_dates),
        "n_seasons": len(seasons),
        "n_events": len(evs),
        "growth_events": len([e for e in evs if "长势" in e["type"]]),
        "wet_events": len([e for e in evs if "偏湿" in e["type"]]),
        "focus": focus,
        "ndvi_p30": round(ndvi_p30, 4),
        "ndvi_p50": round(ndvi_p50, 4),
        "evi_p30": round(evi_p30, 4),
        "ndwi_p85": round(ndwi_p85, 4),
        "events": evs,
        "seasons": seasons,
        "month_hits": dict(month_hits),
        "method_note": (
            f"长势与事件仅用{season.label_zh}；阈值来自生育期内部分位，非全年平均。"
        ),
    }

    # VCI-like within season
    vci_like = []
    mn_s, mx_s = float(np.min(ndvi_s_arr)), float(np.max(ndvi_s_arr))
    for d in season_dates:
        n = by[d]["NDVI"]["mean"]
        vci = (n - mn_s) / (mx_s - mn_s + 1e-6) * 100
        vci_like.append((d, vci, n))
    drought_mod = sum(1 for _, v, n in vci_like if v < 35 and n >= UNCROPPED_NDVI)
    drought_sev = sum(1 for _, v, n in vci_like if v < 20 and n >= UNCROPPED_NDVI)
    flood_cand = sum(
        1
        for d in season_dates
        if (_wet_layer(d) or {}).get("mean", -1) > ndwi_p85
        and by[d]["NDVI"]["mean"] < ndvi_p50
    )
    open_water_dates: list[dict[str, Any]] = []
    for d in season_dates:
        wl = _wet_layer(d)
        if wl is None:
            continue
        wet_mean = float(wl.get("mean", -1))
        if wet_mean <= 0:
            continue
        ndvi_mean = None
        if "NDVI" in by[d]:
            ndvi_mean = round(float(by[d]["NDVI"]["mean"]), 4)
        open_water_dates.append(
            {
                "date": d,
                "wet_mean": round(wet_mean, 4),
                "ndvi_mean": ndvi_mean,
            }
        )
    open_water_dates.sort(key=lambda x: (-float(x["wet_mean"]), x["date"]))
    abs_water = len(open_water_dates)

    if abs_water == 0 and flood_cand < 3:
        rs_flood = "低（未见明水面）"
    elif abs_water == 0:
        rs_flood = "中等（生育期相对偏湿）"
    else:
        rs_flood = "偏高（见明水面）"

    if drought_sev >= 3:
        rs_drought = "偏高"
    elif drought_mod >= 4:
        rs_drought = "中等"
    else:
        rs_drought = "低"

    rs = {
        "method": f"seasonal_{crop_key}_{sorted(season_months)}_{sorted(peak_months)}",
        "counts": {"season_scenes": len(season_dates), "peak_scenes": len(peak_dates)},
        "ndvi_range": [
            round(float(np.min(ndvi_s_arr)), 3),
            round(float(np.max(ndvi_s_arr)), 3),
        ],
        "ndwi_range": (
            [round(float(np.min(ndwi_s_arr)), 3), round(float(np.max(ndwi_s_arr)), 3)]
            if len(ndwi_s)
            else None
        ),
        "ndwi_median": round(float(np.median(ndwi_s_arr)), 3) if len(ndwi_s) else None,
        "peak_ndvi_mean": round(peak_mean, 3),
        "peak_ndvi_max": round(peak_max, 3),
        "season_ndvi_mean": round(season_mean, 3),
        "offseason_ndvi_mean": round(off_mean, 3),
        "rs_flood_level": rs_flood,
        "rs_drought_level": rs_drought,
        "absolute_open_water_scenes": abs_water,
        "open_water_dates": open_water_dates,
        "drought_moderate_vci_lt35": drought_mod,
        "drought_severe_vci_lt20": drought_sev,
        "flood_candidates": flood_cand,
        "year_peak": year_peak,
        "possible_uncropped_years": uncropped_years,
        "uncropped_rule": (
            f"峰值期(7–8月)地块均值 NDVI max < {UNCROPPED_NDVI} "
            "记为可能大面积未种植/绝产年"
        ),
    }

    # ---- dimension scores ----
    crop_suit = _pick_crop_suit(suitability, crop_key)
    crop_score = float(crop_suit.get("score") or 72.0)

    wl = float(soil.get("waterlogging_risk") or 0)
    ph = float(soil.get("avg_ph") or 7)
    soil_score = 100.0
    soil_bits: list[str] = []
    texture = soil.get("dominant_texture") or ""
    if texture:
        soil_bits.append(str(texture))
    if ph > 7.5:
        soil_score -= min(25, (ph - 7.5) * 20)
        soil_bits.append(f"偏碱 pH {ph:.2f}")
    elif ph < 5.5:
        soil_score -= min(25, (5.5 - ph) * 20)
        soil_bits.append(f"偏酸 pH {ph:.2f}")
    if wl > 0.3:
        soil_score -= min(20, wl * 30)
        soil_bits.append("有一定渍水风险")
    drain = soil.get("drainage_class") or ""
    if "well" in drain.lower():
        soil_bits.append(f"排水较好（{crop_label}一般合适，过干年份要看墒）")
    soil_score = max(35.0, min(95.0, soil_score))

    if not peak_dates and not season_dates:
        vigor = 55.0
        vigor_plain = (
            f"暂无足够的{crop_label}季遥感场景，长势分按中性占位，请先回填指数后再生成"
        )
    elif peak_mean >= PEAK_GOOD:
        vigor = 85.0
        vigor_plain = f"峰值期平均 NDVI≈{peak_mean:.2f}，达到较好{crop_label}冠层水平"
    elif peak_mean >= PEAK_OK:
        vigor = 70.0
        vigor_plain = f"峰值期平均 NDVI≈{peak_mean:.2f}，{crop_label}季冠层中等偏好"
    elif peak_mean >= PEAK_WEAK:
        vigor = 55.0
        vigor_plain = (
            f"峰值期平均 NDVI≈{peak_mean:.2f}，生育期长势偏弱，建议看密度/水肥"
        )
    else:
        vigor = 40.0
        vigor_plain = f"峰值期平均 NDVI≈{peak_mean:.2f}，明显偏低"

    if peak_mean > off_mean + 0.25:
        vigor += 5
        vigor_plain += f"；旺季明显高于淡季({off_mean:.2f})，季节节律正常"
    elif peak_mean < off_mean + 0.1 and ndvi_p:
        vigor -= 8
        vigor_plain += "；旺季相对淡季抬升不足，节律偏弱"

    vigor -= min(12, len(growth_flags) * 1.5)
    if uncropped_years:
        vigor_plain += (
            f"；{uncropped_years} 年峰值极低，更像大面积未种植/绝产，"
            "已从「差长势」里单独标出"
        )
    vigor = max(25.0, min(92.0, round(float(vigor), 1)))
    vigor_plain += (
        f"。生育期场景 {len(season_dates)} 景，峰值 {len(peak_dates)} 景；"
        "全年均 NDVI 不参与打分。"
    )

    heat = float(weather_summary.get("heat_stress_days") or 0)
    wd = float(weather_summary.get("water_deficit_mm") or 0)
    weather = 78.0
    wplain_bits: list[str] = []
    if heat >= 8:
        weather -= 12
        wplain_bits.append(f"近月热胁迫 {int(heat)} 天")
    elif heat >= 4:
        weather -= 6
        wplain_bits.append(f"近月热胁迫 {int(heat)} 天")
    if abs(wd) > 40:
        weather -= 10
        wplain_bits.append("水分盈亏偏大")
    elif abs(wd) > 15:
        weather -= 4
    ms = weather_stress.get("moisture_status") or ""
    if "Adequate" in ms or "optimal" in (weather_stress.get("status") or "").lower():
        weather += 3
        wplain_bits.append("近况墒情尚可")
    weather = max(40.0, min(92.0, weather))
    wplain = "；".join(wplain_bits) or "近月气象压力不大"

    # Conservative wet/drought: no red without hard evidence
    wet_safety = 88.0
    wet_bits: list[str] = []
    if abs_water >= 2:
        wet_safety = 48.0
        wet_bits.append(f"卫星见明水面 {abs_water} 景，有硬涝证据")
    elif abs_water == 1:
        wet_safety = 62.0
        wet_bits.append("偶见明水面信号，需核实")
    else:
        wet_bits.append("卫星没看到大片明水面，不像发过大洪水泡田")
        if flood_cand:
            wet_safety -= min(8, flood_cand * 1.5)
            wet_bits.append(
                f"有 {flood_cand} 次「相对偏湿」弱信号，只作提醒，不按灾史打红灯"
            )
        if wl > 0.3:
            wet_safety -= min(7, wl * 12)
            wet_bits.append("土略有积水倾向，雨季留意低洼即可")
        if "well" in drain.lower():
            wet_bits.append("排水等级较好，有利散墒")
    wet_safety = max(45.0 if abs_water == 0 else 30.0, min(92.0, round(wet_safety, 1)))
    # Cap: without hard open-water evidence, never red
    if abs_water == 0 and wet_safety < 55:
        wet_safety = 55.0
    wet_plain = "；".join(wet_bits) + f"。综合 {wet_safety} 分。"

    drought_safety = 90.0
    d_bits: list[str] = []
    hard_drought = False  # no official drought disaster feed yet
    if hard_drought:
        drought_safety = 45.0
        d_bits.append("有可引用的干旱成灾记录")
    else:
        d_bits.append("没有可引用的干旱成灾记录")
        if peak_mean >= PEAK_OK:
            d_bits.append(f"旺季平均绿度约 {peak_mean:.2f}，关键生长期并不像「旱垮了」")
        else:
            drought_safety -= 10
            d_bits.append(f"旺季绿度约 {peak_mean:.2f}，偏低但不足以单独定旱灾")
        # relative VCI dips are reminders only
        if drought_sev or drought_mod:
            drought_safety -= min(6, drought_sev * 1.5 + drought_mod * 0.5)
            d_bits.append("相对历史偏绿少只作提醒；苗期/成熟回落不算旱")
        awc = soil.get("rootzone_awc_mm")
        if awc is not None:
            d_bits.append(f"土壤保水约 {float(awc):.0f} mm")
    drought_safety = max(
        55.0 if not hard_drought else 25.0, min(95.0, round(drought_safety, 1))
    )
    drought_plain = "；".join(d_bits) + f"。综合 {drought_safety} 分。"

    dims = [
        {
            "key": "crop",
            "name": f"作物匹配（{crop_label}）",
            "score": round(crop_score, 1),
            "light": _light(crop_score),
            "plain": (
                f"系统给{crop_label} {crop_score} 分（{crop_suit.get('rating') or '—'}）。限制："
                + ("；".join(crop_suit.get("limiting_factors") or ["—"]))
            ),
            "weight": "25%",
        },
        {
            "key": "soil",
            "name": "土壤条件",
            "score": round(soil_score, 1),
            "light": _light(soil_score),
            "plain": "；".join(soil_bits) or "土壤条件中等",
            "weight": "20%",
        },
        {
            "key": "vigor",
            "name": "生育期遥感长势",
            "score": float(vigor),
            "light": _light(vigor),
            "plain": vigor_plain,
            "weight": "20%",
        },
        {
            "key": "weather",
            "name": "天气适宜",
            "score": round(weather, 1),
            "light": _light(weather),
            "plain": wplain,
            "weight": "15%",
        },
        {
            "key": "wet_safety",
            "name": "抗渍/洪涝安全",
            "score": round(wet_safety, 1),
            "light": _light(wet_safety),
            "plain": wet_plain,
            "weight": "10%",
        },
        {
            "key": "drought_safety",
            "name": "抗旱安全",
            "score": round(drought_safety, 1),
            "light": _light(drought_safety),
            "plain": drought_plain,
            "weight": "10%",
        },
    ]

    raw = sum(d["score"] * WEIGHTS[d["key"]] for d in dims)
    conf = 78
    if qmean < 0.4:
        conf -= 8
    if not dates:
        conf -= 20
    if field_meta.get("boundary_source") == "survey":
        conf += 2
    conf = max(45, min(88, conf))
    overall = round(raw * (0.85 + 0.15 * conf / 100), 1)
    ov_light = _light(overall)
    ov_grade = "较好" if overall >= 70 else ("一般" if overall >= 55 else "偏弱")

    hard_flood = abs_water >= 1
    if not hard_flood and not hard_drought and overall >= 70:
        one_liner = f"适合{crop_label}，生长季长势不错；没有真涝真旱硬证据，涝旱项已按保守提醒重算。"
    elif overall >= 70:
        one_liner = "生育期长势尚可，综合条件中等偏好"
    elif overall >= 55:
        one_liner = f"能种{crop_label}，但要盯生育期水肥与局部未种植斑块"
    else:
        one_liner = "短板明显，建议先核实种植记录再谈改种"

    thinking = (
        f"按{season.label_zh}重算，不用全年 NDVI 平均。{season.vigor_note}"
        f"峰值期均 NDVI≈{peak_mean:.2f}（最高≈{peak_max:.2f}），淡季≈{off_mean:.2f}。"
        f"{crop_label}适宜性 {crop_score}。土壤 {texture or '—'}、pH {ph:.2f}。"
        f"涝硬证据明水面={abs_water} 景；旱无成灾档案。"
        f"疑似未种植/极低绿度年份：{uncropped_years or '未发现（地块均值口径）'}。"
        f"分数仍是开源体检不是买地判决。"
    )

    annual_means = [by[d]["NDVI"]["mean"] for d in dates if "NDVI" in by[d]]
    scorecard = {
        "overall": {
            "score": overall,
            "grade": ov_grade,
            "light": ov_light,
            "one_liner": one_liner,
            "thinking": thinking,
        },
        "dimensions": dims,
        "confidence": {
            "score": conf,
            "plain": (
                f"生育期口径；影像质量均分约{qmean:.2f}；峰值景{len(peak_dates)}"
            ),
        },
        "howto": (
            f"长势分只看{crop_label}生育期/峰值期，不看全年平均。"
            "绿灯≥70，黄灯55–69，红灯<55。"
            "峰值期整景极低绿度单独标「可能未种植」。"
            "无硬涝/旱证据不打红灯。"
        ),
        "method": {
            "crop": crop_key,
            "crop_name_zh": crop_label,
            "season_months": sorted(season_months),
            "peak_months": sorted(peak_months),
            "uncropped_ndvi": UNCROPPED_NDVI,
            "peak_ndvi_mean": round(peak_mean, 3),
            "annual_ndvi_mean_NOT_used": (
                round(float(np.mean(annual_means)), 3) if annual_means else None
            ),
        },
        "method_wet_drought": {
            "rule": "no_red_without_hard_flood_or_drought_evidence",
            "absolute_open_water_scenes": abs_water,
            "open_water_dates": open_water_dates,
            "peak_ndvi_mean": round(peak_mean, 3),
            "wet_score": wet_safety,
            "drought_score": drought_safety,
        },
    }

    return {
        "scorecard": scorecard,
        "rs": rs,
        "risk": risk,
        "by_date": {
            d: {k: v["mean"] for k, v in layers.items()} for d, layers in by.items()
        },
        "meta": {
            "qmean": round(qmean, 4),
            "n_dates": len(dates),
            "season_dates": season_dates,
            "peak_dates": peak_dates,
            "years": years,
            "ndvi_p30": ndvi_p30,
            "evi_p30": evi_p30,
            "ndwi_p85": ndwi_p85,
            "month_hits": dict(month_hits),
        },
    }
