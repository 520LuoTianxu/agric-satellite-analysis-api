"""Moisture context for the biweekly PDF, from facts the report already has.

不新接气象、雷达或历史服务。正文用白话写叶子含水、有效降雨和蒸散。
两项同向才说要留意浇水或排水；对不上、缺测或只有一项时不写成没有风险。

实际能用的输入：
- 指标序列里已经算好的 NDMI（中位数或均值）和它的变化；
- 事实里的本期和前期格点降水、蒸散，缺测日不补 0、不算收支；
- 任务载荷里已经读到的哨兵一号景。VV 满 3 景才套用现有同轨规则，
  规则给不出标记时不写成无风险；
- 像元产品里如果本来就有 MNDWI 列，只描述该列，不换算成面积。

没有同月历史基线（干旱规则要求的生长季同月样本不在双周报事实里），
因此不调用 ``classify_drought_scene``，避免把「季外」或「正常」印成没有风险。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from agric_satellite_analysis_common.agri_classify import (
    MIN_ORBIT_SAMPLES,
    classify_flood_series,
)
from app.reports.biweekly.metric_stats import columns_of
from app.reports.biweekly.report_text import cn_md, fmt_index, fmt_pct, fmt_signed

_FLOOD_WORDS = {
    "flood_severe": "规则标记为积水候选（回波更低）",
    "flood_moderate": "规则标记为积水候选",
    "watch": "接近该规则的界限",
    "dry": "未达到积水标记条件",
}


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _metric_item(facts: dict[str, Any], metric: str) -> dict[str, Any]:
    return ((facts.get("comparison") or {}).get("metrics") or {}).get(metric) or {}


def _complete(summary: dict[str, Any] | None) -> tuple[float, float] | None:
    """Precipitation minus ET0 only when every day in the window has both."""
    summary = summary or {}
    days = int(summary.get("days") or 0)
    if days <= 0:
        return None
    if summary.get("missing_precipitation") or summary.get("missing_et0"):
        return None
    if int(summary.get("precipitation_days") or 0) != days or int(summary.get("et0_days") or 0) != days:
        return None
    precip = _num(summary.get("precipitation_mm"))
    et0 = _num(summary.get("et0_mm"))
    if precip is None or et0 is None:
        return None
    return precip, et0


def _weather_sentence(facts: dict[str, Any]) -> tuple[str, float | None]:
    period = facts.get("weather_period") or {}
    if not period or int(period.get("days") or 0) <= 0:
        return "有效降雨：没有可用的降雨或蒸散记录，不能据此判断这半月是亏是余。", None
    days = int(period["days"])
    precip = _num(period.get("precipitation_mm"))
    et0 = _num(period.get("et0_mm"))
    precip_days = int(period.get("precipitation_days") or 0)
    et0_days = int(period.get("et0_days") or 0)
    parts = [f"有效降雨：这{days}天"]
    if precip is None:
        parts.append("有效降雨缺测")
    else:
        parts.append(f"有效降雨合计{fmt_index(precip, 1)}毫米（记下{precip_days}天）")
    if et0 is None:
        parts.append("蒸散缺测")
    else:
        parts.append(f"蒸散合计{fmt_index(et0, 1)}毫米（记下{et0_days}天）")
    balance = _complete(period)
    balance_mm = None
    if balance is None:
        parts.append("有效降雨或蒸散有缺测，不把缺的那天当成0，也不计算水分收支")
    else:
        balance_mm = round(balance[0] - balance[1], 1)
        if balance_mm < 0:
            account = "这半月有效降雨少于蒸散，是亏的"
        elif balance_mm > 0:
            account = "这半月有效降雨多于蒸散，是余的"
        else:
            account = "这半月有效降雨和蒸散差不多"
        parts.append(
            f"有效降雨减去蒸散为{fmt_signed(balance_mm, 1)}毫米，{account}，"
            "这是附近气象记录算出的数，不是地里量到的土壤含水量"
        )
    antecedent = facts.get("weather_antecedent") or {}
    if int(antecedent.get("days") or 0) > 0 and _complete(antecedent) is None:
        parts.append("前期有效降雨或蒸散也不完整，不并入")
    elif _complete(antecedent) is not None:
        before = _complete(antecedent)
        assert before is not None
        parts.append(
            f"本期开始前{int(antecedent['days'])}天有效降雨合计{fmt_index(before[0], 1)}毫米、"
            f"蒸散合计{fmt_index(before[1], 1)}毫米"
        )
    return "，".join(parts) + "。", balance_mm


def _radar_rows(facts: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in facts.get("radar_observations") or []:
        if isinstance(row, dict) and row.get("date"):
            rows.append(row)
    return rows


def _radar_sentence(facts: dict[str, Any]) -> tuple[str, int]:
    rows = _radar_rows(facts)
    counted = int((facts.get("data_quality") or {}).get("radar_count") or facts.get("s1_count") or 0)
    counted = max(counted, len(rows))
    vv_rows = [row for row in rows if _num(row.get("vv")) is not None]
    if counted <= 0:
        return "这半月没有雷达图，不能对积水作出判断。", 0
    if len(vv_rows) < MIN_ORBIT_SAMPLES:
        return (
            f"这半月雷达{counted}景，其中能看回波的{len(vv_rows)}景。"
            f"能拿来比的不到{MIN_ORBIT_SAMPLES}景，不能确认或排除积水。",
            len(vv_rows),
        )
    labelled = classify_flood_series(vv_rows, date_bounded=True)
    groups: dict[str, list[str]] = {}
    unlabeled = 0
    for day, kind in labelled:
        if kind is None:
            unlabeled += 1
            continue
        groups.setdefault(str(kind), []).append(str(day)[:10])
    bits = []
    for kind in ("flood_severe", "flood_moderate", "watch", "dry"):
        days = groups.get(kind) or []
        if not days:
            continue
        shown = "、".join(cn_md(day) for day in days[:4])
        bits.append(f"{shown}{_FLOOD_WORDS[kind]}")
    if not bits:
        text = "雷达有记录，但还对不上同一条轨道，不能写成无风险。"
    else:
        text = "雷达：" + "；".join(bits) + "。"
        if "dry" in groups and len(groups) == 1:
            text += "没有达到标记不等于没有积水风险。"
        else:
            text += "这只是已有规则的标记，要到田里核对，不是灾损认定。"
        if unlabeled:
            text += f"另有{unlabeled}景对不上，那些日期不能写成无风险。"
    return text, len(vv_rows)


def _mndwi_sentence(products: list[dict[str, Any]], latest_day: str | None) -> str:
    chosen = None
    for product in products or []:
        if latest_day and str(product.get("date") or "")[:10] != latest_day:
            continue
        columns = columns_of(product)
        key = next((name for name in columns if str(name).lower() == "mndwi"), None)
        if key is not None:
            chosen = (product, columns, key)
            if latest_day:
                break
    if chosen is None:
        return ""
    _product, columns, key = chosen
    values = np.array(
        [np.nan if item is None else float(item) for item in columns.get(key) or []],
        dtype=float,
    )
    clear = columns.get("clear")
    valid = np.isfinite(values) & (values >= -1.0) & (values <= 1.0)
    if clear:
        mask = np.ones(values.shape, dtype=bool)
        for i, item in enumerate(list(clear)[: values.size]):
            try:
                if item is not None and float(item) == 0.0:
                    mask[i] = False
            except (TypeError, ValueError):
                continue
        valid &= mask
    n_valid = int(valid.sum())
    if n_valid < 30:
        return "另有一项水体指数，能用的点太少，不统计比例。"
    fraction = float(np.mean(values[valid] > 0))
    median = float(np.median(values[valid]))
    return (
        f"另有一项水体指数，中位数{fmt_index(median)}，偏高的地方占{fmt_pct(fraction)}。"
        "偏高只按这个指数分开，不是积水面积。"
    )


def _reader_judgment(ndmi_delta: float | None, balance_mm: float | None) -> str:
    """两项同向才说浇水或排水。只有一项、或都接近不变时，明说看不出来。"""
    leaf = 0 if ndmi_delta in (None, 0) else (1 if ndmi_delta > 0 else -1)
    rain = 0 if balance_mm in (None, 0) else (1 if balance_mm > 0 else -1)
    if leaf and rain and leaf == rain:
        if leaf < 0:
            return (
                "叶子里的水和有效降雨都偏干，这半月要留意浇水。"
                "这是本期参考，不是灾损认定。"
            )
        return (
            "叶子里的水和有效降雨都偏湿，这半月要留意排水。"
            "这是本期参考，不是灾损认定。"
        )
    if leaf or rain:
        return "现在只有一项能对上，还不能判断要不要浇水或排水，也不判定干旱或洪涝。"
    return "现有记录还对不齐，现在还不能判断偏干还是偏湿，不能写成无风险。不判定干旱或洪涝。"


def assess_water_risk(
    facts: dict[str, Any], products: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Sentences that separate observation, derivation and missing evidence."""
    comparison = facts.get("comparison") or {}
    ndmi = _metric_item(facts, "ndmi")
    latest_day = comparison.get("latest_date")
    baseline_day = comparison.get("baseline_date")
    ndmi_latest = _num(ndmi.get("latest"))
    ndmi_base = _num(ndmi.get("baseline"))
    ndmi_delta = _num(ndmi.get("delta")) if ndmi_latest is not None and ndmi_base is not None else None
    has_ndmi_pair = ndmi_latest is not None and ndmi_base is not None and bool(latest_day and baseline_day)
    weather_line, balance_mm = _weather_sentence(facts)
    has_weather = not weather_line.startswith("有效降雨：没有可用")
    lines: list[str] = []
    if has_ndmi_pair:
        if ndmi_delta is None or ndmi_delta == 0:
            move = "差不多"
        elif ndmi_delta > 0:
            move = "多了一些"
        else:
            move = "少了一些"
        lines.append(
            f"叶子里的水：{cn_md(latest_day)}为{fmt_index(ndmi_latest)}，"
            f"{cn_md(baseline_day)}为{fmt_index(ndmi_base)}，变化{fmt_signed(ndmi_delta)}，{move}。"
            "这是整块地的变化，不是灾害等级。"
        )
    else:
        lines.append("叶子里的水：没有可对比的两期，看不出是多了还是少了。")
    lines.append(weather_line)
    conflict = False
    if ndmi_delta is not None and balance_mm is not None and ndmi_delta != 0 and balance_mm != 0:
        if (ndmi_delta > 0 and balance_mm < 0) or (ndmi_delta < 0 and balance_mm > 0):
            conflict = True
            lines.append(
                f"叶子里的水变化为{fmt_signed(ndmi_delta)}，有效降雨减去蒸散为{fmt_signed(balance_mm, 1)}毫米，"
                "两项方向不一致，不合成一个旱涝结论，也不判定干旱或洪涝。"
            )
    radar_line, vv_count = _radar_sentence(facts)
    mndwi_line = _mndwi_sentence(list(products or []), str(latest_day)[:10] if latest_day else None)
    closing = "" if conflict else _reader_judgment(ndmi_delta, balance_mm)
    lines.append(
        radar_line
        + mndwi_line
        + "没有往年同一个月可以比，看不出比往年更干还是更湿。"
        + closing
    )
    status = "described" if has_ndmi_pair or has_weather else "insufficient"
    if status == "insufficient":
        lines = [
            "水分状况：没有可对比的叶子含水，也没有可用的有效降雨或蒸散，证据不足，"
            "现在还不能判断偏干还是偏湿，不能写成无风险。不判定干旱或洪涝。"
        ]
    return {
        "status": status,
        "lines": lines,
        "ndmi_delta": ndmi_delta,
        "balance_mm": balance_mm,
        "conflict": conflict,
        "radar_vv_count": vv_count,
        "has_ndmi_pair": has_ndmi_pair,
        "has_weather": has_weather,
    }
