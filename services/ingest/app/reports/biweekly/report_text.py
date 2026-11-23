"""Wording of the official biweekly report, built from enriched facts.

行文规则：
- 先结论、后证据、再给田间核查建议；每条结论都能在证据表或图上找到对应数值；
- 指标一律用专业名称（如归一化植被指数NDVI），首次出现附一句用途说明；
- 只描述数值水平、变化方向和位置，不写「好转」「变差」，不推断病虫害、缺肥等原因；
- 缺测写「缺测」，不以 0 代替；阶段来自种植方案的计划日期，写明未经田间确认；
- S1/S2/S3 阶段代号不直接出现在正文，使用中文阶段名。

返回的字符串可含 reportlab Paragraph 支持的 <b> 标记。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from agric_satellite_analysis_common.biweekly.metric_specs import (
    METRIC_SPECS,
    REPORT_METRIC_ORDER,
    metric_abbr,
)
from agric_satellite_analysis_common.biweekly.phase_policy import (
    MAPPING_VERSION,
    PHASE_FORMAL_NAMES,
    PHASE_METRICS,
    PHASE_STAGE_SPAN,
    STAGE_BACKGROUND,
)

MISSING = "缺测"
DEMO_NOTICE = "示例数据 · 非实测 · 不用于农事决策"
# 参与阶段走向对照的植被指数；NDMI 反映水分，不按植被指数的季节走向判读。
VEGETATION_INDICES = ("ndvi", "evi", "ndre")
REJECT_REASONS = {
    "cloud_over_limit": "云量超过限值",
    "cloud_unreadable": "云量无法读取",
    "no_index_values": "无有效指数值",
    "excluded": "质量不符合要求",
}


def as_day(value: Any) -> str:
    return str(value or "")[:10]


def cn_date(day: Any) -> str:
    parts = as_day(day).split("-")
    if len(parts) != 3:
        return str(day or "")
    return f"{int(parts[0])}年{int(parts[1])}月{int(parts[2])}日"


def cn_md(day: Any) -> str:
    parts = as_day(day).split("-")
    if len(parts) != 3:
        return str(day or "")
    return f"{int(parts[1])}月{int(parts[2])}日"


def fmt_index(value: float | None, digits: int = 2) -> str:
    if value is None:
        return MISSING
    text = f"{value:.{digits}f}"
    # 避免出现 -0.00。
    return text[1:] if text.startswith("-") and float(text) == 0 else text


def fmt_signed(value: float | None, digits: int = 2) -> str:
    if value is None:
        return MISSING
    text = f"{value:+.{digits}f}"
    return text.replace("+", "", 1).replace("-", "", 1) if float(text) == 0 else text


def fmt_pct(fraction: float | None) -> str:
    if fraction is None:
        return MISSING
    pct = 100.0 * fraction
    if 0 < pct < 1:
        return "不足1%"
    if 99 < pct < 100:
        return "超过99%"
    return f"{pct:.0f}%"


def join_names(names: list[str]) -> str:
    names = [name for name in names if name]
    if len(names) <= 1:
        return "".join(names)
    return "、".join(names[:-1]) + "和" + names[-1]


def period_of(facts: dict[str, Any]) -> dict[str, Any]:
    return dict(facts.get("period") or {})


def period_end_inclusive(period: dict[str, Any]) -> str:
    end = as_day(period.get("end_inclusive") or period.get("end"))
    if end:
        return end
    exclusive = as_day(period.get("end_exclusive"))
    if exclusive:
        return (date.fromisoformat(exclusive) - timedelta(days=1)).isoformat()
    return ""


def period_text(facts: dict[str, Any]) -> str:
    period = period_of(facts)
    start, end = as_day(period.get("start")), period_end_inclusive(period)
    if not start or not end:
        return cn_date(start) if start else MISSING
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    tail = cn_md(end) if start[:4] == end[:4] else cn_date(end)
    return f"{cn_date(start)}至{tail}（{days}天）"


def is_demo(facts: dict[str, Any]) -> bool:
    return str(facts.get("data_kind") or "").lower() == "demo"


def land_title(facts: dict[str, Any]) -> str:
    name = str(facts.get("land_name") or "").strip()
    if name:
        return name
    land_id = str(facts.get("land_id") or "").strip()
    return f"地块 {land_id}" if land_id else "地块"


# ---------------------------------------------------------------- stage


def plan_source_text(phase: dict[str, Any]) -> str:
    if phase.get("plan_source") == "reference" or phase.get("phenology_basis") == "PLAN_DERIVED_FROM_BTU_REFERENCE":
        name = (phase.get("reference") or {}).get("name")
        label = f"参照方案（{name}）" if name else "参照方案"
        return f"依据{label}的计划日期，非本田方案，未经田间确认"
    if phase.get("plan_source") == "native" or phase.get("plan_label") == "本田方案":
        return "依据本田种植方案的计划日期，未经田间确认"
    return ""


def _phase_name(label: str | None) -> str | None:
    return PHASE_FORMAL_NAMES.get(label) if label else None


def stage_summary(facts: dict[str, Any]) -> dict[str, Any]:
    """Stage of the period in formal words. ``kind``: single, split, unstaged, none."""
    phase = dict(facts.get("phase") or {})
    source = plan_source_text(phase)
    rows = [row for row in (phase.get("observation_stages") or []) if row.get("stage_code")]
    if not rows:
        # 没有有效观测时，退回到计划窗口内出现的阶段。
        seen: dict[str, dict[str, Any]] = {}
        for row in phase.get("planned_days") or []:
            if row.get("stage_code"):
                seen.setdefault(row["stage_code"], row)
        rows = list(seen.values())
    if not source or not rows:
        return {
            "kind": "none",
            "stages": [],
            "latest_stage": None,
            "text": "本田未关联种植方案，不作生育阶段判断",
            "source": "",
        }
    codes = list(dict.fromkeys(row["stage_code"] for row in rows))
    labels = list(dict.fromkeys(row.get("phase_label") for row in rows))
    latest = rows[-1]
    if len(labels) == 1 and labels[0] in PHASE_METRICS:
        text = f"{join_names(codes)}，属{_phase_name(labels[0])}"
        kind = "single"
    elif len(labels) == 1:
        text = f"{join_names(codes)}，该时期不按生长阶段选取指标"
        kind = "unstaged"
    else:
        parts = []
        for code in codes:
            row = next(item for item in rows if item["stage_code"] == code)
            name = _phase_name(row.get("phase_label"))
            parts.append(f"{code}（{name}）" if name else code)
        text = "跨越" + join_names(parts) + "，指标按最近一次有效观测所在阶段选取"
        kind = "split"
    return {
        "kind": kind,
        "stages": codes,
        "latest_stage": latest.get("stage_code"),
        "latest_phase": latest.get("phase_label"),
        "text": text,
        "source": source,
    }


# ---------------------------------------------------------------- observations


def _series(facts: dict[str, Any]) -> list[dict[str, Any]]:
    return list(facts.get("metric_series") or [])


def _row_on(facts: dict[str, Any], day: str | None) -> dict[str, Any] | None:
    for row in _series(facts):
        if row["date"] == day:
            return row
    return None


def observation_text(facts: dict[str, Any]) -> str:
    quality = facts.get("data_quality") or {}
    used = list(quality.get("used_dates") or [])
    rejected = list(quality.get("rejected") or [])
    if used:
        text = f"{len(used)}次：{'、'.join(cn_md(day) for day in used)}（哨兵二号）"
    else:
        text = "本期无有效光学观测"
    if rejected:
        text += f"；另有{len(rejected)}次过境未采用（{'、'.join(cn_md(row['date']) for row in rejected)}）"
    return text


def comparison_text(facts: dict[str, Any]) -> str:
    comparison = facts.get("comparison") or {}
    latest, baseline = comparison.get("latest_date"), comparison.get("baseline_date")
    if not latest:
        return "本期无有效观测，不作对比"
    if not baseline:
        return f"{cn_md(latest)}为最近一次有效观测，此前无可对比的有效观测"
    where = "本期内上一次有效观测" if comparison.get("baseline_in_period") else "本期开始前最近一次有效观测"
    gap = comparison.get("gap_days")
    gap_text = f"，间隔{gap}天" if gap is not None else ""
    return f"{cn_md(latest)}对比{cn_md(baseline)}（{where}{gap_text}）"


def basic_info_rows(facts: dict[str, Any]) -> list[tuple[str, str]]:
    stage = stage_summary(facts)
    stage_text = stage["text"] + (f"（{stage['source']}）" if stage["source"] else "")
    rows = [
        ("监测周期", period_text(facts)),
        ("有效光学观测", observation_text(facts)),
        ("生育阶段", stage_text),
        ("对比基准", comparison_text(facts)),
    ]
    if is_demo(facts):
        rows.append(("数据性质", str(facts.get("data_notice") or DEMO_NOTICE)))
    return rows


# ---------------------------------------------------------------- metric wording


def _statistic_word(statistic: str | None) -> str:
    return "中位数" if statistic == "median" else "均值"


def _direction_word(direction: str | None) -> str:
    return {"up": "升高", "down": "降低", "flat": "持平"}.get(direction or "", "")


def change_phrase(item: dict[str, Any]) -> str:
    """'升高 0.05' / '持平' / '' when there is nothing to compare."""
    direction = item.get("direction")
    if direction in ("up", "down"):
        return f"{_direction_word(direction)} {fmt_index(abs(item['delta']))}"
    if direction == "flat":
        return "持平"
    return ""


def reference_phrase(metric: str, value: float | None) -> str:
    spec = METRIC_SPECS[metric]
    if value is None or spec.low_reference is None:
        return ""
    ref = fmt_index(spec.low_reference)
    if round(value, 2) < spec.low_reference:
        return f"低于低值参考线 {ref}"
    if round(value, 2) == round(spec.low_reference, 2):
        return f"与低值参考线 {ref} 相当"
    return f"高于低值参考线 {ref}"


def metric_level_sentence(facts: dict[str, Any], metric: str) -> str:
    comparison = facts.get("comparison") or {}
    item = (comparison.get("metrics") or {}).get(metric) or {}
    abbr = metric_abbr(metric)
    latest_day, baseline_day = comparison.get("latest_date"), comparison.get("baseline_date")
    if item.get("latest") is None:
        return f"<b>{abbr}</b>：{cn_md(latest_day) if latest_day else '本期'}无有效数据。"
    text = (
        f"<b>{abbr}</b>：{cn_md(latest_day)}全田{_statistic_word(item.get('statistic'))} "
        f"{fmt_index(item['latest'])}"
    )
    ref = reference_phrase(metric, item["latest"])
    if ref:
        text += f"，{ref}"
    change = change_phrase(item)
    if change:
        text += f"；较{cn_md(baseline_day)}{change}"
    elif baseline_day:
        text += f"；{cn_md(baseline_day)}该指标缺测，不作对比"
    else:
        text += "；无可对比的前期观测"
    return text + "。"


def stage_interpretation(facts: dict[str, Any]) -> str | None:
    """Stage background plus whether the observed direction matches it."""
    stage = stage_summary(facts)
    code = stage.get("latest_stage")
    background = STAGE_BACKGROUND.get(code or "")
    if not background:
        return None
    text = f"<b>阶段背景</b>：{background['text']}"
    plan = facts.get("report_metrics") or {}
    comparison = facts.get("comparison") or {}
    items = comparison.get("metrics") or {}
    watched = [m for m in (plan.get("primary") or []) if m in VEGETATION_INDICES] or ["ndvi"]
    moves = {m: (items.get(m) or {}).get("direction") for m in watched}
    moves = {m: d for m, d in moves.items() if d}
    trend = background["trend"]
    if not moves or trend == "low":
        return text
    expected = {"rise": "up", "decline": "down", "stable": "flat"}[trend]
    groups: dict[str, list[str]] = {}
    for m, d in moves.items():
        groups.setdefault(d, []).append(metric_abbr(m))
    described = "，".join(
        join_names(names) + ("均" if len(names) > 1 else "") + _direction_word(d) for d, names in groups.items()
    )
    if all(d == expected for d in moves.values()):
        text += f"本期{described}，与该阶段的常见走向一致。"
    elif trend == "rise" and any(d == "down" for d in moves.values()):
        text += f"本期{described}，与该阶段的常见走向不一致，原因需结合田间情况判断。"
    elif trend == "decline" and any(d == "up" for d in moves.values()):
        text += f"本期{described}，与该阶段的常见走向不同。"
    else:
        text += f"本期{described}。"
    return text


def _infield(facts: dict[str, Any], metric: str) -> dict[str, Any] | None:
    for item in facts.get("infield") or []:
        if item.get("metric") == metric:
            return item
    return None


def flagged_clause(item: dict[str, Any], *, with_metric: bool = True) -> str:
    """Whether statistically low pixels exist, whether they form patches, and where."""
    relative = item.get("relative") or {}
    count = int(relative.get("flagged_pixels") or 0)
    clustered = int(relative.get("clustered_pixels") or 0)
    abbr = metric_abbr(item["metric"]) if with_metric else ""
    if count == 0:
        return f"{abbr}未发现显著低于本田整体水平的像元"
    if clustered == 0:
        return f"{abbr}有 {count} 个零星像元显著低于本田整体水平，未成片分布"
    text = f"{abbr}有 {count} 个像元（占有效像元的 {fmt_pct(relative.get('flagged_fraction'))}）显著低于本田整体水平"
    text += "，均成片分布" if clustered == count else f"，其中 {clustered} 个成片分布"
    regions = relative.get("regions") or []
    if regions:
        top = regions[0]
        if top["share"] >= 0.5:
            text += f"，主要位于田块{top['name']}"
        else:
            text += "，以田块" + join_names([r["name"] for r in regions[:2]]) + "居多"
    return text


def infield_sentence(facts: dict[str, Any]) -> str | None:
    plan = facts.get("report_metrics") or {}
    clauses = [
        flagged_clause(item)
        for item in (_infield(facts, metric) for metric in (plan.get("primary") or []))
        if item is not None
    ]
    if not clauses:
        return None
    return "<b>田内差异</b>：" + "；".join(clauses) + "。"


def supporting_sentence(facts: dict[str, Any]) -> str | None:
    plan = facts.get("report_metrics") or {}
    items = (facts.get("comparison") or {}).get("metrics") or {}
    parts = []
    for metric in plan.get("supporting") or []:
        item = items.get(metric) or {}
        if item.get("latest") is None:
            parts.append(f"{metric_abbr(metric)}{MISSING}")
            continue
        change = change_phrase(item)
        parts.append(f"{metric_abbr(metric)} {fmt_index(item['latest'])}" + (f"（{change}）" if change else ""))
    if not parts:
        return None
    return "<b>辅助指标</b>：" + "，".join(parts) + "。"


def quality_sentence(facts: dict[str, Any]) -> str:
    quality = facts.get("data_quality") or {}
    used = list(quality.get("used_dates") or [])
    rejected = list(quality.get("rejected") or [])
    total = len(used) + len(rejected)
    if not used:
        text = f"<b>数据质量</b>：本期光学过境 {total} 次，均未采用。" if total else "<b>数据质量</b>：本期没有光学过境记录。"
        return text
    text = f"<b>数据质量</b>：本期光学过境 {total} 次，采用 {len(used)} 次"
    low, high = quality.get("valid_fraction_min"), quality.get("valid_fraction_max")
    if low is not None and high is not None:
        if round(low, 2) == round(high, 2):
            text += f"；采用观测的有效像元占比为 {fmt_pct(low)}"
        else:
            text += f"；采用观测的有效像元占比为 {fmt_pct(low)} 至 {fmt_pct(high)}"
    text += "。"
    if quality.get("mean_only_dates"):
        text += "部分日期没有像元级数据，仅有田块均值。"
    if len(used) == 1:
        text += "本期仅 1 次有效观测，结论只代表该日状况。"
    return text


def weather_sentence(facts: dict[str, Any]) -> str | None:
    weather = facts.get("weather_period") or {}
    rain, et0 = weather.get("precipitation_mm"), weather.get("et0_mm")
    tmin, tmax = weather.get("min_c"), weather.get("max_c")
    if rain is None and et0 is None and tmin is None and tmax is None:
        return None
    days = int(weather.get("days") or 0)
    parts = []
    if rain is not None:
        parts.append(f"降水量 {rain:.1f} 毫米" + _coverage_note(weather.get("precipitation_days"), days))
    if et0 is not None:
        parts.append(f"参考作物蒸散量（ET0）{et0:.1f} 毫米" + _coverage_note(weather.get("et0_days"), days))
    if tmin is not None and tmax is not None:
        parts.append(f"气温 {tmin:.1f} 至 {tmax:.1f} ℃")
    return "<b>气象</b>：本期" + "，".join(parts) + "。"


def _coverage_note(have: Any, days: int) -> str:
    """Say how many days a total covers when some days have no record."""
    if have is None or not days or int(have) >= days:
        return ""
    return f"（{days} 天中 {days - int(have)} 天缺测，合计只含 {int(have)} 天）"


def surrounding_sentence(facts: dict[str, Any]) -> str | None:
    meta = facts.get("surrounding_comparison_meta") or {}
    rows = list(meta.get("comparisons") or [])
    if not rows or not facts.get("surrounding_comparison_image"):
        return None
    row = rows[-1]
    diff = row["difference"]
    word = "高于" if diff > 0 else ("低于" if diff < 0 else "等于")
    amount = f" {abs(diff):.1f}" if diff else ""
    return (
        f"<b>周边对照</b>：{cn_md(row['date'])}本田超绿指数（ExG）中位数 {row['field']:.1f}，"
        f"{word}周边1公里内农田{amount}。"
    )


def conclusion_items(facts: dict[str, Any]) -> list[str]:
    comparison = facts.get("comparison")
    plan = facts.get("report_metrics") or {}
    if not comparison:
        items = ["<b>本期无有效光学观测</b>，不对长势水平和变化作出判断。", quality_sentence(facts)]
        weather = weather_sentence(facts)
        if weather:
            items.append(weather)
        return items
    items = [metric_level_sentence(facts, metric) for metric in plan.get("primary") or []]
    for extra in (
        stage_interpretation(facts),
        infield_sentence(facts),
        supporting_sentence(facts),
        surrounding_sentence(facts),
        weather_sentence(facts),
        quality_sentence(facts),
    ):
        if extra:
            items.append(extra)
    return items


# ---------------------------------------------------------------- evidence table


def evidence_table(facts: dict[str, Any]) -> list[list[str]] | None:
    comparison = facts.get("comparison")
    plan = facts.get("report_metrics") or {}
    if not comparison:
        return None
    latest_day, baseline_day = comparison["latest_date"], comparison.get("baseline_date")
    latest_row = _row_on(facts, latest_day) or {"metrics": {}}
    header = [
        "指标",
        f"{cn_md(latest_day)}",
        f"{cn_md(baseline_day)}" if baseline_day else "对比基准",
        "变化量",
        "第10至第90百分位",
        "低值参考线",
        "低于参考线的像元",
    ]
    rows = [header]
    for role, metrics in (("主要", plan.get("primary") or []), ("辅助", plan.get("supporting") or [])):
        for metric in metrics:
            item = (comparison.get("metrics") or {}).get(metric) or {}
            entry = (latest_row.get("metrics") or {}).get(metric) or {}
            spec = METRIC_SPECS[metric]
            spread = (
                f"{fmt_index(entry.get('p10'))} 至 {fmt_index(entry.get('p90'))}"
                if entry.get("status") == "ok" and item.get("statistic") == "median"
                else "—"
            )
            below = (
                fmt_pct(entry.get("below_low_reference_fraction"))
                if entry.get("status") == "ok" and item.get("statistic") == "median"
                else "—"
            )
            rows.append(
                [
                    f"{spec.abbr}（{role}）",
                    fmt_index(item.get("latest")),
                    fmt_index(item.get("baseline")) if baseline_day else "—",
                    fmt_signed(item.get("delta")) if item.get("delta") is not None else "—",
                    spread,
                    fmt_index(spec.low_reference) if spec.low_reference is not None else "—",
                    below,
                ]
            )
    return rows


def evidence_caption(facts: dict[str, Any]) -> str:
    plan = facts.get("report_metrics") or {}
    statistics = facts.get("metric_statistics") or {}
    kinds = {statistics.get(m) for m in (plan.get("primary") or []) + (plan.get("supporting") or [])} - {None}
    if kinds == {"median"}:
        basis = "数值为田块内有效像元的中位数"
    elif kinds == {"mean"}:
        basis = "数值为田块均值（无像元级数据，未提供百分位）"
    else:
        basis = "数值为田块内有效像元的中位数；无像元级数据的指标为田块均值"
    return f"{basis}，指数均无量纲。变化量为最近一次观测减对比基准。“—”表示不适用，“{MISSING}”表示没有有效数据。"


# ---------------------------------------------------------------- field checks


def next_period_sentence(facts: dict[str, Any]) -> str | None:
    phase = facts.get("phase") or {}
    nxt = phase.get("next_week") or {}
    if not nxt.get("start") or not nxt.get("stage_code"):
        return None
    end = as_day(nxt.get("end_exclusive"))
    end_text = cn_md((date.fromisoformat(end) - timedelta(days=1)).isoformat()) if end else ""
    span = f"{cn_md(nxt['start'])}至{end_text}" if end_text else f"{cn_md(nxt['start'])}起"
    name = _phase_name(nxt.get("phase_label"))
    stage = f"{nxt['stage_code']}（{name}）" if name else str(nxt["stage_code"])
    text = f"下一监测周期（{span}）按计划处于{stage}"
    primary = [metric_abbr(m) for m in (nxt.get("primary_metrics") or [])]
    if primary:
        text += f"，主要指标为{join_names(primary)}"
    return text + "。"


def field_check_items(facts: dict[str, Any]) -> list[str]:
    """Checks that follow from this period's evidence. Never states a cause."""
    comparison = facts.get("comparison")
    plan = facts.get("report_metrics") or {}
    items: list[str] = []
    if not comparison:
        items.append("本期没有可用于判断的光学观测，建议按常规巡田，重点查看近期天气过程后的田间状况。")
        nxt = next_period_sentence(facts)
        if nxt:
            items.append(nxt)
        return items

    places: list[str] = []
    for metric in plan.get("primary") or []:
        item = _infield(facts, metric)
        regions = ((item or {}).get("relative") or {}).get("regions") or []
        if int(((item or {}).get("relative") or {}).get("clustered_pixels") or 0) > 0 and regions:
            name = f"田块{regions[0]['name']}"
            if name not in places:
                places.append(name)
    if places:
        items.append(
            f"<b>优先核查位置</b>：{join_names(places)}（田内分布图中黑框标出的像元）。"
            "查看该处的作物长势和土壤墒情。"
        )

    stage = stage_summary(facts)
    background = STAGE_BACKGROUND.get(stage.get("latest_stage") or "") or {}
    metrics_cmp = comparison.get("metrics") or {}
    low = [
        m
        for m in plan.get("primary") or []
        if METRIC_SPECS[m].low_reference is not None
        and (metrics_cmp.get(m) or {}).get("latest") is not None
        and round(metrics_cmp[m]["latest"], 2) < METRIC_SPECS[m].low_reference
    ]
    if low and background.get("trend") not in ("low", "decline"):
        items.append(
            f"<b>全田水平</b>：{join_names([metric_abbr(m) for m in low])}低于低值参考线，"
            "建议结合播期、出苗情况和近期农事，抽查全田长势。"
        )
    against = [
        m
        for m in plan.get("primary") or []
        if m in VEGETATION_INDICES
        and background.get("trend") == "rise"
        and (metrics_cmp.get(m) or {}).get("direction") == "down"
    ]
    if against:
        items.append(
            f"<b>变化方向</b>：{join_names([metric_abbr(m) for m in against])}"
            f"较{cn_md(comparison.get('baseline_date'))}降低，与该阶段的常见走向不一致，建议近期到田查看。"
        )
    latest_row = _row_on(facts, comparison["latest_date"]) or {"metrics": {}}
    first = (plan.get("primary") or ["ndvi"])[0]
    fraction = ((latest_row.get("metrics") or {}).get(first) or {}).get("valid_fraction")
    if fraction is not None and fraction < 0.995:
        items.append(
            f"<b>未评价区域</b>：{cn_md(comparison['latest_date'])}有 {fmt_pct(1 - fraction)} 的像元"
            "因云、云影或缺测未作评价（图中灰色），巡田时一并查看。"
        )
    if not items:
        items.append("本期未发现需要优先核查的位置，按常规巡田即可。")
    items.append(
        "遥感指数只提示位置和变化，不能判定原因。数值偏低可能与出苗、土壤、水分、养分、病虫害等多种因素有关，"
        "以田间查看为准。"
    )
    nxt = next_period_sentence(facts)
    if nxt:
        items.append(nxt)
    return items


