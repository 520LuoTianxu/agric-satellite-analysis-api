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
from app.reports.biweekly.cover import compact_date_span, compose_official_cover
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



def _image_path(facts: dict, key: str) -> Path | None:
    raw = facts.get(key)
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.is_file() else None


def _variation_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value:
        return [str(value)]
    return []


def _draw_figure(c, path: Path | None, y: float, *, max_height: float) -> float:
    if not path or not path.is_file():
        return y
    height = min(max_height, max(y - 56, 80))
    c.drawImage(str(path), M - 4, y - height, width=CW + 8, height=height, preserveAspectRatio=True, mask="auto")
    return y - height - 10

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


def _date_span(period: dict, *, compact: bool = False) -> str:
    if compact:
        return compact_date_span(period)
    start = _day(period.get("start"))
    end = _day(period.get("end_inclusive") or period.get("end"))
    if not end:
        exclusive = _day(period.get("end_exclusive"))
        if exclusive:
            from datetime import date, timedelta

            end = (date.fromisoformat(exclusive) - timedelta(days=1)).isoformat()
    return f"{_cn_date(start)}—{_cn_date(end)}"


def _cn_date(day: str) -> str:
    parts = _day(day).split("-")
    if len(parts) != 3:
        return day
    year, month, day_n = parts
    return f"{int(year)}年{int(month)}月{int(day_n)}日"


def _cn_md(day: str) -> str:
    parts = _day(day).split("-")
    if len(parts) != 3:
        return day
    return f"{int(parts[1])}月{int(parts[2])}日"


def _report_metrics(facts: dict) -> list[str]:
    phase = _phase(facts)
    metrics = list(phase.get("primary_metrics") or []) + list(phase.get("supporting_metrics") or [])
    metrics = [m for m in metrics if m not in FORBIDDEN_REPORT_METRICS]
    if metrics:
        return metrics
    # No plan/stage selection: still show the four plain optical readings, without
    # turning them into a phenology judgment.
    observed = list(facts.get("observations") or []) + list(facts.get("prior_observations") or [])
    available = []
    for metric in ("ndvi", "evi", "ndre", "ndmi"):
        if any(_metric_value(row, metric) is not None for row in observed):
            available.append(metric)
    return available


def _sorted_obs(rows: list[dict]) -> list[dict]:
    return sorted((row for row in rows if _day(row.get("date"))), key=lambda row: _day(row.get("date")))


def _matplotlib_cn():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    regular, bold = resolve_yahei_fonts()
    font_manager.fontManager.addfont(str(regular))
    font_manager.fontManager.addfont(str(bold))
    name = font_manager.FontProperties(fname=str(regular)).get_name()
    plt.rcParams["font.family"] = name
    plt.rcParams["axes.unicode_minus"] = False
    return plt, font_manager.FontProperties(fname=str(regular)), font_manager.FontProperties(fname=str(bold))


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



METRIC_COLORS = {
    "ndvi": "#2E6B3C",
    "evi": "#7AA86D",
    "ndre": "#2F5E9E",
    "ndmi": "#A35B2C",
}
METRIC_MARKERS = {"ndvi": "o", "evi": "s", "ndre": "D", "ndmi": "^"}


