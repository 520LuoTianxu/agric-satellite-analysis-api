"""Render an official six-page winter-wheat biweekly PDF in the v8 style.

The report is driven only by generate facts. Missing weather, in-field
maps, or surrounding farmland contrast are disclosed as missing. FVC, LAI,
rankings, and disaster area are never invented.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.phase_policy import (
    FORBIDDEN_REPORT_METRICS,
    PHASE_LABELS,
    PLAIN_NAMES,
)
from app.reports.biweekly.fonts import resolve_yahei_fonts

W, H = A4
M = 48
CW = W - 2 * M
JUDGE = HexColor("#1F4D38")
RISK = HexColor("#7A3E2E")
RULE = HexColor("#D0D0D0")
DECOR = HexColor("#1F4D38")
INK = HexColor("#000000")
J = '<b><font color="#1F4D38" size="11">'
R = '<b><font color="#7A3E2E" size="11">'
B = '<b><font color="#000000" size="11">'
E = "</font></b>"

_FONTS_READY = False


def _register_fonts() -> None:
    global _FONTS_READY
    if _FONTS_READY:
        return
    regular, bold = resolve_yahei_fonts()
    names = set(pdfmetrics.getRegisteredFontNames())
    if "YaHei" not in names:
        pdfmetrics.registerFont(TTFont("YaHei", str(regular)))
    if "YaHei-Bold" not in names:
        pdfmetrics.registerFont(TTFont("YaHei-Bold", str(bold)))
    pdfmetrics.registerFontFamily("YaHei", normal="YaHei", bold="YaHei-Bold")
    _FONTS_READY = True


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _plain(metric: str) -> str:
    return PLAIN_NAMES.get(metric, metric)


def _period(facts: dict) -> dict:
    return dict(facts.get("period") or {})


def _phase(facts: dict) -> dict:
    return dict(facts.get("phase") or {})


def _land_title(facts: dict) -> str:
    name = str(facts.get("land_name") or "").strip()
    land_id = str(facts.get("land_id") or "").strip()
    if name:
        return name
    return f"地块 {land_id}" if land_id else "地块"


def _plan_clause(phase: dict) -> str:
    if phase.get("plan_source") == "reference" or phase.get("phenology_basis") == "PLAN_DERIVED_FROM_BTU_REFERENCE":
        ref = phase.get("reference") or {}
        label = ref.get("name") or "另一方案"
        return f"阶段来自参照方案（{label}），不是本田已关联的方案，也不是实地确认的物候。"
    if phase.get("plan_source") == "native" or phase.get("plan_label") == "本田方案":
        return "阶段来自本田方案的计划窗口，不是实地确认的物候。"
    return "本田未关联种植方案，正文不给出生育期或三阶段判断。"


def _metric_value(row: dict, metric: str) -> float | None:
    if metric in FORBIDDEN_REPORT_METRICS:
        return None
    raw = row.get(f"{metric}_avg")
    if raw is None:
        raw = row.get(metric)
    if raw is None and isinstance(row.get("metrics"), dict):
        raw = row["metrics"].get(metric)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _mean(rows: list[dict], metric: str) -> float | None:
    values = [v for v in (_metric_value(row, metric) for row in rows) if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "缺失"
    return f"{value:.{digits}f}"


def _date_span(period: dict) -> str:
    start = _day(period.get("start"))
    end = _day(period.get("end_inclusive") or period.get("end_exclusive"))
    return f"{_cn_date(start)}—{_cn_date(end)}"


def _cn_date(day: str) -> str:
    parts = _day(day).split("-")
    if len(parts) != 3:
        return day
    year, month, day_n = parts
    return f"{int(year)}年{int(month)}月{int(day_n)}日"


def heading(c, no: str, title: str, y: float) -> float:
    c.setFillColor(INK)
    c.setFont("YaHei-Bold", 14)
    c.drawString(M, y, f"{no}  {title}")
    c.setStrokeColor(RULE)
    c.setLineWidth(0.6)
    c.line(M, y - 8, W - M, y - 8)
    return y - 30


def subhead(c, title: str, y: float) -> float:
    c.setFillColor(INK)
    c.setFont("YaHei-Bold", 11.5)
    c.drawString(M, y, title)
    return y - 22


def footer(c, page: int, total: int, land_title: str) -> None:
    c.setStrokeColor(RULE)
    c.setLineWidth(0.5)
    c.line(M, 36, W - M, 36)
    c.setFillColor(INK)
    c.setFont("YaHei", 8)
    c.drawString(M, 22, f"乡合农服  ·  种植双周报  ·  {land_title}")
    c.drawRightString(W - M, 22, f"{page} / {total}")


def header_bar(c, title: str) -> None:
    c.setFillColor(DECOR)
    c.rect(M, H - 32, 22, 3, fill=1, stroke=0)
    c.setFont("YaHei", 9)
    c.setFillColor(INK)
    c.drawString(M, H - 48, "种植双周长势报告")
    c.drawRightString(W - M, H - 48, title)


def flow_html(c, html: str, x: float, y: float, width: float = CW, size: float = 10.5, leading: float = 18, sp: float = 8) -> float:
    style = ParagraphStyle(
        "p",
        fontName="YaHei",
        fontSize=size,
        leading=leading,
        textColor=INK,
        wordWrap="CJK",
    )
    para = Paragraph(html, style)
    _, height = para.wrap(width, 2000)
    para.drawOn(c, x, y - height)
    return y - height - sp


def bullets(c, items: list[str], y: float, indent: float = 16, size: float = 10.5, leading: float = 18, sp: float = 8) -> float:
    style = ParagraphStyle(
        "b",
        fontName="YaHei",
        fontSize=size,
        leading=leading,
        textColor=INK,
        wordWrap="CJK",
        leftIndent=indent,
        firstLineIndent=-indent,
    )
    for text in items:
        html = f"{B}·{E}  {text}"
        para = Paragraph(html, style)
        _, height = para.wrap(CW, 2000)
        para.drawOn(c, M, y - height)
        y = y - height - sp
    return y


def _metric_chart(facts: dict, dest: Path) -> Path | None:
    observations = list(facts.get("observations") or [])
    phase = _phase(facts)
    metrics = list(phase.get("primary_metrics") or []) + list(phase.get("supporting_metrics") or [])
    metrics = [m for m in metrics if m not in FORBIDDEN_REPORT_METRICS]
    if not metrics or not observations:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager

        regular, _bold = resolve_yahei_fonts()
        font_manager.fontManager.addfont(str(regular))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular)).get_name()
        plt.rcParams["axes.unicode_minus"] = False
        fig, ax = plt.subplots(figsize=(8.4, 2.4), dpi=140)
        dates = [_day(row.get("date"))[5:] for row in observations]
        x = list(range(len(observations)))
        drawn = 0
        for metric in metrics:
            ys = [_metric_value(row, metric) for row in observations]
            if all(v is None for v in ys):
                continue
            ax.plot(x, ys, marker="o", linewidth=1.8, label=_plain(metric))
            drawn += 1
        if drawn == 0:
            plt.close(fig)
            return None
        prior = (facts.get("prior_comparison") or {}).get("metrics") or {}
        for metric, item in prior.items():
            if not isinstance(item, dict) or item.get("status") != "compared":
                continue
            if item.get("prior_mean") is None:
                continue
            ax.axhline(item["prior_mean"], linestyle="--", linewidth=0.8, alpha=0.45)
        ax.set_xticks(x)
        ax.set_xticklabels(dates)
        ax.set_ylabel("本期田间均值")
        ax.legend(frameon=False, ncols=min(4, drawn), fontsize=8, loc="upper left")
        ax.grid(axis="y", linestyle=":", alpha=0.4)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()
        dest.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(dest, dpi=140, facecolor="white")
        plt.close(fig)
        return dest
    except Exception:  # noqa: BLE001 — chart is optional; text still carries numbers
        return None


def _weather_chart(series: list[dict], dest: Path) -> Path | None:
    if not series:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager

        regular, _bold = resolve_yahei_fonts()
        font_manager.fontManager.addfont(str(regular))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular)).get_name()
        plt.rcParams["axes.unicode_minus"] = False
        fig, ax1 = plt.subplots(figsize=(8.4, 2.6), dpi=140)
        days = [_day(row.get("date"))[5:] for row in series]
        x = list(range(len(series)))
        rain = [row.get("precipitation_mm") for row in series]
        tmin = [row.get("min_c") for row in series]
        tmax = [row.get("max_c") for row in series]
        if any(v is not None for v in rain):
            ax1.bar(x, [0 if v is None else v for v in rain], color="#8FB8D6", width=0.6, label="降雨毫米")
            ax1.set_ylabel("降雨（毫米）")
        ax2 = ax1.twinx()
        if any(v is not None for v in tmax):
            ax2.plot(x, tmax, color="#7A3E2E", linewidth=1.6, label="最高气温")
        if any(v is not None for v in tmin):
            ax2.plot(x, tmin, color="#1F4D38", linewidth=1.6, label="最低气温")
        ax2.set_ylabel("气温（℃）")
        ax1.set_xticks(x[:: max(1, len(x) // 10)])
        ax1.set_xticklabels([days[i] for i in range(0, len(days), max(1, len(x) // 10))])
        ax1.spines["top"].set_visible(False)
        ax2.spines["top"].set_visible(False)
        h1, l1 = ax1.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax1.legend(h1 + h2, l1 + l2, frameon=False, fontsize=8, loc="upper left")
        fig.tight_layout()
        dest.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(dest, dpi=140, facecolor="white")
        plt.close(fig)
        return dest
    except Exception:  # noqa: BLE001
        return None


def _judgment_bullets(facts: dict) -> list[str]:
    phase = _phase(facts)
    period = _period(facts)
    observations = list(facts.get("observations") or [])
    display = phase.get("display_phase")
    phase_name = phase.get("phase_name") or (PHASE_LABELS.get(display) if display else None)
    primary = [m for m in (phase.get("primary_metrics") or []) if m not in FORBIDDEN_REPORT_METRICS]
    supporting = [m for m in (phase.get("supporting_metrics") or []) if m not in FORBIDDEN_REPORT_METRICS]
    items: list[str] = []
    if phase.get("display_phases_if_split"):
        items.append(
            f"{J}这两周跨越计划中的两个阶段，不把读数合成一个阶段均值。{E}"
            + _plan_clause(phase)
        )
    elif display and phase_name:
        stage = phase.get("observation_stages") or []
        names = []
        for row in stage:
            name = row.get("stage_code")
            if name and name not in names:
                names.append(name)
        stage_txt = "、".join(names) if names else "计划阶段"
        items.append(
            f"{J}这两周主要处于计划中的{stage_txt}（{phase_name}）。{E}"
            + _plan_clause(phase)
            + f"本期有 {len(observations)} 次可用光学观测。"
        )
    else:
        items.append(_plan_clause(phase) + f"本期有 {len(observations)} 次可用光学观测。")

    if primary:
        focus = "、".join(_plain(m) for m in primary)
        support = "、".join(_plain(m) for m in supporting) if supporting else ""
        head = f"{J}本期更应看{focus}"
        if support:
            head += f"，并对照{support}"
        head += f"。{E}"
        bits = []
        for metric in primary + supporting:
            current = _mean(observations, metric)
            if current is None:
                bits.append(f"{_plain(metric)}缺失")
                continue
            bits.append(f"{_plain(metric)}为 {_fmt(current)}")
        cmp_ = facts.get("prior_comparison") or {}
        metrics_cmp = cmp_.get("metrics") or {}
        improved = []
        declined = []
        missing = []
        for metric in primary + supporting:
            item = metrics_cmp.get(metric) or {}
            if item.get("status") != "compared":
                if metric in metrics_cmp:
                    missing.append(_plain(metric))
                continue
            name = item.get("plain_name") or _plain(metric)
            delta = item.get("delta")
            if item.get("higher"):
                improved.append(f"{name}升高 {_fmt(delta)}")
            else:
                declined.append(f"{name}未升高（变化 {_fmt(delta)}）")
        if cmp_.get("can_claim_sustained_improvement"):
            head += f"{J}相对上一有可用光学的遥感期，对比项均升高，且本期有两次及以上有效观测。{E}"
        elif cmp_.get("all_compared_higher") and not cmp_.get("can_claim_sustained_improvement"):
            head += f"{J}相对上一有可用光学的遥感期，对比项均升高。{E}仅一次有效观测，不能写成持续改善。"
        elif improved or declined:
            head += "相对上一有可用光学的遥感期，" + "；".join(improved + declined) + "。"
            if not cmp_.get("can_claim_sustained_improvement") and len(observations) < 2:
                head += "仅一次有效观测，不能写成持续改善。"
        else:
            head += "没有上一有可用光学的遥感期对照，不能作出好转判断。"
        if bits:
            head += "本期田间均值：" + "，".join(bits) + "。"
        items.append(head)
    else:
        items.append("本期没有按阶段选定的主指标，不把各项读数合成一个阶段结论。")

    surround = facts.get("surrounding_comparison")
    if not surround:
        items.append("周边一至二公里田地对照缺失。对照须使用影像中识别的田块，不使用项目登记地块，因此本期不作周边高低判断。")
    spatial = facts.get("spatial_variation")
    if not spatial:
        items.append(f"{R}田内偏低或变差的位置图缺失。{E}不把缺失图件说成田内已经均匀，也不换算受灾面积。")

    weather = facts.get("weather_period") or {}
    rain = weather.get("precipitation_mm")
    et0 = weather.get("et0_mm")
    if rain is None and et0 is None:
        items.append("本期气温与降雨记录缺失，不对水分状况作出偏低或偏高的结论。")
    elif rain is not None and et0 is not None:
        gap = et0 - rain
        if gap > 0:
            items.append(
                f"{R}这两周降雨 {_fmt(rain, 1)} 毫米，田间耗水约 {_fmt(et0, 1)} 毫米，存在失墒压力，尚不能确认旱害。{E}"
            )
        else:
            items.append(
                f"这两周降雨 {_fmt(rain, 1)} 毫米，田间耗水约 {_fmt(et0, 1)} 毫米。这只能说明水分账，不能代替田间墒情，也不能确认旱害。"
            )
    else:
        if rain is not None:
            items.append(f"这两周降雨 {_fmt(rain, 1)} 毫米。田间耗水记录缺失，不能据此确认旱害或排除失墒。")
        else:
            items.append("本期降雨记录缺失，不对水分状况作出偏低或偏高的结论。")

    if int((_period(facts).get("s1_products") or facts.get("s1_count") or 0) or 0) == 0 and not facts.get("in_period_radar"):
        items.append("本期没有雷达影像，无法判断是否积水，也不能把缺少影像当成内涝风险很低。")
    return items


def _next_week_bullets(facts: dict) -> list[str]:
    phase = _phase(facts)
    nxt = phase.get("next_week") or {}
    items: list[str] = []
    if nxt.get("start"):
        start, end = _cn_date(nxt["start"]), _cn_date(nxt.get("end_exclusive"))
        stage = nxt.get("stage_code") or "下一阶段"
        phase_label = nxt.get("phase_label")
        phase_name = PHASE_LABELS.get(phase_label) if phase_label else None
        primary = [m for m in (nxt.get("primary_metrics") or []) if m not in FORBIDDEN_REPORT_METRICS]
        focus = "、".join(_plain(m) for m in primary) if primary else "已选定的阶段指标"
        label = f"{stage}" + (f"（{phase_name}）" if phase_name else "")
        source = "参照计划" if phase.get("plan_source") == "reference" else "本田计划"
        items.append(
            f"{J}按{source}，下一周（{start}—{end}）进入{label}，{E}更应看{focus}；该阶段不能当成已核实的本田物候，仍需结合田间查看。"
        )
    if not facts.get("spatial_variation"):
        items.append("田内偏低或变差位置图缺失，下一周如需核对局部差异，仍应结合田间查看，不能仅凭影像决定灌溉量。")
    if not facts.get("in_period_radar") and int((_period(facts).get("s1_products") or facts.get("s1_count") or 0) or 0) == 0:
        items.append("本期没有雷达影像，不能判断积水，也不能把缺少影像当成内涝风险很低。")
    return items or ["下一周应当关注的事项，须结合本期已有观测到田间核对，不对尚未发生的影像作出预判。"]


def _notes(facts: dict) -> list[str]:
    phase = _phase(facts)
    period = _period(facts)
    cutoff = str(facts.get("cutoff") or (period.get("end_exclusive") or "") + "T00:00:00+08:00")
    items = [_plan_clause(phase)]
    if phase.get("plan_source") == "reference" or phase.get("phenology_basis") == "PLAN_DERIVED_FROM_BTU_REFERENCE":
        ref = phase.get("reference") or {}
        disclosure = ref.get("disclosure")
        if disclosure:
            items.append(str(disclosure))
        else:
            items.append("正文中的生育期来自调用方明确传入的参照方案，须按参照阅读，不是本田已关联方案。")
    if not facts.get("in_period_radar") and int((period.get("s1_products") or facts.get("s1_count") or 0) or 0) == 0:
        items.append("本期没有雷达影像，不对内涝风险作出偏低或偏高的结论。")
    items.append(f"报告只使用 {_cn_date(cutoff[:10])} 及此前的观测（北京时间）。下一周应当关注的事项，是根据本期已有观测提出的核对方向，不是对之后影像的预判。")
    items.append("没有经过地面标定的覆盖度和已验证的叶面积，正文不给出这类数值。")
    items.append("田内偏低和变差范围只在有可靠图件时说明位置，不换算受灾亩数，也不推断病虫害或灌溉原因。")
    items.append("判断页上的本田跨周期读数，与周边对照不是同一套数值，不能直接加减。周边比较若给出，只使用影像中识别的田块，不是项目登记地块对照，也不是同作物排名。")
    items.append("结论供了解田间情况参考，不作为受灾认定、保险理赔或交易的依据。")
    return items


def _draw_cover(c, facts: dict, land_title: str) -> None:
    period = _period(facts)
    phase = _phase(facts)
    c.setFillColor(DECOR)
    c.rect(0, H - 28 * mm, W, 28 * mm, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("YaHei", 11)
    c.drawString(M, H - 16 * mm, "乡合农服")
    c.setFont("YaHei-Bold", 22)
    c.drawString(M, H - 24 * mm, "种植双周长势报告")
    c.setFillColor(INK)
    c.setFont("YaHei-Bold", 20)
    c.drawString(M, H - 60 * mm, land_title)
    c.setFont("YaHei", 13)
    c.drawString(M, H - 74 * mm, _date_span(period))
    plan = "本田方案"
    if phase.get("plan_source") == "reference" or phase.get("phenology_basis") == "PLAN_DERIVED_FROM_BTU_REFERENCE":
        plan = "参照方案（须按参照阅读）"
    elif phase.get("plan_source") not in {"native", "reference"} and not phase.get("plan_label"):
        plan = "未关联种植方案"
    elif phase.get("plan_label"):
        plan = str(phase.get("plan_label"))
        if phase.get("plan_source") == "reference":
            plan = f"参照方案 · {plan}"
    c.setFont("YaHei-Bold", 12)
    c.drawString(M, H - 94 * mm, plan)
    display = phase.get("display_phase")
    phase_name = phase.get("phase_name") or PHASE_LABELS.get(display)
    if display and phase_name:
        c.setFont("YaHei", 12)
        c.drawString(M, H - 108 * mm, f"本期主要阶段：{phase_name}")
    c.setFillColor(DECOR)
    c.rect(M, 38 * mm, 18, 3, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont("YaHei", 9)
    policy = (_phase(facts).get("policy_version") or facts.get("policy_version") or "").strip()
    mapping = (_phase(facts).get("mapping_version") or "").strip()
    meta = "  ·  ".join(item for item in (policy, mapping) if item)
    c.drawString(M, 30 * mm, meta or "种植双周正式报告")
    footer(c, 1, 6, land_title)


def render_official_biweekly_pdf(facts: dict[str, Any], dest: str | Path, *, figures_dir: Path | None = None) -> Path:
    """Write a six-page official PDF from generate facts. Returns the output path."""
    if not facts:
        raise ReportError("GENERATE_FAILED")
    _register_fonts()
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fig_dir = Path(figures_dir or (dest_path.parent / f"{dest_path.stem}-figures"))
    fig_dir.mkdir(parents=True, exist_ok=True)
    land_title = _land_title(facts)
    period = _period(facts)
    total = 6
    metric_chart = _metric_chart(facts, fig_dir / "period_metrics.png")
    weather_rows = list(facts.get("weather_daily") or [])
    weather_chart = _weather_chart(weather_rows, fig_dir / "weather.png") if weather_rows else None

    c = canvas.Canvas(str(dest_path), pagesize=A4)
    c.setTitle(f"{land_title} 种植双周长势报告（{_date_span(period)}）")
    c.setAuthor("乡合农服")
    c.setSubject("地块双周长势正式报告")

    _draw_cover(c, facts, land_title)
    c.showPage()

    header_bar(c, "本期判断")
    y = H - 78
    y = heading(c, "01", "这两周的综合判断", y)
    y = subhead(c, "结论", y)
    bullets_all = _judgment_bullets(facts)
    y = bullets(c, bullets_all[:2], y, leading=18, sp=9)
    if metric_chart and metric_chart.is_file():
        ch = 168
        c.drawImage(str(metric_chart), M - 4, y - ch, width=CW + 8, height=ch, preserveAspectRatio=True, mask="auto")
        y = y - ch - 12
    y = bullets(c, bullets_all[2:], y, leading=18, sp=9)
    y -= 8
    nxt = (_phase(facts).get("next_week") or {})
    nxt_label = "下一周应当关注"
    if nxt.get("start"):
        nxt_label = f"下一周应当关注（{_cn_date(nxt['start'])}—{_cn_date(nxt.get('end_exclusive'))}）"
    y = subhead(c, nxt_label, y)
    y = bullets(c, _next_week_bullets(facts), y, leading=18, sp=9)
    y = flow_html(
        c,
        "上图若给出，是本田同一套田间光学观测。主要指标因生长阶段而异，不以单一指标概括各期。与周边的对比若给出，采用另一项指标，只比较相对高低。不作同作物排名。",
        M,
        y,
        size=8.5,
        leading=13.5,
        sp=4,
    )
    footer(c, 2, total, land_title)
    c.showPage()

    header_bar(c, "田内偏低与变差的位置")
    y = H - 78
    y = heading(c, "02", "田内需要关注的位置", y)
    if facts.get("spatial_variation"):
        y = bullets(c, [str(facts["spatial_variation"])], y, leading=17, sp=6)
    else:
        y = bullets(
            c,
            [
                f"{R}本期缺少可用于标明田内偏低或变差位置的像元图。{E}不把缺失图件理解为田内已经均匀。",
                "因此，即使整体读数相对上一周期有变化，也不能据此指出某一局部已经好转。不换算受灾面积，也不推断病虫害或灌溉原因。",
            ],
            y,
            leading=17,
            sp=8,
        )
    footer(c, 3, total, land_title)
    c.showPage()

    header_bar(c, "周边田地对照")
    y = H - 78
    y = heading(c, "03", "周边一至二公里长势对照", y)
    if facts.get("surrounding_comparison"):
        y = bullets(c, [str(facts["surrounding_comparison"])], y, leading=17, sp=6)
    else:
        y = bullets(
            c,
            [
                f"{R}本期缺少周边一至二公里田地对照。{E}该项须使用同一天影像中识别出的田块，并画在一张卫星图上，不能改用项目登记地块，也不作同作物排名。",
                "因此本期不对「本田是否高于周边」作出判断。判断页上的本田跨周期读数不能代替周边对照。",
            ],
            y,
            leading=17,
            sp=8,
        )
    footer(c, 4, total, land_title)
    c.showPage()

    header_bar(c, "气温与降雨")
    y = H - 78
    y = heading(c, "04", "气温、降雨与水分", y)
    weather = facts.get("weather_period") or {}
    antecedent = facts.get("weather_antecedent") or {}
    weather_bits = []
    if weather.get("precipitation_mm") is not None or weather.get("et0_mm") is not None:
        rain = weather.get("precipitation_mm")
        et0 = weather.get("et0_mm")
        text = "本期记录："
        if rain is not None:
            text += f"降雨 {_fmt(rain, 1)} 毫米"
        if et0 is not None:
            text += ("，" if rain is not None else "") + f"田间耗水约 {_fmt(et0, 1)} 毫米"
        text += "。"
        if antecedent.get("precipitation_mm") is not None or antecedent.get("et0_mm") is not None:
            text += "开始前 30 天："
            if antecedent.get("precipitation_mm") is not None:
                text += f"降雨 {_fmt(antecedent['precipitation_mm'], 1)} 毫米"
            if antecedent.get("et0_mm") is not None:
                text += ("，" if antecedent.get("precipitation_mm") is not None else "") + f"耗水约 {_fmt(antecedent['et0_mm'], 1)} 毫米"
            text += "。"
        if rain is not None and et0 is not None and et0 > rain:
            weather_bits.append(f"{R}{text}降雨少于同期耗水，这是判断存在失墒压力的依据。{E}这不能代替田间墒情，也不能确认旱害。")
        else:
            weather_bits.append(text + "这只能说明水分账，不能代替田间墒情，也不能确认旱害。")
    else:
        weather_bits.append(f"{R}本期气温与降雨记录缺失。{E}不对水分状况作出偏低或偏高的结论，也不用其他参数填补。")
    if weather_rows:
        weather_bits.append("下图为本期及可获得的对照日气温、降雨。缺测日期不补估。")
    y = bullets(c, weather_bits, y, leading=17, sp=8)
    if weather_chart and weather_chart.is_file():
        ch = 240
        c.drawImage(str(weather_chart), M - 4, y - ch, width=CW + 8, height=ch, preserveAspectRatio=True, mask="auto")
    footer(c, 5, total, land_title)
    c.showPage()

    header_bar(c, "说明")
    y = H - 78
    y = heading(c, "05", "阅读说明", y)
    y = bullets(c, _notes(facts), y, size=10.5, leading=17.5, sp=8)
    footer(c, 6, total, land_title)
    c.save()
    if not dest_path.is_file() or dest_path.stat().st_size < 1000:
        raise ReportError("GENERATE_FAILED")
    return dest_path
