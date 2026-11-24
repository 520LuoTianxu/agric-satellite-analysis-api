"""Render the official winter-wheat biweekly PDF.

版面顺序：封面 → 本期结论 → 田里哪一块不一样 → 和周围的农田比 → 指标变化
→ 气象条件（水分状况并入此页）→ 说明。两节图在指标变化前面，各自编号。
没有数据的章节整节省略，并在「说明」页的「本期未提供的内容」中列出原因，不留空页，
也不用 0 或占位图代替。页数随内容变化，页脚页码按实际总页数生成。

The report is driven only by generate facts. FVC, LAI, rankings and disaster
area are never invented.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    HRFlowable,
    KeepTogether,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from agric_satellite_analysis_common.biweekly.metric_specs import METRIC_SPECS
from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.phase_policy import FORBIDDEN_REPORT_METRICS
from app.reports.biweekly import report_text as text
from app.reports.biweekly.cover import (
    COVER_FOOTER_CONTACTS,
    COVER_FOOTER_DISCLAIMER,
    COVER_FOOTER_ORG,
    compact_date_span,
    compose_official_cover,
    crop_badge_text,
    wrap_footer_text,
)
from app.reports.biweekly.enrichment import ensure_report_inputs, series_value
from app.reports.biweekly.fonts import resolve_yahei_fonts
from app.reports.biweekly.narrative_ai import (
    infield_line,
    judgment_paragraphs,
    reader_note,
    section_lines,
)

W, H = A4
M = 48
CW = W - 2 * M
TOP = 64
BOTTOM = 50
INK = HexColor("#000000")
MUTED = HexColor("#4A4A4A")
RULE = HexColor("#BFC5C1")
DECOR = HexColor("#1F4D38")
HEAD_BG = HexColor("#EEF2EF")
NOTICE = HexColor("#9C2B1B")

_FONTS_READY = False


# reportlab 的行首禁则表只收了半角和日文标点，没有全角的逗号、分号、冒号等，
# 中文段落会出现以「；」「，」开头的行。这里把常用全角标点补进去（该标点改为悬挂在上一行行尾）。
_FULLWIDTH_CANNOT_START = "，；：！？）》〉」』】〕”’％、。"


def _extend_line_start_rules() -> None:
    from reportlab.lib import textsplit
    from reportlab.platypus import paragraph

    for module in (textsplit, paragraph):
        current = module.ALL_CANNOT_START
        missing = "".join(ch for ch in _FULLWIDTH_CANNOT_START if ch not in current)
        if missing:
            module.ALL_CANNOT_START = current + missing


def _register_fonts() -> None:
    global _FONTS_READY
    if _FONTS_READY:
        return
    _extend_line_start_rules()
    regular, bold = resolve_yahei_fonts()
    names = set(pdfmetrics.getRegisteredFontNames())
    if "YaHei" not in names:
        pdfmetrics.registerFont(TTFont("YaHei", str(regular)))
    if "YaHei-Bold" not in names:
        pdfmetrics.registerFont(TTFont("YaHei-Bold", str(bold)))
    pdfmetrics.registerFontFamily("YaHei", normal="YaHei", bold="YaHei-Bold")
    _FONTS_READY = True


def _styles() -> dict[str, ParagraphStyle]:
    base = {"fontName": "YaHei", "textColor": INK, "wordWrap": "CJK"}
    return {
        "body": ParagraphStyle("body", fontSize=10, leading=16, spaceAfter=4, **base),
        "bullet": ParagraphStyle(
            "bullet", fontSize=10, leading=16, spaceAfter=4, leftIndent=12, bulletIndent=0, **base
        ),
        "note": ParagraphStyle(
            "note", fontSize=9.2, leading=14.5, spaceAfter=3, leftIndent=12, bulletIndent=0, **base
        ),
        "small": ParagraphStyle("small", fontSize=8.5, leading=13, spaceAfter=4, **{**base, "textColor": MUTED}),
        "h1": ParagraphStyle("h1", fontSize=14, leading=20, spaceAfter=2, **{**base, "fontName": "YaHei-Bold"}),
        "h2": ParagraphStyle(
            "h2", fontSize=11, leading=17, spaceBefore=8, spaceAfter=4, **{**base, "fontName": "YaHei-Bold"}
        ),
        "cell": ParagraphStyle("cell", fontSize=8.8, leading=13, **base),
        "cell_head": ParagraphStyle("cell_head", fontSize=8.8, leading=13, **{**base, "fontName": "YaHei-Bold"}),
        "label": ParagraphStyle("label", fontSize=9.5, leading=15, **{**base, "fontName": "YaHei-Bold"}),
        "value": ParagraphStyle("value", fontSize=9.5, leading=15, **base),
    }


class FitImage(Flowable):
    """An image at full text width that shrinks to the space left on the page.

    剩余空间不足以放下最小尺寸时，返回完整高度，由版面引擎换页，
    不会压到页脚，也不会被截断。
    """

    def __init__(self, path: str | Path, *, max_width: float = CW, max_height: float = 600, min_scale: float = 0.72):
        super().__init__()
        from PIL import Image as PILImage

        self.path = str(path)
        with PILImage.open(self.path) as img:
            px_w, px_h = img.size
        self.aspect = px_h / max(px_w, 1)
        self.full_w = max_width
        self.full_h = max_width * self.aspect
        if self.full_h > max_height:
            self.full_h = max_height
            self.full_w = max_height / self.aspect
        self.min_scale = min_scale
        self.draw_w, self.draw_h = self.full_w, self.full_h
        self.hAlign = "CENTER"
        self.spaceAfter = 6

    def wrap(self, avail_width, avail_height):
        scale = 1.0
        if self.full_h > avail_height:
            fit = avail_height / self.full_h
            scale = fit if fit >= self.min_scale else 1.0
        self.draw_w, self.draw_h = self.full_w * scale, self.full_h * scale
        self.width, self.height = self.draw_w, self.draw_h
        return self.draw_w, self.draw_h

    def draw(self):
        self.canv.drawImage(self.path, 0, 0, width=self.draw_w, height=self.draw_h, mask="auto")


def _numbered_canvas(*, title: str, right: str, demo: bool):
    """Canvas that draws header, footer and page numbers once the total is known."""

    class NumberedCanvas(rl_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_pages: list[dict] = []

        def showPage(self):
            self._saved_pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved_pages)
            for index, state in enumerate(self._saved_pages, start=1):
                self.__dict__.update(state)
                self._decorate(index, total)
                super().showPage()
            super().save()

        def _decorate(self, page: int, total: int) -> None:
            if demo:
                # 示例数据水印：每页都有，截取任何一页都能看出不是实测。
                self.saveState()
                self.setFillColor(NOTICE)
                self.setFillAlpha(0.07)
                self.setFont("YaHei-Bold", 30)
                self.translate(W / 2, H / 2)
                self.rotate(32)
                self.drawCentredString(0, 0, text.DEMO_NOTICE)
                self.restoreState()
            if page == 1:
                return
            self.saveState()
            self.setFillColor(DECOR)
            self.rect(M, H - 30, 22, 3, fill=1, stroke=0)
            self.setFillColor(INK)
            self.setFont("YaHei", 8.5)
            left = "种植双周长势报告"
            self.drawString(M, H - 45, left)
            used = pdfmetrics.stringWidth(left, "YaHei", 8.5)
            if demo:
                self.setFillColor(NOTICE)
                self.setFont("YaHei-Bold", 8.5)
                notice = f"  ·  {text.DEMO_NOTICE}"
                self.drawString(M + used, H - 45, notice)
                used += pdfmetrics.stringWidth(notice, "YaHei-Bold", 8.5)
                self.setFillColor(INK)
                self.setFont("YaHei", 8.5)
            # 地块名过长时截短，页眉左右两段不相互覆盖。
            label = right
            room = CW - used - 16
            while len(label) > 4 and pdfmetrics.stringWidth(label, "YaHei", 8.5) > room:
                label = label[:-2].rstrip() + "…"
            self.drawRightString(W - M, H - 45, label)
            self.setStrokeColor(RULE)
            self.setLineWidth(0.5)
            self.line(M, H - 52, W - M, H - 52)
            self.line(M, 36, W - M, 36)
            self.setFillColor(INK)
            self.setFont("YaHei", 8)
            self.drawString(M, 22, f"乡合农服  ·  种植双周报  ·  {title}")
            self.drawRightString(W - M, 22, f"{page} / {total}")
            self.restoreState()

    return NumberedCanvas


# ---------------------------------------------------------------- charts


def _matplotlib_cn():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    regular, bold = resolve_yahei_fonts()
    font_manager.fontManager.addfont(str(regular))
    font_manager.fontManager.addfont(str(bold))
    plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular)).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    return plt, font_manager.FontProperties(fname=str(bold))


def trend_y_limits(
    display_range: tuple[float, float],
    values: list[float | None],
    lows: list[float | None] | None = None,
    highs: list[float | None] | None = None,
) -> tuple[float, float, bool]:
    """Axis range that keeps every finite point, percentile and its label inside the axes.

    The colour-scale range is always included. The axis grows past that range when an
    observation sits outside it, and always keeps a label margin above the highest mark.
    Missing values are ignored rather than drawn as zero.
    """
    lo, hi = display_range
    span = hi - lo if hi > lo else 1.0
    finite: list[float] = []
    for value in list(values) + list(lows or []) + list(highs or []):
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number and number not in (float("inf"), float("-inf")):
            finite.append(number)
    data_min = min(finite) if finite else lo
    data_max = max(finite) if finite else hi
    # 0.16 倍色标跨度大约够一行 7.5 磅标签；底部留空放未采用过境的灰叉，不占用数值。
    margin = 0.16 * span
    gutter = 0.10 * span
    y0 = min(lo, data_min) - gutter
    y1 = max(hi, data_max + margin)
    expanded = data_min < lo - 1e-9 or data_max > hi + 1e-9
    return y0, y1, expanded


def _boxes_overlap(left, right, pad: float = 2.0) -> bool:
    return not (
        left.x1 + pad < right.x0
        or right.x1 + pad < left.x0
        or left.y1 + pad < right.y0
        or right.y1 + pad < left.y0
    )


def _place_point_labels(ax, items: list[tuple[Any, float, str]]) -> None:
    """Put value labels inside the axes and away from each other.

    ``items`` are ``(x, y, text)`` in data coordinates. Candidates are tried in
    display points so the choice follows the real glyph box, not a guessed width.
    """
    if not items:
        return
    fig = ax.figure
    renderer = fig.canvas.get_renderer()
    ax_box = ax.get_window_extent(renderer)
    placed = []
    candidates = ((0, 4), (0, -11), (12, 5), (-12, 5), (0, 15), (12, -12), (-12, -12), (0, -20))
    for x, y, label in items:
        chosen = None
        chosen_box = None
        for dx, dy in candidates:
            probe = ax.annotate(
                label,
                (x, y),
                xytext=(dx, dy),
                textcoords="offset points",
                ha="center",
                va="bottom" if dy >= 0 else "top",
                fontsize=7.2,
                color="#000000",
                annotation_clip=True,
                zorder=5,
            )
            fig.canvas.draw()
            box = probe.get_window_extent(renderer).padded(0.4)
            inside = (
                box.x0 >= ax_box.x0 - 0.5
                and box.x1 <= ax_box.x1 + 0.5
                and box.y0 >= ax_box.y0 - 0.5
                and box.y1 <= ax_box.y1 + 0.5
            )
            if inside and not any(_boxes_overlap(box, other) for other in placed):
                chosen = probe
                chosen_box = box
                break
            probe.remove()
        if chosen is None:
            # 没有完全避开的位置时仍放在点的上方，并保证落在纵轴范围内。
            chosen = ax.annotate(
                label,
                (x, y),
                xytext=(0, 4),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=7.2,
                color="#000000",
                annotation_clip=True,
                zorder=5,
            )
            fig.canvas.draw()
            chosen_box = chosen.get_window_extent(renderer)
        placed.append(chosen_box)


def _report_metric_roles(facts: dict) -> list[tuple[str, str]]:
    plan = facts.get("report_metrics") or {}
    roles = [(m, "主要指标") for m in plan.get("primary") or []]
    roles += [(m, "辅助指标") for m in plan.get("supporting") or []]
    return [(m, role) for m, role in roles if m not in FORBIDDEN_REPORT_METRICS and m in METRIC_SPECS]


def _trend_chart(facts: dict, dest: Path) -> Path | None:
    """Small multiples. The y-axis keeps the colour scale and every finite observation."""
    series = list(facts.get("metric_series") or [])
    roles = _report_metric_roles(facts)
    statistics = facts.get("metric_statistics") or {}
    if not series or not roles:
        return None
    period = text.period_of(facts)
    start, end = text.as_day(period.get("start")), text.period_end_inclusive(period)
    rejected = [row["date"] for row in (facts.get("data_quality") or {}).get("rejected") or []]
    try:
        plt, bold = _matplotlib_cn()
        from matplotlib.lines import Line2D
        from matplotlib.patches import Patch

        def day(value: str) -> date:
            return date.fromisoformat(value)

        ncols = 2 if len(roles) > 1 else 1
        nrows = (len(roles) + ncols - 1) // ncols
        # 面板加高，给百分位竖线上方的数值标签留出空档，避免顶到标题或被图框裁掉。
        panel_h = 2.45
        total_h = panel_h * nrows + 0.78
        fig, axes = plt.subplots(nrows, ncols, figsize=(7.2, total_h), dpi=160, squeeze=False)
        all_days = [day(row["date"]) for row in series] + [day(d) for d in rejected]
        if start:
            all_days.append(day(start))
        if end:
            all_days.append(day(end))
        x_lo, x_hi = min(all_days) - timedelta(days=2), max(all_days) + timedelta(days=2)
        any_spread = False
        label_jobs: list[tuple[Any, list[tuple[Any, float, str]]]] = []
        for index, (metric, role) in enumerate(roles):
            ax = axes[index // ncols][index % ncols]
            spec = METRIC_SPECS[metric]
            stat = statistics.get(metric)
            lo, hi = spec.display_range
            if start and end:
                ax.axvspan(day(start), day(end) + timedelta(days=1), color="#EDF2EE", zorder=0)
            points = [(day(row["date"]), series_value(row, metric, stat), row) for row in series]
            points = [point for point in points if point[1] is not None]
            lows: list[float | None] = []
            highs: list[float | None] = []
            for _when, value, row in points:
                entry = row["metrics"][metric]
                if stat == "median" and entry.get("p10") is not None and entry.get("p90") is not None:
                    lows.append(entry["p10"])
                    highs.append(entry["p90"])
            y0, y1, _expanded = trend_y_limits(
                (lo, hi),
                [value for _when, value, _row in points],
                lows,
                highs,
            )
            if points:
                ax.plot([p[0] for p in points], [p[1] for p in points], color="#1F4D38", lw=1.2, zorder=2, clip_on=True)
            labels: list[tuple[Any, float, str]] = []
            for when, value, row in points:
                entry = row["metrics"][metric]
                has_spread = stat == "median" and entry.get("p10") is not None and entry.get("p90") is not None
                if has_spread:
                    any_spread = True
                    ax.plot(
                        [when, when],
                        [entry["p10"], entry["p90"]],
                        color="#1F4D38",
                        lw=1.0,
                        zorder=2,
                        clip_on=True,
                    )
                ax.plot(
                    [when],
                    [value],
                    marker="o",
                    markersize=5.2,
                    markerfacecolor="#1F4D38" if row["in_period"] else "white",
                    markeredgecolor="#1F4D38",
                    markeredgewidth=1.1,
                    zorder=3,
                    clip_on=True,
                )
                anchor = max(entry["p90"], value) if has_spread else value
                labels.append((when, anchor, text.fmt_index(value)))
            if not points:
                ax.text(
                    0.5,
                    0.5,
                    text.MISSING,
                    transform=ax.transAxes,
                    ha="center",
                    va="center",
                    fontsize=9,
                    color="#6B6B6B",
                )
            # 灰叉固定在纵轴底部的留白里，不代表该日的指数。
            for item in rejected:
                ax.plot(
                    [day(item)],
                    [y0 + 0.04 * (y1 - y0)],
                    marker="x",
                    color="#8A8A8A",
                    markersize=5,
                    markeredgewidth=1.1,
                    zorder=3,
                    clip_on=True,
                )
            ax.set_ylim(y0, y1)
            ax.set_xlim(x_lo, x_hi)
            ticks = sorted({p[0] for p in points} | {day(d) for d in rejected})
            # 日期不多时全部标出，相邻一天也不抽掉；再多才按间隔抽稀，避免标签叠在一起。
            close = any((b - a).days <= 1 for a, b in zip(ticks, ticks[1:]))
            if len(ticks) <= 8:
                shown = ticks
            else:
                min_gap = (x_hi - x_lo).days / 9.0
                shown = []
                for tick in ticks:
                    if not shown or (tick - shown[-1]).days >= max(min_gap, 1):
                        shown.append(tick)
            ax.set_xticks(shown, [f"{t.month}/{t.day}" for t in shown])
            ax.tick_params(labelsize=6.5 if close else 7.5, length=2.5)
            if close and len(shown) > 4:
                for label in ax.get_xticklabels():
                    label.set_rotation(32)
                    label.set_ha("right")
            ax.set_title(f"{spec.abbr}（{role}）", fontsize=9, fontproperties=bold, loc="left", pad=8)
            ax.grid(axis="y", color="#E3E7E4", lw=0.5)
            ax.set_axisbelow(True)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                ax.spines[side].set_color("#9AA39D")
            label_jobs.append((ax, labels))
        for extra in range(len(roles), nrows * ncols):
            axes[extra // ncols][extra % ncols].axis("off")
        handles = [
            Line2D([0], [0], marker="o", color="#1F4D38", markerfacecolor="#1F4D38", lw=1.2, markersize=5,
                   label="本期观测"),
            Line2D([0], [0], marker="o", color="#1F4D38", markerfacecolor="white", lw=0, markersize=5,
                   label="前期观测"),
        ]
        if any_spread:
            handles.append(Line2D([0], [0], color="#1F4D38", lw=1.0, marker="|", markersize=7,
                                  label="竖线：第10至第90百分位"))
        if rejected:
            handles.append(Line2D([0], [0], marker="x", color="#8A8A8A", lw=0, markersize=5, label="未采用的过境"))
        handles.append(Patch(facecolor="#EDF2EE", edgecolor="none", label="本监测周期"))
        fig.legend(handles=handles, loc="lower center", ncol=min(len(handles), 3), frameon=False, fontsize=7.2,
                   handlelength=1.6, columnspacing=1.1, bbox_to_anchor=(0.5, 0.0))
        fig.subplots_adjust(
            left=0.08,
            right=0.985,
            top=1 - 0.42 / total_h,
            bottom=0.92 / total_h,
            wspace=0.26,
            hspace=0.58,
        )
        # 先定好坐标范围再放标签，标签避让才能知道哪些位置会越出图框。
        fig.canvas.draw()
        for ax, labels in label_jobs:
            _place_point_labels(ax, labels)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(dest, dpi=160, facecolor="white")
        plt.close(fig)
        return dest if dest.is_file() else None
    except Exception:  # noqa: BLE001 — 图是可选的，正文仍保留已经算好的数值
        return None


def _weather_chart(facts: dict, dest: Path) -> Path | None:
    """Daily rain, ET0 and temperature. Days without a record are marked, not drawn as 0."""
    rows_all = [row for row in (facts.get("weather_daily") or []) if isinstance(row, dict) and row.get("date")]
    if not rows_all:
        return None
    period = text.period_of(facts)
    start, end = text.as_day(period.get("start")), text.period_end_inclusive(period)
    period_rows = [r for r in rows_all if start and start <= text.as_day(r["date"]) <= (end or "9999-99-99")]
    before_rows = [r for r in rows_all if start and text.as_day(r["date"]) < start]
    if not period_rows and not before_rows:
        period_rows = rows_all
    try:
        plt, bold = _matplotlib_cn()
        from matplotlib.lines import Line2D
        from matplotlib.patches import Patch

        legend: dict[str, Any] = {}

        half_day = timedelta(hours=12)

        def noon(value: Any) -> datetime:
            # 用带时刻的日期作横轴，缺测日和监测周期的底纹才有一天的宽度。
            return datetime.combine(date.fromisoformat(text.as_day(value)), time(12))

        def series_of(rows, key):
            return [float("nan") if r.get(key) is None else float(r[key]) for r in rows]

        def panel(ax, rows, title, *, shade: bool):
            days = [noon(r["date"]) for r in rows]
            rain = [r.get("precipitation_mm") for r in rows]
            have_rain = [(d, v) for d, v in zip(days, rain) if v is not None]
            if have_rain:
                ax.bar([d for d, _ in have_rain], [v for _, v in have_rain], width=0.7, color="#7FA9C9", zorder=2)
                legend["日降水量"] = Patch(facecolor="#7FA9C9", label="日降水量")
            if any(r.get("et0_mm") is not None for r in rows):
                # 缺测日为 NaN，折线在该日断开，不连过去。
                ax.plot(days, series_of(rows, "et0_mm"), color="#4C4C4C", lw=1.0, ls="-.", zorder=3)
                legend["日参考作物蒸散量ET0"] = Line2D([0], [0], color="#4C4C4C", lw=1.0, ls="-.",
                                                    label="日参考作物蒸散量ET0")
            ax.set_ylabel("降水量、ET0（毫米）", fontsize=8)
            ax.set_ylim(bottom=0)
            top = ax.get_ylim()[1]
            ax.set_ylim(0, max(top * 1.25, 5))
            missing = [
                d for d, r in zip(days, rows) if r.get("precipitation_mm") is None or r.get("et0_mm") is None
            ]
            if missing:
                # 缺测日用灰色底纹标出，柱子和折线留空，不画成 0。
                for d in missing:
                    ax.axvspan(d - half_day, d + half_day, facecolor="#D0D0D0", edgecolor="none", zorder=1)
                legend["降水或ET0缺测日"] = Patch(facecolor="#D0D0D0", label="降水或ET0缺测日")
            ax2 = ax.twinx()
            for key, color, label in (("max_c", "#B5482F", "日最高气温"), ("min_c", "#2F5E9E", "日最低气温")):
                if any(r.get(key) is not None for r in rows):
                    ax2.plot(days, series_of(rows, key), color=color, lw=1.3, marker="o", markersize=2.2, zorder=4)
                    legend[label] = Line2D([0], [0], color=color, lw=1.3, marker="o", markersize=2.2, label=label)
            ax2.set_ylabel("气温（℃）", fontsize=8)
            ax2.tick_params(labelsize=7.5, length=2.5)
            if shade and start and end:
                ax.axvspan(noon(start) - half_day, noon(end) + half_day, color="#EDF2EE", zorder=0)
                legend["本监测周期"] = Patch(facecolor="#EDF2EE", label="本监测周期")
            ax.set_xlim(days[0] - timedelta(days=1), days[-1] + timedelta(days=1))
            step = max(1, len(days) // 9)
            ticks = days[::step]
            ax.set_xticks(ticks, [f"{t.month}/{t.day}" for t in ticks])
            ax.tick_params(labelsize=7.5, length=2.5)
            ax.set_title(title, loc="left", fontsize=9.5, pad=5, fontproperties=bold)
            for axis in (ax, ax2):
                axis.spines["top"].set_visible(False)

        if before_rows and period_rows:
            fig, axes = plt.subplots(2, 1, figsize=(7.2, 5.3), dpi=200)
            panel(axes[0], period_rows, f"本期逐日气象（{compact_date_span(period)}）", shade=False)
            panel(axes[1], before_rows + period_rows,
                  f"本期开始前{len(before_rows)}天至本期末", shade=True)
            total_h = 5.3
        else:
            fig, ax = plt.subplots(figsize=(7.2, 2.9), dpi=200)
            panel(ax, period_rows or rows_all, "本期逐日气象", shade=False)
            total_h = 2.9
        fig.legend(handles=list(legend.values()), loc="lower center", ncol=min(6, len(legend)), frameon=False,
                   fontsize=7.5, columnspacing=1.2, handlelength=1.6, bbox_to_anchor=(0.5, 0.0))
        fig.subplots_adjust(left=0.085, right=0.915, top=1 - 0.3 / total_h, bottom=0.62 / total_h, hspace=0.42)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(dest, dpi=200, facecolor="white")
        plt.close(fig)
        return dest if dest.is_file() else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- flowables


def _heading(story: list, styles: dict, number: str, title: str) -> None:
    story.append(Paragraph(f"{number}、{title}", styles["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=2, spaceAfter=8))


def _bullets(story: list, styles: dict, items: list[str], *, style: str = "bullet") -> None:
    for item in items:
        story.append(Paragraph(item, styles[style], bulletText="•"))


def _model_prose(story: list, styles: dict, items: list[str]) -> None:
    if items:
        _bullets(story, styles, items)


def _info_table(styles: dict, rows: list[tuple[str, str]]) -> Table:
    data = [[Paragraph(label, styles["label"]), Paragraph(value, styles["value"])] for label, value in rows]
    table = Table(data, colWidths=[78, CW - 78])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, -1), HEAD_BG),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                ("LINEABOVE", (0, 0), (-1, 0), 0.4, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _data_table(styles: dict, rows: list[list[str]], widths: list[float]) -> Table:
    data = [[Paragraph(cell, styles["cell_head"]) for cell in rows[0]]]
    data += [[Paragraph(cell, styles["cell"]) for cell in row] for row in rows[1:]]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
                ("GRID", (0, 0), (-1, -1), 0.4, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _shared_image_budget(n_images: int, extra_lines: int) -> float:
    """Height per map so the maps and the short notes under them stay on one page."""
    frame = H - TOP - BOTTOM
    reserve = 46 + 18 + extra_lines * 14 + 10
    return max(160.0, (frame - reserve) / max(n_images, 1))


def _image_path(raw: Any) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.is_file() else None


_SECTION_NUMBERS = "一二三四五六七八九"
# 田内图的说明用种植户的叫法。证据表和说明页仍用正式名称。
_GROWER_METRIC = {
    "ndvi": "长势",
    "evi": "叶子绿不绿",
    "ndre": "叶绿素",
    "ndmi": "叶子里的水",
}


def build_story(facts: dict[str, Any], fig_dir: Path) -> list:
    """Flowables after the cover, in reading order."""
    styles = _styles()
    story: list = []
    counter = iter(_SECTION_NUMBERS)

    # 一、本期结论：基本信息 → 结论 → 主要证据 → 田间核查建议
    _heading(story, styles, next(counter), "本期结论")
    story.append(_info_table(styles, text.basic_info_rows(facts)))
    story.append(Paragraph("结论", styles["h2"]))
    _model_prose(story, styles, section_lines(facts.get("ai_narrative"), "conclusion"))
    # 模型失败只加一行说明，不另起空白页，也不把思考过程写进来。
    note = reader_note(facts.get("ai_narrative"))
    if note:
        story.append(Paragraph(note, styles["small"]))
    # 只跟一两句农事和土壤底子。气温由模型写进结论，不再在下面重复。
    from app.reports.biweekly.conclusion_context import build_conclusion_context

    context = facts.get("conclusion_context") or build_conclusion_context(facts)
    for line in context.get("lines") or []:
        story.append(Paragraph(str(line), styles["small"]))
    evidence = text.evidence_table(facts)
    if evidence:
        block = [
            Paragraph("主要证据", styles["h2"]),
            _data_table(styles, evidence, [90, 64, 64, 56, CW - 274]),
            Spacer(1, 3),
            Paragraph(text.evidence_caption(facts), styles["small"]),
        ]
        story.append(KeepTogether(block))
    story.append(Paragraph("田间核查建议", styles["h2"]))
    _model_prose(story, styles, section_lines(facts.get("ai_narrative"), "field_notes"))

    # 田里哪一块不一样、和周围的农田比各自成章，排在指标变化前面。
    patterns = facts.get("field_patterns") or {}
    infield = [
        item
        for item in (facts.get("infield") or [])
        if _image_path(item.get("image")) and (item.get("stats") or {}).get("status") == "ok"
    ]
    if infield:
        story.append(PageBreak())
        _heading(story, styles, next(counter), "田里哪一块不一样")
        names = "、".join(
            _GROWER_METRIC.get(item["metric"], METRIC_SPECS[item["metric"]].name)
            for item in infield
        )
        story.append(Paragraph(
            f"这几张图看{names}。从左到右是上一次、这一次、变了多少，以及哪一块比整块地偏弱。"
            "黑框圈出的是连成片、比整块地明显偏弱的地方。",
            styles["small"],
        ))
        persistent = list(patterns.get("persistent_lines") or [])
        model_lines: list[str] = []
        for item in infield:
            model_lines.extend(infield_line(facts.get("ai_narrative"), item["metric"]))
        budget = _shared_image_budget(len(infield), len(persistent) + len(model_lines) + 1)
        for item in infield:
            story.append(
                FitImage(
                    item["image"],
                    max_height=budget,
                    min_scale=0.86 if len(infield) > 1 else 0.72,
                )
            )
        for line in persistent:
            story.append(Paragraph(line, styles["small"]))
        _model_prose(story, styles, model_lines)

    surround_image = _image_path(facts.get("surrounding_comparison_image"))
    if text.has_surrounding(facts) and surround_image:
        story.append(PageBreak())
        _heading(story, styles, next(counter), "和周围的农田比")
        story.append(Paragraph(
            "下面把这块地和周围大约一公里的农田放在一起，看谁更绿。"
            "周围的地是卫星图认出来的，种的不一定是同一种作物，只说明相对高低。",
            styles["small"],
        ))
        _model_prose(story, styles, section_lines(facts.get("ai_narrative"), "surrounding"))
        story.append(FitImage(surround_image, max_height=380))
        contrast = _image_path(facts.get("surrounding_contrast_image"))
        if contrast:
            story.append(FitImage(contrast, max_height=170))

    # 两节图都有时，指标变化是第四节。缺哪一节就空出，不留空白页。
    story.append(PageBreak())
    _heading(story, styles, next(counter), "指标变化")
    judgment = judgment_paragraphs(facts.get("ai_narrative"))
    if judgment:
        story.append(Paragraph("变化好坏", styles["h2"]))
        _model_prose(story, styles, judgment)
    metric_notes = section_lines(facts.get("ai_narrative"), "metric_notes")
    if metric_notes:
        story.append(Paragraph("指标说明", styles["h2"]))
        _bullets(story, styles, metric_notes)
    trend = _trend_chart(facts, fig_dir / "metric_trend.png")
    trend_notes = [text.observation_footnote(facts), text.trend_axis_note(facts)]
    trend_notes.extend(patterns.get("uniformity_lines") or [])
    if patterns.get("history_line"):
        trend_notes.append(patterns["history_line"])
    if not text.has_weather(facts):
        trend_notes.extend((facts.get("water_risk") or {}).get("lines") or [])
    if trend:
        # 图单独缩放。说明文字跟在图后，放不下就续到下一页，不把图裁掉。
        story.append(FitImage(trend, max_height=430, min_scale=0.82))
    if not metric_notes:
        # 没有模型说明时，仍用正式名称介绍每个指标；有模型说明时不重复铺一段。
        _bullets(story, styles, text.metric_definition_items(facts), style="note")
    for note in trend_notes:
        if note:
            story.append(Paragraph(note, styles["small"]))

    # 气象条件（有数据才出）。
    if text.has_weather(facts):
        story.append(PageBreak())
        _heading(story, styles, next(counter), "气象条件")
        _model_prose(story, styles, section_lines(facts.get("ai_narrative"), "weather"))
        chart = _weather_chart(facts, fig_dir / "weather.png")
        if chart:
            story.append(FitImage(chart, max_height=340, min_scale=0.8))
        for line in (facts.get("water_risk") or {}).get("lines") or []:
            story.append(Paragraph(line, styles["small"]))

    # 说明
    story.append(PageBreak())
    _heading(story, styles, next(counter), "说明")
    story.append(Paragraph("数据与方法", styles["h2"]))
    _bullets(story, styles, text.method_items(facts), style="note")
    story.append(Spacer(1, 2))
    story.append(_data_table(styles, text.metric_spec_table(facts), [44, CW - 44 - 2 * 76, 76, 76]))
    story.append(Paragraph("生长阶段与指标", styles["h2"]))
    _bullets(story, styles, text.stage_note_items(facts), style="note")
    stage_table = text.stage_metric_table(facts)
    if stage_table:
        story.append(Spacer(1, 2))
        story.append(_data_table(styles, stage_table, [184, 100, 100, CW - 384]))
    story.append(Paragraph("本期未提供的内容", styles["h2"]))
    _bullets(story, styles, text.omitted_items(facts), style="note")
    story.append(Paragraph("使用范围", styles["h2"]))
    _bullets(story, styles, text.limit_items(facts), style="note")
    return story



def _draw_cover_footer_fallback(c) -> None:
    """Typographic-only cover: same centred footer as the artwork path."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    heading_font, heading_size = "YaHei-Bold", 9
    contact_font, contact_size = "YaHei", 8.5
    disc_font, disc_size = "YaHei", 8
    max_width = W - 2 * M
    cx = W / 2

    class _WidthFont:
        def __init__(self, name, size):
            self.name, self.size = name, size

        def getbbox(self, text: str):
            return (0, 0, stringWidth(text, self.name, self.size), self.size)

    disc_lines = wrap_footer_text(COVER_FOOTER_DISCLAIMER, _WidthFont(disc_font, disc_size), max_width)
    line_h = 11
    gap = 6
    height = line_h * (1 + len(COVER_FOOTER_CONTACTS) + len(disc_lines)) + gap
    y = 16 * mm + height
    c.setStrokeColor(DECOR)
    c.setLineWidth(0.6)
    c.line(cx - 45, y + 7, cx + 45, y + 7)
    c.setFillColor(DECOR)
    c.setFont(heading_font, heading_size)
    c.drawCentredString(cx, y - heading_size, COVER_FOOTER_ORG)
    y -= line_h
    c.setFillColor(HexColor("#2D3E34"))
    c.setFont(contact_font, contact_size)
    for line in COVER_FOOTER_CONTACTS:
        c.drawCentredString(cx, y - contact_size, line)
        y -= line_h
    y -= gap
    c.setFillColor(HexColor("#666666"))
    c.setFont(disc_font, disc_size)
    for line in disc_lines:
        c.drawCentredString(cx, y - disc_size, line)
        y -= line_h