def _metric_chart(facts: dict, dest: Path) -> Path | None:
    current = _sorted_obs(list(facts.get("observations") or []))
    prior = _sorted_obs(list(facts.get("prior_observations") or []))
    metrics = _report_metrics(facts)
    rows = prior + current
    if not metrics or not current:
        return None
    try:
        plt, yahei, yahei_b = _matplotlib_cn()
        fig, ax = plt.subplots(figsize=(8.4, 2.55), dpi=160)
        dates = [_cn_md(_day(row.get("date"))) for row in rows]
        x = list(range(len(rows)))
        drawn = 0
        series = {}
        for metric in metrics:
            ys = [_metric_value(row, metric) for row in rows]
            if all(v is None for v in ys):
                continue
            color = METRIC_COLORS.get(metric, "#333333")
            ax.plot(
                x,
                ys,
                color=color,
                linewidth=1.8,
                marker=METRIC_MARKERS.get(metric, "o"),
                markersize=6,
                label=_plain(metric),
                zorder=3,
            )
            series[metric] = ys
            drawn += 1
        if drawn == 0:
            plt.close(fig)
            return None
        # Point labels, staggered when values sit close together.
        for i in range(len(rows)):
            pts = sorted(
                ((metric, series[metric][i]) for metric in series if series[metric][i] is not None),
                key=lambda item: item[1],
            )
            if not pts:
                continue
            values = [item[1] for item in pts]
            span = max(values) - min(values) if values else 0.0
            offset = max(0.012, span * 0.08)
            dy = {metric: offset for metric, _ in pts}
            for (m0, y0), (m1, y1) in zip(pts, pts[1:]):
                if y1 - y0 < offset * 1.6:
                    dy[m0] = -offset
                    dy[m1] = offset
            for metric, yi in pts:
                va = "bottom" if dy[metric] > 0 else "top"
                ax.text(
                    i,
                    yi + dy[metric],
                    f"{yi:.2f}" if abs(yi) >= 0.1 else f"{yi:.3f}",
                    ha="center",
                    va=va,
                    fontsize=7.2,
                    color=METRIC_COLORS.get(metric, "#333333"),
                )
        split = len(prior) - 0.5 if prior else None
        if split is not None and split >= 0:
            ax.axvline(split, color="#B8C4BC", lw=0.9, ls="--", zorder=1)
            ax.axvspan(-0.4, split, color="#F3F1EC", zorder=0)
            ax.axvspan(split, len(rows) - 0.6, color="#E8EFEA", zorder=0)
            ymin = ax.get_ylim()[0]
            ax.text(max(split / 2.0, -0.2), ymin, "上一周期", ha="center", va="bottom", fontsize=8, color="#5A5A5A")
            ax.text((split + len(rows) - 0.6) / 2.0, ymin, "本期", ha="center", va="bottom", fontsize=8, color="#1F4D38")
            title = "本田四项读数：上一有遥感的周期 与 本期"
        else:
            title = "本田四项读数（本期）"
        ax.set_xticks(x, dates)
        ax.set_ylabel("读数")
        ax.set_title(title, loc="left", color="#1F4D38", fontsize=11, pad=8, fontproperties=yahei_b)
        ax.legend(frameon=False, ncol=min(4, drawn), loc="upper center", bbox_to_anchor=(0.5, -0.18), fontsize=8)
        ax.grid(axis="y", color="#E6EBE8", lw=0.6)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_color("#D5DDD8")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()
        dest.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(dest, dpi=160, facecolor="white", bbox_inches="tight")
        plt.close(fig)
        return dest if dest.is_file() else None
    except Exception:  # noqa: BLE001 — chart is optional; text still carries the judgment
        return None