# ---------------------------------------------------------------- metrics page


def metric_definition_items(facts: dict[str, Any]) -> list[str]:
    """First-use explanation: full name plus one sentence on what the metric shows."""
    plan = facts.get("report_metrics") or {}
    items = []
    for role, metrics in (("主要指标", plan.get("primary") or []), ("辅助指标", plan.get("supporting") or [])):
        for metric in metrics:
            spec = METRIC_SPECS[metric]
            items.append(f"<b>{spec.label}</b>（{role}）：{spec.purpose}。")
    return items


def metric_spec_table(facts: dict[str, Any]) -> list[list[str]]:
    """Definition, valid range, fixed colour scale and reference line of each metric shown."""
    plan = facts.get("report_metrics") or {}
    metrics = [m for m in REPORT_METRIC_ORDER if m in (plan.get("primary") or []) + (plan.get("supporting") or [])]
    rows = [["指标", "计算式", "有效范围", "色标范围", "低值参考线"]]
    for metric in metrics:
        spec = METRIC_SPECS[metric]
        rows.append(
            [
                spec.abbr,
                spec.formula,
                f"{spec.valid_range[0]:g} 至 {spec.valid_range[1]:g}",
                f"{spec.display_range[0]:g} 至 {spec.display_range[1]:g}",
                fmt_index(spec.low_reference) if spec.low_reference is not None else "—",
            ]
        )
    return rows