def _draw_cover(c, doc) -> None:
    facts, cover_image = doc._cover_facts, doc._cover_image
    if cover_image and Path(cover_image).is_file():
        c.drawImage(str(cover_image), 0, 0, width=W, height=H, preserveAspectRatio=False, mask="auto")
        return
    # typographic fallback only if the bundled artwork cannot be composed
    c.saveState()
    c.setFillColor(DECOR)
    c.rect(0, H - 28 * mm, W, 28 * mm, fill=1, stroke=0)
    c.setFillColor(HexColor("#FFFFFF"))
    c.setFont("YaHei", 11)
    c.drawString(M, H - 12 * mm, "乡合农服")
    c.setFont("YaHei-Bold", 20)
    c.drawString(M, H - 23 * mm, "种植双周长势报告")
    c.setFillColor(INK)
    c.setFont("YaHei-Bold", 20)
    c.drawString(M, H - 60 * mm, text.land_title(facts))
    c.setFont("YaHei", 13)
    c.drawString(M, H - 74 * mm, compact_date_span(text.period_of(facts)))
    badge = crop_badge_text(facts)
    if badge:
        c.setFillColor(DECOR)
        c.roundRect(M, H - 108 * mm, 78 * mm, 10 * mm, 3, fill=1, stroke=0)
        c.setFillColor(HexColor("#FFFFFF"))
        c.setFont("YaHei-Bold", 11)
        c.drawString(M + 8, H - 105 * mm, f"作物  {badge}")
    if text.is_demo(facts):
        c.setFillColor(NOTICE)
        c.setFont("YaHei-Bold", 13)
        c.drawString(M, H - 90 * mm, text.DEMO_NOTICE)
    _draw_cover_footer_fallback(c)
    c.restoreState()