def _weather_chart(facts: dict, dest: Path) -> Path | None:
    series = list(facts.get("weather_daily") or [])
    if not series:
        return None
    period = _period(facts)
    start = _day(period.get("start"))
    end = _day(period.get("end_inclusive") or period.get("end"))
    if not end:
        exclusive = _day(period.get("end_exclusive"))
        if exclusive:
            from datetime import date, timedelta

            end = (date.fromisoformat(exclusive) - timedelta(days=1)).isoformat()
    period_rows = [row for row in series if start and start <= _day(row.get("date")) <= (end or "9999-99-99")]
    antecedent_rows = [row for row in series if start and _day(row.get("date")) < start]
    if not period_rows and not antecedent_rows:
        period_rows = series
    try:
        plt, yahei, yahei_b = _matplotlib_cn()

        def _plot(ax, rows, title, *, shade_start: str | None = None, shade_end: str | None = None):
            days = [_day(row.get("date")) for row in rows]
            x = list(range(len(rows)))
            rain = [row.get("precipitation_mm") for row in rows]
            tmin = [row.get("min_c") for row in rows]
            tmax = [row.get("max_c") for row in rows]
            if any(v is not None for v in rain):
                ax.bar(x, [0 if v is None else v for v in rain], color="#8FB8D6", width=0.6, label="日降雨", zorder=2)
                ax.set_ylabel("降雨（毫米）")
            ax2 = ax.twinx()
            if any(v is not None for v in tmax):
                ax2.plot(x, tmax, color="#7A3E2E", linewidth=1.6, marker="o", markersize=2.4, label="日最高气温", zorder=3)
            if any(v is not None for v in tmin):
                ax2.plot(x, tmin, color="#1F4D38", linewidth=1.6, marker="o", markersize=2.4, label="日最低气温", zorder=3)
            ax2.set_ylabel("气温（摄氏度）")
            if shade_start and shade_end:
                xs = [i for i, day in enumerate(days) if shade_start <= day <= shade_end]
                if xs:
                    ax.axvspan(xs[0] - 0.5, xs[-1] + 0.5, color="#E8EFEA", zorder=0)
            step = max(1, len(x) // 8)
            ax.set_xticks(x[::step], [days[i][5:] for i in range(0, len(days), step)])
            ax.set_title(title, loc="left", color="#1F4D38", fontsize=10.5, pad=6, fontproperties=yahei_b)
            ax.spines["top"].set_visible(False)
            ax2.spines["top"].set_visible(False)
            handles, labels = [], []
            for axis in (ax, ax2):
                h, lab = axis.get_legend_handles_labels()
                handles.extend(h)
                labels.extend(lab)
            if handles:
                ax.legend(handles, labels, frameon=False, fontsize=7.5, loc="upper left", ncol=3)
            return ax

        if antecedent_rows and period_rows:
            fig, axes = plt.subplots(2, 1, figsize=(8.4, 6.4), dpi=150, sharex=False)
            _plot(axes[0], period_rows, f"本期（{_date_span(period, compact=True)}）日气温与降雨")
            _plot(
                axes[1],
                antecedent_rows + period_rows,
                f"本期开始前30天与本期（{_cn_md(antecedent_rows[0].get('date'))}—{_cn_md(period_rows[-1].get('date'))}）",
                shade_start=start,
                shade_end=end,
            )
            fig.suptitle(
                f"气温与降雨（截止 {_cn_date(end or period_rows[-1].get('date'))}，北京时间）",
                fontsize=12,
                color="#1F4D38",
                y=1.01,
                fontproperties=yahei_b,
            )
        else:
            fig, ax = plt.subplots(figsize=(8.4, 3.2), dpi=150)
            _plot(ax, period_rows or series, "本期日气温与降雨")
        fig.tight_layout()
        dest.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(dest, dpi=150, facecolor="white", bbox_inches="tight")
        plt.close(fig)
        return dest if dest.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def _join_names(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]}和{names[1]}"
    return "、".join(names[:-1]) + "和" + names[-1]


def _intra_period_trend(observations: list[dict], metrics: list[str]) -> str:
    rows = _sorted_obs(observations)
    if len(rows) < 2:
        return ""
    first, last = rows[0], rows[-1]
    rose, fell = [], []
    for metric in metrics:
        a, b = _metric_value(first, metric), _metric_value(last, metric)
        if a is None or b is None:
            continue
        if b > a + 1e-4:
            rose.append(_plain(metric))
        elif b < a - 1e-4:
            fell.append(_plain(metric))
    bits = []
    if rose:
        bits.append(_join_names(rose) + "小幅升高")
    if fell:
        bits.append(_join_names(fell) + "略有回落")
    if not bits:
        return "这两周内各项读数变化很小。"
    return "这两周内" + "，".join(bits) + "。"


def _prior_trend_phrase(facts: dict, metrics: list[str]) -> str:
    cmp_ = facts.get("prior_comparison") or {}
    items = cmp_.get("metrics") or {}
    higher, lower, missing = [], [], []
    for metric in metrics:
        row = items.get(metric) or {}
        name = row.get("plain_name") or _plain(metric)
        if row.get("status") != "compared":
            if metric in items:
                missing.append(name)
            continue
        (higher if row.get("higher") else lower).append(name)
    if not higher and not lower:
        return "没有上一有可用光学的遥感期对照，不能作出好转判断。"
    if higher and not lower:
        if cmp_.get("can_claim_sustained_improvement"):
            body = f"相对上一有可用光学的遥感期，{_join_names(higher)}都明显升高。"
        else:
            body = f"相对上一有可用光学的遥感期，{_join_names(higher)}升高。"
            if len(facts.get("observations") or []) < 2:
                body += "仅一次有效观测，不能写成持续改善。"
        return body
    parts = []
    if higher:
        parts.append(_join_names(higher) + "升高")
    if lower:
        parts.append(_join_names(lower) + "未升高")
    return "相对上一有可用光学的遥感期，" + "，".join(parts) + "。不能据此写成好转。"