def stage_metric_table(facts: dict[str, Any]) -> list[list[str]] | None:
    """The three growth phases with their metric sets; the current one is marked."""
    stage = stage_summary(facts)
    if stage["kind"] == "none":
        return None
    current = (facts.get("report_metrics") or {}).get("phase_label")
    rows = [["生长阶段", "包含的生育期", "主要指标", "辅助指标"]]
    for label in ("S1", "S2", "S3"):
        name = PHASE_FORMAL_NAMES[label] + ("（本期）" if label == current else "")
        rows.append(
            [
                f"<b>{name}</b>" if label == current else name,
                PHASE_STAGE_SPAN[label],
                "、".join(metric_abbr(m) for m in PHASE_METRICS[label]["primary"]),
                "、".join(metric_abbr(m) for m in PHASE_METRICS[label]["supporting"]),
            ]
        )
    return rows


def metric_choice_sentence(facts: dict[str, Any]) -> str:
    plan = facts.get("report_metrics") or {}
    stage = stage_summary(facts)
    primary = join_names([metric_abbr(m) for m in plan.get("primary") or []])
    if plan.get("basis") == "listed":
        return f"本期主要指标为{primary}，按上游下发的指标清单展示，不附生长阶段解释。"
    if plan.get("basis") == "default" or not plan.get("rationale"):
        if stage["kind"] == "none":
            return f"本田未关联种植方案，四项指标一并列出，田内分布图采用{primary}，不作分阶段判读。"
        return f"本期所处时期不按生长阶段选取指标，四项指标一并列出，田内分布图采用{primary}。"
    lead = f"本期按{plan.get('formal_name')}（{plan.get('stage_span')}）选取指标。"
    if plan.get("basis") == "latest_observation_phase":
        lead = f"本期跨越两个生长阶段，按最近一次有效观测所在的{plan.get('formal_name')}选取指标。"
    return lead + str(plan["rationale"])


