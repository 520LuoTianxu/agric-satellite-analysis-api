"""Structured facts for the official biweekly report: tables, captions and method notes.

结论、田间说明、田内、周边和气象的成段文字不在这里用条件拼句，由 narrative_ai 撰写。
这里只保留程序算好的数值表、指标定义和固定的方法说明。
缺测写「缺测」，不以 0 代替。S1/S2/S3 不直接出现在正文，使用中文阶段名。
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
)

MISSING = "缺测"
DEMO_NOTICE = "示例数据 · 非实测 · 不用于农事决策"
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


# ---------------------------------------------------------------- evidence table


def evidence_table(facts: dict[str, Any]) -> list[list[str]] | None:
    comparison = facts.get("comparison")
    plan = facts.get("report_metrics") or {}
    if not comparison:
        return None
    latest_day, baseline_day = (
        comparison["latest_date"],
        comparison.get("baseline_date"),
    )
    latest_row = _row_on(facts, latest_day) or {"metrics": {}}
    header = [
        "指标",
        f"{cn_md(latest_day)}",
        f"{cn_md(baseline_day)}" if baseline_day else "对比基准",
        "变化量",
        "第10至第90百分位",
    ]
    rows = [header]
    for role, metrics in (
        ("主要", plan.get("primary") or []),
        ("辅助", plan.get("supporting") or []),
    ):
        for metric in metrics:
            item = (comparison.get("metrics") or {}).get(metric) or {}
            entry = (latest_row.get("metrics") or {}).get(metric) or {}
            spec = METRIC_SPECS[metric]
            spread = _percentile_spread(entry) or "—"
            rows.append(
                [
                    f"{spec.abbr}（{role}）",
                    fmt_index(item.get("latest")),
                    fmt_index(item.get("baseline")) if baseline_day else "—",
                    fmt_signed(item.get("delta")) if item.get("delta") is not None else "—",
                    spread,
                ]
            )
    return rows


def _percentile_spread(entry: dict[str, Any] | None) -> str | None:
    """Show p10–p90 only when the latest observation actually has them.

    Series values may still use field means when an earlier date lacks pixels;
    that must not hide real percentiles of the date shown in the table.
    """
    if not entry or entry.get("status") != "ok":
        return None
    p10, p90 = entry.get("p10"), entry.get("p90")
    if p10 is None or p90 is None:
        return None
    return f"{fmt_index(p10)} 至 {fmt_index(p90)}"


def _latest_has_percentiles(facts: dict[str, Any]) -> bool:
    comparison = facts.get("comparison") or {}
    latest = _row_on(facts, comparison.get("latest_date"))
    if not latest:
        return False
    plan = facts.get("report_metrics") or {}
    metrics = (plan.get("primary") or []) + (plan.get("supporting") or [])
    mets = latest.get("metrics") or {}
    return any(_percentile_spread(mets.get(metric) or {}) for metric in metrics)


def evidence_caption(facts: dict[str, Any]) -> str:
    plan = facts.get("report_metrics") or {}
    statistics = facts.get("metric_statistics") or {}
    kinds = {statistics.get(m) for m in (plan.get("primary") or []) + (plan.get("supporting") or [])} - {None}
    has_spread = _latest_has_percentiles(facts)
    if kinds == {"median"}:
        basis = "数值为田块内有效像元的中位数"
    elif kinds == {"mean"} and has_spread:
        basis = "数值为田块均值（因部分日期只有田块均值，时间序列统一用均值以便各日期可比）；百分位来自最近一次观测的有效像元"
    elif kinds == {"mean"}:
        basis = "数值为田块均值（无像元级数据，未提供百分位）"
    elif has_spread:
        basis = "有像元统计的指标数值为田块内有效像元的中位数，其余为田块均值；百分位来自最近一次观测中确有像元统计的指标"
    else:
        basis = "数值为田块内有效像元的中位数；无像元级数据的指标为田块均值"
    return f"{basis}，指数均无量纲。变化量为最近一次观测减对比基准。“—”表示不适用，“{MISSING}”表示没有有效数据。"


# ---------------------------------------------------------------- metrics page


def metric_definition_items(facts: dict[str, Any]) -> list[str]:
    """First-use explanation: full name plus one sentence on what the metric shows."""
    plan = facts.get("report_metrics") or {}
    items = []
    for role, metrics in (
        ("主要指标", plan.get("primary") or []),
        ("辅助指标", plan.get("supporting") or []),
    ):
        for metric in metrics:
            spec = METRIC_SPECS[metric]
            items.append(f"<b>{spec.label}</b>（{role}）：{spec.purpose}。")
    return items


def metric_spec_table(facts: dict[str, Any]) -> list[list[str]]:
    """Definition, valid range, fixed colour scale and reference line of each metric shown."""
    plan = facts.get("report_metrics") or {}
    metrics = [m for m in REPORT_METRIC_ORDER if m in (plan.get("primary") or []) + (plan.get("supporting") or [])]
    rows = [["指标", "计算式", "有效范围", "色标范围"]]
    for metric in metrics:
        spec = METRIC_SPECS[metric]
        rows.append(
            [
                spec.abbr,
                spec.formula,
                f"{spec.valid_range[0]:g} 至 {spec.valid_range[1]:g}",
                f"{spec.display_range[0]:g} 至 {spec.display_range[1]:g}",
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
                [
                    row["date"],
                    "本期" if row["in_period"] else "前期",
                    cloud,
                    fraction,
                    *values,
                    "采用",
                ],
            )
        )
    for row in (facts.get("data_quality") or {}).get("rejected") or []:
        cloud = f"{row['cloud_pct']:.0f}%" if row.get("cloud_pct") is not None else MISSING
        reason = REJECT_REASONS.get(row.get("reason") or "", "质量不符合要求")
        rows.append(
            (
                row["date"],
                [
                    row["date"],
                    "本期",
                    cloud,
                    "—",
                    *(["—"] * len(metrics)),
                    f"未采用：{reason}",
                ],
            )
        )
    rows.sort(key=lambda item: item[0])
    return [header] + [cells for _, cells in rows]


def observation_caption(facts: dict[str, Any]) -> str:
    quality = facts.get("data_quality") or {}
    limit = quality.get("cloud_limit_pct")
    limit_text = f"{limit:.0f}%" if limit is not None else "限值"
    return f"云量指地块范围内的云量，缺少时为整景云量；超过 {limit_text} 的观测不采用。有效像元占比指扣除云、云影和缺测后可用于统计的像元比例。前期观测仅用于对比。"


def has_weather(facts: dict[str, Any]) -> bool:
    period = facts.get("weather_period") or {}
    return bool(facts.get("weather_daily")) and (period.get("precipitation_mm") is not None or period.get("et0_mm") is not None)


def has_surrounding(facts: dict[str, Any]) -> bool:
    meta = facts.get("surrounding_comparison_meta") or {}
    return bool(facts.get("surrounding_comparison_image")) and bool(meta.get("comparisons"))


# ---------------------------------------------------------------- notes page


def method_items(facts: dict[str, Any]) -> list[str]:
    quality = facts.get("data_quality") or {}
    limit = quality.get("cloud_limit_pct")
    limit_text = f"{limit:.0f}%" if limit is not None else "限值"
    return [
        f"光学数据来自哨兵二号（Sentinel-2）卫星，分析网格约 10 米。只采用地块云量不超过 {limit_text} 的观测；被标记为云或云影的像元不参与统计。",
        "田块水平用有效像元的中位数表示，田内离散程度用第10至第90百分位表示，不采用最大值和最小值，以免个别像元的极端值影响结论。",
        "色标范围固定（见下表），不随单期或单个地块的数值范围调整，不同日期、不同地块的图可以直接比较。"
        "NDVI色标上限 1.0 是该指数的理论最大值，不是本田的长势目标；植被类指数小于 0 的像元多为水体、"
        "积雪等非植被，图中单独以蓝灰色表示。",
        "“显著低于本田整体水平”按箱线图离群值规则判定（像元值低于“下四分位数减 1.5 倍四分位距”），是统计口径；不少于 4 个相邻像元（约 400 平方米）才视为成片，零星像元不作为核查位置。",
        "变化量为最近一次有效观测减对比基准观测，逐像元配对计算。",
    ]


def stage_note_items(facts: dict[str, Any]) -> list[str]:
    stage = stage_summary(facts)
    if stage["kind"] == "none":
        return ["本田未关联种植方案，本报告不作生育阶段判断，指标不按阶段选取。"]
    return [
        f"生育阶段{stage['source']}，实际生育进程可能早于或晚于计划。",
        f"小麦全生育期分为三个生长阶段，各阶段选用的指标不同（见下表）。阶段划分与指标选取规则（版本 {MAPPING_VERSION}）尚待农学审核；阶段背景中的“常见走向”是一般规律，不是对本田的实测结论。",
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
        items.append("<b>本报告为示例数据生成的样稿，数值并非对任何真实地块的观测，不用于农事决策。</b>" + (str(facts.get("data_source_note") or "")))
    if end:
        items.append(f"本报告只使用 {cn_date(end)}（北京时间）及此前的观测，不包含之后的情况。")
    items.append("本报告不估算受影响面积和产量，不判定病虫害、缺肥、干旱等原因。")
    items.append("本报告用于了解田间状况，不作为灾损认定、保险理赔或交易的依据。")
    return items