def _surrounding_judgment(facts: dict) -> str | None:
    canned = facts.get("surrounding_judgment")
    if canned:
        return str(canned)
    meta = facts.get("surrounding_comparison_meta") or {}
    stats = (meta.get("stats") or {}) if isinstance(meta, dict) else {}
    by_date = meta.get("by_date") if isinstance(meta, dict) else None
    def _one(day: str | None, block: dict) -> str | None:
        field = (block.get("field") or {})
        ring = (block.get("surrounding_1km") or block.get("ring1") or {})
        fmean, rmean = field.get("mean"), ring.get("mean")
        if fmean is None or rmean is None:
            return None
        delta = fmean - rmean
        day_txt = _cn_md(day) if day else ""
        cmp_txt = "高于" if fmean > rmean else "不高于"
        return f"{day_txt}本田 {fmean:.2f} 对一公里 {rmean:.2f}（{cmp_txt} {abs(delta):.2f}）".replace("本田", " 本田" if day_txt else "本田").strip()

    pieces = []
    if isinstance(by_date, dict):
        for day, block in sorted(by_date.items()):
            bit = _one(day, block)
            if bit:
                pieces.append(bit)
    elif stats:
        bit = _one(meta.get("date"), stats)
        if bit:
            pieces.append(bit)
    if not pieces:
        if facts.get("surrounding_comparison"):
            return None  # page 4 has detail; judgment will use a short fallback below
        return ""
    first_stats = stats or ((list(by_date.values())[0] if by_date else {}) or {})
    field_mean = ((first_stats.get("field") or {}).get("mean"))
    ring_mean = ((first_stats.get("surrounding_1km") or {}).get("mean"))
    direction = "高于" if field_mean is not None and ring_mean is not None and field_mean > ring_mean else "不高于"
    if field_mean is not None and ring_mean is not None and all(
        ((block.get("field") or {}).get("mean") or 0) > ((block.get("surrounding_1km") or {}).get("mean") or 0)
        for block in (by_date.values() if by_date else [stats])
        if (block.get("field") or {}).get("mean") is not None
    ):
        direction = "高于"
        lead = f"{J}本田绿度高于周边田地。{E}"
    elif direction == "不高于":
        lead = f"{B}本田可见光绿度不高于周边田地。{E}"
    else:
        lead = f"{J}本田绿度高于周边田地。{E}"
    return lead + "；".join(pieces) + "。此项不能与上图直接加减。"


def _spatial_judgment(facts: dict) -> str | None:
    canned = facts.get("spatial_judgment")
    if canned:
        body = str(canned)
        if "<font" not in body:
            body = body.replace("田内并不均匀。", "")
            return f"{R}田内并不均匀。{E}{body}"
        return body
    meta = facts.get("spatial_variation_meta") or {}
    summary = meta.get("summary") if isinstance(meta, dict) else None
    if summary:
        return f"{R}田内并不均匀。{E}{summary}"
    if facts.get("spatial_variation") or facts.get("spatial_variation_image"):
        return f"{R}田内并不均匀。{E}偏低或变差的位置见后页图。整体读数升高，不等于各处都已好转。"
    return ""