def observation_table(facts: dict[str, Any]) -> list[list[str]]:
    """One row per optical pass: used dates with values, dropped dates with the reason."""
    statistics = facts.get("metric_statistics") or {}
    plan = facts.get("report_metrics") or {}
    metrics = [m for m in REPORT_METRIC_ORDER if m in (plan.get("primary") or []) + (plan.get("supporting") or [])]
    first = (plan.get("primary") or ["ndvi"])[0]
    header = ["观测日期", "时段", "云量", "有效像元占比"] + [metric_abbr(m) for m in metrics] + ["采用情况"]
    rows: list[tuple[str, list[str]]] = []
    for row in _series(facts):
        entry = row["metrics"].get(first) or {}
        values = []
        for metric in metrics:
            item = row["metrics"][metric]
            stat = statistics.get(metric)
            value = item.get("median") if (stat == "median" and item.get("status") == "ok") else None
            if stat == "mean":
                value = item.get("field_mean")
            values.append(fmt_index(value))
        cloud = f"{row['cloud_pct']:.0f}%" if row.get("cloud_pct") is not None else MISSING
        fraction = fmt_pct(entry.get("valid_fraction")) if row["source"] == "pixels" else "—"
        rows.append(
            (
                row["date"],
                [row["date"], "本期" if row["in_period"] else "前期", cloud, fraction, *values, "采用"],
            )
        )
    for row in (facts.get("data_quality") or {}).get("rejected") or []:
        cloud = f"{row['cloud_pct']:.0f}%" if row.get("cloud_pct") is not None else MISSING
        reason = REJECT_REASONS.get(row.get("reason") or "", "质量不符合要求")
        rows.append((row["date"], [row["date"], "本期", cloud, "—", *(["—"] * len(metrics)), f"未采用：{reason}"]))
    rows.sort(key=lambda item: item[0])
    return [header] + [cells for _, cells in rows]