def render_official_biweekly_pdf(facts: dict[str, Any], dest: str | Path, *, figures_dir: Path | None = None) -> Path:
    """Write the official PDF from generate facts. Returns the output path."""
    if not facts:
        raise ReportError("GENERATE_FAILED")
    _register_fonts()
    facts = ensure_report_inputs(facts)
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fig_dir = Path(figures_dir or (dest_path.parent / f"{dest_path.stem}-figures"))
    fig_dir.mkdir(parents=True, exist_ok=True)
    title = text.land_title(facts)
    period = text.period_of(facts)
    cover_image = None
    try:
        cover_image = compose_official_cover(facts, fig_dir / "cover.png", land_title=title)
    except Exception:  # noqa: BLE001 — 封面底图缺失时用文字封面
        cover_image = None

    doc = BaseDocTemplate(
        str(dest_path),
        pagesize=A4,
        leftMargin=M,
        rightMargin=M,
        topMargin=TOP,
        bottomMargin=BOTTOM,
        title=f"{title} 种植双周长势报告（{compact_date_span(period)}）",
        author="乡合农服",
        subject="地块双周长势正式报告" + ("（示例数据，非实测）" if text.is_demo(facts) else ""),
    )
    doc._cover_facts, doc._cover_image = facts, cover_image
    frame = Frame(M, BOTTOM, CW, H - TOP - BOTTOM, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(
        [
            PageTemplate(id="cover", frames=[frame], onPage=_draw_cover),
            PageTemplate(id="body", frames=[frame]),
        ]
    )
    story: list = [NextPageTemplate("body"), Spacer(1, 1), PageBreak()]
    story += build_story(facts, fig_dir)
    doc.build(
        story,
        canvasmaker=_numbered_canvas(
            title=title,
            right=f"{title}  ·  {compact_date_span(period)}",
            demo=text.is_demo(facts),
        ),
    )
    if not dest_path.is_file() or dest_path.stat().st_size < 1000:
        raise ReportError("GENERATE_FAILED")
    return dest_path