def _judgment_bullets(facts: dict) -> list[str]:
    phase = _phase(facts)
    observations = _sorted_obs(list(facts.get("observations") or []))
    display = phase.get("display_phase")
    phase_name = phase.get("phase_name") or (PHASE_LABELS.get(display) if display else None)
    primary = [m for m in (phase.get("primary_metrics") or []) if m not in FORBIDDEN_REPORT_METRICS]
    supporting = [m for m in (phase.get("supporting_metrics") or []) if m not in FORBIDDEN_REPORT_METRICS]
    metrics = _report_metrics(facts)
    items: list[str] = []
    if phase.get("display_phases_if_split"):
        items.append(f"{J}这两周跨越计划中的两个阶段，不把读数合成一个阶段均值。{E}" + _plan_clause(phase))
    elif display and phase_name:
        names = []
        for row in phase.get("observation_stages") or []:
            name = row.get("stage_code")
            if name and name not in names:
                names.append(name)
        stage_txt = "、".join(names) if names else "计划阶段"
        extra = "。"
        trans = phase.get("transitions") or []
        if trans:
            bits = []
            for row in trans:
                day, code = _day(row.get("date") or row.get("start")), row.get("stage_code") or row.get("label")
                if day and code:
                    bits.append(f"{_cn_md(day)}起进入{code}")
            if bits:
                extra = "，" + "，".join(bits) + "。"
        items.append(
            f"{J}这两周主要处于计划中的{stage_txt}（{phase_name}）{extra}{E}"
            + _plan_clause(phase)
            + f"本期有 {len(observations)} 次可用光学观测。"
        )
    else:
        items.append(_plan_clause(phase) + f"本期有 {len(observations)} 次可用光学观测。")

    if primary:
        focus = _join_names([_plain(m) for m in primary])
        support = _join_names([_plain(m) for m in supporting]) if supporting else ""
        head = f"{J}{stage_label(phase, display, phase_name)}更应看{focus}"
        if support:
            head += f"，并对照{support}"
        head += "。"
        head += _prior_trend_phrase(facts, primary + supporting)
        intra = _intra_period_trend(observations, primary + supporting)
        if intra:
            head += intra
        head += E
        items.append(head)
    elif metrics and observations:
        head = f"{J}{_prior_trend_phrase(facts, metrics)}"
        intra = _intra_period_trend(observations, metrics)
        if intra:
            head += intra
        head += E
        head += "这只说明田间光学读数的变化，不作为生育期或三阶段判断。"
        items.append(head)
    else:
        items.append("本期没有按阶段选定的主指标，也不具备可比较的田间光学读数，不把各项读数合成一个阶段结论。")

    surround = _surrounding_judgment(facts)
    if surround:
        items.append(surround)
    elif not facts.get("surrounding_comparison") and not facts.get("surrounding_comparison_image"):
        items.append("周边一至二公里田地对照缺失。对照须使用影像中识别的田块，不使用项目登记地块，因此本期不作周边高低判断。")

    spatial = _spatial_judgment(facts)
    if spatial:
        items.append(spatial)
    elif not facts.get("spatial_variation") and not facts.get("spatial_variation_image"):
        items.append(f"{R}田内偏低或变差的位置图缺失。{E}不把缺失图件说成田内已经均匀，也不换算受灾面积。")

    weather = facts.get("weather_period") or {}
    rain = weather.get("precipitation_mm")
    et0 = weather.get("et0_mm")
    radar_bit = ""
    if int((_period(facts).get("s1_products") or facts.get("s1_count") or 0) or 0) == 0 and not facts.get("in_period_radar"):
        radar_bit = "本期无雷达影像，无法判断是否积水。"
    if rain is None and et0 is None:
        items.append("本期气温与降雨记录缺失，不对水分状况作出偏低或偏高的结论。" + radar_bit)
    elif rain is not None and et0 is not None and et0 > rain:
        items.append(
            f"{R}这两周降雨 {_fmt(rain, 1)} 毫米，田间耗水约 {_fmt(et0, 1)} 毫米，存在失墒压力，尚不能确认旱害。{E}{radar_bit}"
        )
    elif rain is not None and et0 is not None:
        items.append(
            f"这两周降雨 {_fmt(rain, 1)} 毫米，田间耗水约 {_fmt(et0, 1)} 毫米。这只能说明水分账，不能代替田间墒情，也不能确认旱害。" + radar_bit
        )
    elif rain is not None:
        items.append(f"这两周降雨 {_fmt(rain, 1)} 毫米。田间耗水记录缺失，不能据此确认旱害或排除失墒。" + radar_bit)
    else:
        items.append("本期降雨记录缺失，不对水分状况作出偏低或偏高的结论。" + radar_bit)
    return items


def stage_label(phase: dict, display: str | None, phase_name: str | None) -> str:
    names = []
    for row in phase.get("observation_stages") or []:
        code = row.get("stage_code")
        if code and code not in names:
            names.append(code)
    if names:
        return names[0]
    return phase_name or "本期"


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
        focus = _join_names([_plain(m) for m in primary]) if primary else "已选定的阶段指标"
        label = f"{stage}" + (f"（{phase_name}）" if phase_name else "")
        source = "参照计划" if phase.get("plan_source") == "reference" else "本田计划"
        items.append(
            f"{J}按{source}已进入{label}，{E}更应看{focus}；该阶段不能当成已核实的本田物候，仍需结合田间查看。"
        )
    spatial = (facts.get("spatial_variation_meta") or {}).get("next_week")
    if spatial:
        items.append(f"{R}{spatial}{E}不能仅凭影像决定灌溉量。")
    elif facts.get("spatial_variation") or facts.get("spatial_variation_image"):
        items.append(f"{R}核对田内偏低或变差的部位，并核对这些部位的墒情。{E}不能仅凭影像决定灌溉量。")
    elif not facts.get("spatial_variation"):
        items.append("田内偏低或变差位置图缺失，下一周如需核对局部差异，仍应结合田间查看，不能仅凭影像决定灌溉量。")
    if not facts.get("in_period_radar") and int((_period(facts).get("s1_products") or facts.get("s1_count") or 0) or 0) == 0:
        items.append("本期没有雷达影像，不能判断积水，也不能把缺少影像当成内涝风险很低。")
    return items or ["下一周应当关注的事项，须结合本期已有观测到田间核对，不对尚未发生的影像作出预判。"]