def observation_caption(facts: dict[str, Any]) -> str:
    quality = facts.get("data_quality") or {}
    limit = quality.get("cloud_limit_pct")
    limit_text = f"{limit:.0f}%" if limit is not None else "限值"
    return (
        f"云量指地块范围内的云量，缺少时为整景云量；超过 {limit_text} 的观测不采用。"
        "有效像元占比指扣除云、云影和缺测后可用于统计的像元比例。前期观测仅用于对比。"
    )


# ---------------------------------------------------------------- in-field page


def infield_items(facts: dict[str, Any], item: dict[str, Any]) -> list[str]:
    metric = item["metric"]
    spec = METRIC_SPECS[metric]
    abbr = spec.abbr
    stats = item["stats"]
    panels = list(item.get("panels") or [])
    marks = {name: "①②③④"[i] for i, name in enumerate(panels)}
    day = cn_md(item["date"])
    lo, hi = spec.display_range
    abs_marks = "".join(marks[name] for name in ("baseline", "latest") if name in marks)
    level = (
        f"<b>绝对水平（图{abs_marks}）</b>：{day}{abbr}中位数 {fmt_index(stats['median'])}，"
        f"第10至第90百分位为 {fmt_index(stats['p10'])} 至 {fmt_index(stats['p90'])}"
    )
    if spec.low_reference is not None and stats.get("below_low_reference_fraction") is not None:
        share = stats["below_low_reference_fraction"]
        ref = fmt_index(spec.low_reference)
        if share == 0:
            level += f"；没有有效像元低于低值参考线 {ref}"
        elif share == 1:
            level += f"；全部有效像元低于低值参考线 {ref}"
        else:
            level += f"；{fmt_pct(share)} 的有效像元低于低值参考线 {ref}"
    level += f"。色标固定为 {lo:g} 至 {hi:g}，不同日期的图可直接比较。"
    items = [level]
    change = item.get("change")
    if change and "change" in marks:
        median = change["median"]
        rounded = round(median, 2)
        if rounded == 0:
            move = "变化量中位数为 0.00"
        else:
            share = change["increase_fraction"] if rounded > 0 else change["decrease_fraction"]
            word = "升高" if rounded > 0 else "降低"
            move = f"变化量中位数 {fmt_signed(median)}，{fmt_pct(share)} 的配对像元{word}"
        items.append(
            f"<b>变化量（图{marks['change']}）</b>：较{cn_md(item['baseline_date'])}，{move}"
            f"（两次均有效的像元 {change['n_paired']} 个）。"
        )
    elif item.get("baseline_date") is None:
        items.append("<b>变化量</b>：此前没有可逐像元对比的有效观测，本期不提供变化图。")
    relative = item.get("relative") or {}
    text = f"<b>田内相对差异（图{marks.get('relative', '')}）</b>：" + flagged_clause(item, with_metric=False)
    count = int(relative.get("clustered_pixels") or 0)
    below = relative.get("clustered_below_low_reference")
    if count and below is not None and spec.low_reference is not None:
        # 把「相对本田偏低」和「绝对值低」分开交代。
        if below == 0:
            text += "；成片像元的绝对值均不低于低值参考线"
        elif below == count:
            text += "；成片像元的绝对值均低于低值参考线"
        else:
            text += f"；成片像元中 {below} 个的绝对值同时低于低值参考线"
    also = relative.get("clustered_also_on_baseline")
    if count and also is not None:
        text += f"，{also} 个在{cn_md(item['baseline_date'])}同样显著低于当日本田整体水平"
    text += "。相对差异只表示与本田中位数相比的高低，不代表绝对数值低，也不说明原因。"
    items.append(text)
    if stats.get("n_total") and stats["n_valid"] < stats["n_total"]:
        items.append(
            f"<b>有效像元</b>：{day}共 {stats['n_total']} 个像元，{stats['n_valid']} 个有效"
            f"（{fmt_pct(stats['valid_fraction'])}），其余为云、云影或缺测，图中以灰色表示，未作评价。"
        )
    return items


