"""Moisture context for the biweekly PDF, from facts the report already has.

不新接气象、雷达或历史服务，也不把单一 NDMI 阈值写成干旱或洪涝。

实际能用的输入：
- 指标序列里已经算好的 NDVI、NDMI（中位数或均值）和两者的变化；
- 由这两项按现有公式算出的 NDDI，只作交叉对照；
- 事实里的本期和前期格点降水、ET0，缺测日不补 0、不算收支；
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
    compute_nddi,
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


def _nddi_at(ndvi: float | None, ndmi: float | None) -> float | None:
    """NDDI from the two-decimal values the report prints, so a reader can recompute it."""
    if ndvi is None or ndmi is None:
        return None
    value = compute_nddi(round(float(ndvi), 2), round(float(ndmi), 2))
    return None if value is None else float(value)


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
        return "气象背景：没有可用的降水或参考蒸散记录，不能据此判断水分收支。", None
    days = int(period["days"])
    precip = _num(period.get("precipitation_mm"))
    et0 = _num(period.get("et0_mm"))
    precip_days = int(period.get("precipitation_days") or 0)
    et0_days = int(period.get("et0_days") or 0)
    parts = [f"气象背景：本期窗口{days}天"]
    if precip is None:
        parts.append("降水缺测")
    else:
        parts.append(f"降水合计{fmt_index(precip, 1)}毫米（有记录{precip_days}天）")
    if et0 is None:
        parts.append("ET0缺测")
    else:
        parts.append(f"ET0合计{fmt_index(et0, 1)}毫米（有记录{et0_days}天）")
    balance = _complete(period)
    balance_mm = None
    if balance is None:
        parts.append("降水或ET0有缺测，不把缺测日当成0，也不计算水分收支")
    else:
        balance_mm = round(balance[0] - balance[1], 1)
        parts.append(
            f"降水减ET0为{fmt_signed(balance_mm, 1)}毫米，这是格点气象差，不是土壤含水量"
        )
    antecedent = facts.get("weather_antecedent") or {}
    if int(antecedent.get("days") or 0) > 0 and _complete(antecedent) is None:
        parts.append("前期降水或ET0也不完整，不并入")
    elif _complete(antecedent) is not None:
        before = _complete(antecedent)
        assert before is not None
        parts.append(
            f"本期开始前{int(antecedent['days'])}天降水合计{fmt_index(before[0], 1)}毫米、"
            f"ET0合计{fmt_index(before[1], 1)}毫米"
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
        return "本期没有哨兵一号雷达影像，不能对积水作出判断。", 0
    if len(vv_rows) < MIN_ORBIT_SAMPLES:
        return (
            f"本期哨兵一号{counted}景，其中带后向散射的{len(vv_rows)}景。"
            f"同轨可比样本不足{MIN_ORBIT_SAMPLES}景，不能确认或排除积水。",
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
        text = "哨兵一号有记录，但同轨基线仍不足以标记，不能写成无风险。"
    else:
        text = "哨兵一号" + "；".join(bits) + "。"
        if "dry" in groups and len(groups) == 1:
            text += "未达到标记条件不等于没有积水风险。"
        else:
            text += "这是已有同轨规则的输出，需现场核对，不是灾损认定。"
        if unlabeled:
            text += f"另有{unlabeled}景基线不足，那些日期不能写成无风险。"
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
        return "像元产品没有 MNDWI，不另作水体指数统计。"
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
        return f"MNDWI 有效像元只有{n_valid}个，样本不足，不统计水体指数比例。"
    fraction = float(np.mean(values[valid] > 0))
    median = float(np.median(values[valid]))
    return (
        f"最新一期 MNDWI 中位数{fmt_index(median)}，大于 0 的有效像元占{fmt_pct(fraction)}。"
        "大于 0 只按指数定义分开，不是积水面积。"
    )


def assess_water_risk(
    facts: dict[str, Any], products: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Sentences that separate observation, derivation and missing evidence."""
    comparison = facts.get("comparison") or {}
    ndmi = _metric_item(facts, "ndmi")
    ndvi = _metric_item(facts, "ndvi")
    latest_day = comparison.get("latest_date")
    baseline_day = comparison.get("baseline_date")
    ndmi_latest = _num(ndmi.get("latest"))
    ndmi_base = _num(ndmi.get("baseline"))
    ndmi_delta = _num(ndmi.get("delta")) if ndmi_latest is not None and ndmi_base is not None else None
    has_ndmi_pair = ndmi_latest is not None and ndmi_base is not None and bool(latest_day and baseline_day)
    weather_line, balance_mm = _weather_sentence(facts)
    has_weather = not weather_line.startswith("气象背景：没有可用")
    lines: list[str] = []
    if has_ndmi_pair:
        stat = "中位数" if ndmi.get("statistic") == "median" else "均值" if ndmi.get("statistic") == "mean" else "数值"
        lines.append(
            f"观测：NDMI（{stat}）{cn_md(latest_day)}为{fmt_index(ndmi_latest)}，"
            f"{cn_md(baseline_day)}为{fmt_index(ndmi_base)}，变化{fmt_signed(ndmi_delta)}。"
            "这是田块水平的指数变化，不是灾害等级。"
        )
    else:
        lines.append("观测：没有可对比的 NDMI，不能描述本期水分指数的变化。")
    nddi_now = _nddi_at(_num(ndvi.get("latest")), ndmi_latest)
    nddi_then = _nddi_at(_num(ndvi.get("baseline")), ndmi_base)
    if nddi_now is not None and nddi_then is not None and has_ndmi_pair:
        lines.append(
            f"推算：用已印出的 NDVI 与 NDMI 计算 NDDI，{cn_md(latest_day)}为{fmt_index(nddi_now)}，"
            f"{cn_md(baseline_day)}为{fmt_index(nddi_then)}。"
            "NDDI 只作交叉对照，不按单一 NDMI 阈值划分等级，也不判定干旱。"
        )
    elif has_ndmi_pair:
        lines.append("推算：NDVI 或 NDMI 不足以计算 NDDI，不另给水分指数等级。")
    lines.append(weather_line)
    conflict = False
    if ndmi_delta is not None and balance_mm is not None and ndmi_delta != 0 and balance_mm != 0:
        if (ndmi_delta > 0 and balance_mm < 0) or (ndmi_delta < 0 and balance_mm > 0):
            conflict = True
            lines.append(
                f"NDMI 变化为{fmt_signed(ndmi_delta)}，降水减 ET0 为{fmt_signed(balance_mm, 1)}毫米，"
                "两项方向不一致，不合并成一个结论，也不判定干旱。"
            )
    radar_line, vv_count = _radar_sentence(facts)
    mndwi_line = _mndwi_sentence(list(products or []), str(latest_day)[:10] if latest_day else None)
    lines.append(
        radar_line
        + mndwi_line
        + "没有同月历史基线，不能判断是否较往年偏干或偏湿。"
        + "以上区分观测和推算，不判定干旱或洪涝。"
    )
    status = "described" if has_ndmi_pair or has_weather else "insufficient"
    if status == "insufficient":
        lines = [
            "水分状况：没有可对比的 NDMI，也没有可用的降水或参考蒸散，证据不足，"
            "不能判断偏干或偏湿，也不能写成无风险。不判定干旱或洪涝。"
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