def _notes(facts: dict) -> list[str]:
    phase = _phase(facts)
    period = _period(facts)
    end = _day(period.get("end_inclusive") or period.get("end"))
    if not end:
        exclusive = _day(period.get("end_exclusive"))
        if exclusive:
            from datetime import date, timedelta

            end = (date.fromisoformat(exclusive) - timedelta(days=1)).isoformat()
    items = [_plan_clause(phase)]
    if phase.get("plan_source") == "reference" or phase.get("phenology_basis") == "PLAN_DERIVED_FROM_BTU_REFERENCE":
        ref = phase.get("reference") or facts.get("reference") or {}
        disclosure = ref.get("disclosure")
        if disclosure:
            items.append(str(disclosure))
        else:
            items.append("正文中的生育期来自调用方明确传入的参照方案，须按参照阅读，不是本田已关联方案。")
    if not facts.get("in_period_radar") and int((period.get("s1_products") or facts.get("s1_count") or 0) or 0) == 0:
        note = "本期没有雷达影像，不对内涝风险作出偏低或偏高的结论。"
        prior_radar = facts.get("sar_context_dates") or period.get("sar_context_dates") or []
        if prior_radar:
            days = "、".join(_cn_md(d) for d in prior_radar)
            note += f"{days}的雷达观测在本期开始之前，只能说明前期情况。"
        items.append(note)
    items.append(
        f"报告只使用 {_cn_date(end)} 23 时 59 分（北京时间）及此前的观测。下一周应当关注的事项，是根据本期已有观测提出的核对方向，不是对 {_cn_date(end)} 以后影像的预判。"
        if end
        else "报告只使用本期截止日及此前的观测（北京时间）。"
    )
    items.append("没有经过地面标定的覆盖度和已验证的叶面积，正文不给出这类数值。")
    items.append("田内偏低和变差范围只说明位置，不换算受灾亩数，也不推断病虫害或灌溉原因。")
    items.append("判断页上的本田跨周期读数，与周边对照页的可见光绿度，不是同一套数值，不能直接加减。周边比较若给出，只使用影像中识别的田块，不是项目登记地块对照，也不是同作物排名；周边湿度缺少可靠计算条件时，不给出对比。")
    items.append("结论供了解田间情况参考，不作为受灾认定、保险理赔或交易的依据。")
    # Promote the plan clause to colored emphasis on the first sentence, as in v8.
    if items:
        clause = items[0]
        if "本田未关联" in clause or "尚未关联" in clause:
            items[0] = f"{B}{clause[:clause.find('，')+1] if '，' in clause else clause}{E}" + (
                clause[clause.find('，') + 1 :] if "，" in clause else ""
            )
        elif "阶段来自本田方案" in clause:
            items[0] = f"{B}阶段来自本田方案的计划窗口，{E}" + clause.split("，", 1)[-1] if "，" in clause else f"{B}{clause}{E}"
    return items