# ---------------------------------------------------------------- weather page


def weather_items(facts: dict[str, Any]) -> list[str]:
    weather = facts.get("weather_period") or {}
    antecedent = facts.get("weather_antecedent") or {}
    items: list[str] = []
    sentence = weather_sentence(facts)
    if sentence:
        items.append(sentence.replace("<b>气象</b>：", "<b>本期</b>：", 1).replace("：本期", "：", 1))
    if antecedent.get("precipitation_mm") is not None or antecedent.get("et0_mm") is not None:
        days = int(antecedent.get("days") or 0)
        parts = []
        if antecedent.get("precipitation_mm") is not None:
            parts.append(
                f"降水量 {antecedent['precipitation_mm']:.1f} 毫米"
                + _coverage_note(antecedent.get("precipitation_days"), days)
            )
        if antecedent.get("et0_mm") is not None:
            parts.append(
                f"参考作物蒸散量（ET0）{antecedent['et0_mm']:.1f} 毫米"
                + _coverage_note(antecedent.get("et0_days"), days)
            )
        items.append(f"<b>本期开始前 {days} 天</b>：" + "，".join(parts) + "。")
    if weather.get("et0_mm") is not None:
        items.append(
            "参考作物蒸散量（ET0）是按气象条件估算的标准参考作物需水量，不等于本田实际耗水量。"
            "降水量与ET0的差值只反映气象条件，田间土壤水分状况以实地查看为准。"
        )
    source = {"open-meteo": "Open-Meteo 再分析资料", "nasa-power": "NASA POWER 资料"}.get(
        str(facts.get("weather_source") or ""), None
    )
    note = "气象数据为地块中心点的格点资料" + (f"（{source}）" if source else "") + "，不是田间气象站实测值。"
    if facts.get("weather_note"):
        note += str(facts["weather_note"])
    items.append(note)
    return items