def _draw_cover(c, facts: dict, land_title: str, *, cover_image: Path | None = None) -> None:
    """Draw the formal v8-style cover from the bundled background when available."""
    if cover_image and Path(cover_image).is_file():
        c.drawImage(str(cover_image), 0, 0, width=W, height=H, preserveAspectRatio=False, mask="auto")
        return
    # typographic fallback only if the bundled artwork cannot be composed
    period = _period(facts)
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
    c.drawString(M, H - 74 * mm, _date_span(period, compact=True))
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
    weather_chart = _weather_chart(facts, fig_dir / "weather.png") if facts.get("weather_daily") else None
    cover_image = None
    try:
        cover_image = compose_official_cover(facts, fig_dir / "cover.png", land_title=land_title)
    except Exception:
        cover_image = None

    c = canvas.Canvas(str(dest_path), pagesize=A4)
    c.setTitle(f"{land_title} 种植双周长势报告（{_date_span(period, compact=True)}）")
    c.setAuthor("乡合农服")
    c.setSubject("地块双周长势正式报告")

    _draw_cover(c, facts, land_title, cover_image=cover_image)
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
        nxt_label = f"下一周应当关注（{_cn_md(nxt['start'])}—{_cn_md(nxt.get('end_exclusive'))}）"
    y = subhead(c, nxt_label, y)
    y = bullets(c, _next_week_bullets(facts), y, leading=18, sp=9)
    primary = [m for m in (_phase(facts).get("primary_metrics") or []) if m not in FORBIDDEN_REPORT_METRICS]
    if primary:
        caption = (
            "上图是本田同一套田间光学观测。"
            + _join_names([_plain(m) for m in primary])
            + "是"
            + stage_label(_phase(facts), _phase(facts).get("display_phase"), _phase(facts).get("phase_name"))
            + "更应看重的指标。与周边的对比是另一套读数，只比相对高低。不作同作物排名。"
        )
    else:
        caption = "上图若给出，是本田同一套田间光学观测。与周边的对比若给出，是另一套读数，只比相对高低。不作同作物排名。"
    y = flow_html(
        c,
        caption,
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
    spatial_items = _variation_items(facts.get("spatial_variation"))
    spatial_image = _image_path(facts, "spatial_variation_image")
    if spatial_items or spatial_image:
        if spatial_items:
            y = bullets(c, spatial_items, y, leading=17, sp=6)
        y = _draw_figure(c, spatial_image, y, max_height=455)
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
    surround_items = _variation_items(facts.get("surrounding_comparison"))
    surround_image = _image_path(facts, "surrounding_comparison_image")
    if surround_items or surround_image:
        if surround_items:
            y = bullets(c, surround_items, y, leading=17, sp=5)
        y = _draw_figure(c, surround_image, y, max_height=365)
        contrast = _image_path(facts, "surrounding_contrast_image")
        y = _draw_figure(c, contrast, y, max_height=155)
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
        period_days = weather.get("days") or (_period(facts).get("days") or 14)
        text = f"{B}本期 {period_days} 天"
        if rain is not None:
            text += f"降雨 {_fmt(rain, 1)} 毫米"
        if et0 is not None:
            text += ("，" if rain is not None else "") + f"同期田间耗水约 {_fmt(et0, 1)} 毫米"
        text += f"。{E}"
        if antecedent.get("precipitation_mm") is not None or antecedent.get("et0_mm") is not None:
            text += "开始前 30 天"
            if antecedent.get("precipitation_mm") is not None:
                text += f"降雨 {_fmt(antecedent['precipitation_mm'], 1)} 毫米"
            if antecedent.get("et0_mm") is not None:
                text += ("，" if antecedent.get("precipitation_mm") is not None else "") + f"耗水约 {_fmt(antecedent['et0_mm'], 1)} 毫米"
            text += "。"
        if rain is not None and et0 is not None and et0 > rain:
            text += "降雨均少于同期耗水，这是判断存在失墒压力的依据。"
        weather_bits.append(text)
        weather_bits.append(f"{R}这只能说明存在失墒压力，{E}不能代替田间墒情，也不能确认旱害。下图为本期逐日气温、降雨，以及开始前 30 天对照。" if (antecedent or weather) else "下图为本期逐日气温、降雨。")
    else:
        weather_bits.append(f"{R}本期气温与降雨记录缺失。{E}不对水分状况作出偏低或偏高的结论，也不用其他参数填补。")
    if not facts.get("weather_daily"):
        pass
    elif not any("下图" in item for item in weather_bits):
        weather_bits.append("下图为本期逐日气温、降雨，以及可获得的对照日。缺测日期不补估。")
    y = bullets(c, weather_bits, y, leading=17, sp=8)
    if weather_chart and weather_chart.is_file():
        ch = 520
        c.drawImage(str(weather_chart), M - 6, y - ch, width=CW + 12, height=ch, preserveAspectRatio=True, mask="auto")
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