def has_weather(facts: dict[str, Any]) -> bool:
    return bool(facts.get("weather_daily")) and weather_sentence(facts) is not None


# ---------------------------------------------------------------- surrounding page


def surrounding_items(facts: dict[str, Any]) -> list[str]:
    meta = facts.get("surrounding_comparison_meta") or {}
    items = []
    lead = surrounding_sentence(facts)
    if lead:
        items.append(lead.replace("<b>周边对照</b>：", "<b>对照结果</b>：", 1))
    items.append(
        "超绿指数（ExG）由真彩色影像的红、绿、蓝波段计算，反映可见光下的植被绿色程度；"
        "它与NDVI不是同一指标，数值不能相互比较。"
    )
    items.append(
        "周边农田由同日影像自动识别，不是项目登记地块，作物类型未经确认；"
        "对照只说明相对高低，不作同作物排名。"
    )
    cover = meta.get("cover_2km")
    if cover is not None and cover < 0.999:
        items.append(f"影像范围未完整覆盖本田周边2公里（覆盖约 {fmt_pct(cover)}），2公里范围的数值仅供参考。")
    return items


def has_surrounding(facts: dict[str, Any]) -> bool:
    return surrounding_sentence(facts) is not None


# ---------------------------------------------------------------- notes page


def method_items(facts: dict[str, Any]) -> list[str]:
    quality = facts.get("data_quality") or {}
    limit = quality.get("cloud_limit_pct")
    limit_text = f"{limit:.0f}%" if limit is not None else "限值"
    return [
        f"光学数据来自哨兵二号（Sentinel-2）卫星，分析网格约 10 米。只采用地块云量不超过 {limit_text} 的观测；"
        "被标记为云或云影的像元不参与统计。",
        "田块水平用有效像元的中位数表示，田内离散程度用第10至第90百分位表示，不采用最大值和最小值，"
        "以免个别像元的极端值影响结论。",
        "色标范围固定（见下表），不随单期或单个地块的数值范围调整，不同日期、不同地块的图可以直接比较。"
        "NDVI色标上限 1.0 是该指数的理论最大值，不是本田的长势目标；植被类指数小于 0 的像元多为水体、"
        "积雪等非植被，图中单独以蓝灰色表示。",
        "低值参考线是平台通用的参考值，未按作物和生育阶段校准，只用于描述数值水平，不作为诊断标准。",
        "“显著低于本田整体水平”按箱线图离群值规则判定（像元值低于“下四分位数减 1.5 倍四分位距”），"
        "是统计口径；不少于 4 个相邻像元（约 400 平方米）才视为成片，零星像元不作为核查位置。",
        "变化量为最近一次有效观测减对比基准观测，逐像元配对计算。",
    ]


def stage_note_items(facts: dict[str, Any]) -> list[str]:
    stage = stage_summary(facts)
    if stage["kind"] == "none":
        return ["本田未关联种植方案，本报告不作生育阶段判断，指标不按阶段选取。"]
    return [
        f"生育阶段{stage['source']}，实际生育进程可能早于或晚于计划。",
        "小麦全生育期分为三个生长阶段，各阶段选用的指标不同（见下表）。"
        f"阶段划分与指标选取规则（版本 {MAPPING_VERSION}）尚待农学审核；"
        "阶段背景中的“常见走向”是一般规律，不是对本田的实测结论。",
    ]


def omitted_items(facts: dict[str, Any]) -> list[str]:
    items = []
    if not has_surrounding(facts):
        reason = facts.get("surrounding_missing_reason") or "未能完成周边1—2公里农田对照"
        items.append(f"周边农田对照：{reason}，本期不提供。")
    if not has_weather(facts):
        items.append("气象条件：未取得本期气温和降水记录，本期不提供，也不对水分状况作判断。")
    plan = facts.get("report_metrics") or {}
    have = {item["metric"] for item in facts.get("infield") or [] if item.get("image")}
    missing = [metric_abbr(m) for m in (plan.get("primary") or []) if m not in have]
    if missing and facts.get("comparison"):
        items.append(f"田内空间分布（{join_names(missing)}）：没有足够的像元级有效数据，本期不提供分布图。")
    quality = facts.get("data_quality") or {}
    radar = int(quality.get("radar_count") or 0)
    if radar:
        items.append(f"雷达：本期有 {radar} 景哨兵一号雷达影像，本报告未用于指标计算，不作积水判断。")
    else:
        items.append("雷达：本期没有哨兵一号雷达影像，不作积水判断。")
    items.append("植被覆盖度、叶面积指数等未经地面标定的参数，本报告不提供。")
    return items


def limit_items(facts: dict[str, Any]) -> list[str]:
    end = period_end_inclusive(period_of(facts))
    items = []
    if is_demo(facts):
        items.append(
            "<b>本报告为示例数据生成的样稿，数值并非对任何真实地块的观测，不用于农事决策。</b>"
            + (str(facts.get("data_source_note") or ""))
        )
    if end:
        items.append(f"本报告只使用 {cn_date(end)}（北京时间）及此前的观测，不包含之后的情况。")
    items.append("本报告不估算受影响面积和产量，不判定病虫害、缺肥、干旱等原因。")
    items.append("本报告用于了解田间状况，不作为灾损认定、保险理赔或交易的依据。")
    return items
