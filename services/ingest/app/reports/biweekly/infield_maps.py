"""In-field maps of one metric: absolute level, change, and relative deviation.

每个主要指标出一幅图，最多四个子图：
1. 对比基准日的绝对值（固定色标）；
2. 本期最近一次有效观测的绝对值（同一固定色标）；
3. 两次观测之间的变化（后减前，0 居中、正负对称）；
4. 本期最近一次观测相对本田中位数的差异（0 居中、正负对称），并圈出统计上显著偏低的像元。

绝对值图回答「数值高不高」，相对差异图回答「田内哪里和整体不一样」，两者分开画、分开说。
像元按分析网格画成方块，不插值；云、云影和缺测像元画成灰色，不参与统计。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from agric_satellite_analysis_common.biweekly.metric_specs import METRIC_SPECS
from app.reports.biweekly.metric_stats import (
    MIN_CLUSTER_PIXELS,
    MIN_FIELD_PIXELS,
    change_stats,
    cluster_sizes,
    field_stats,
    match_pixels,
    pixel_spacing_m,
    pixel_table,
    region_labels,
    region_shares,
    summarize_values,
    to_local_meters,
    tukey_low_mask,
)
from app.reports.biweekly.scales import (
    BELOW_ZERO_COLOR,
    FLAG_EDGE_COLOR,
    MISSING_COLOR,
    absolute_scale,
    change_scale,
    relative_scale,
)

INK = "#1F2A24"
MUTED = "#5F6B64"


def _as_day(value: Any) -> str:
    return str(value or "")[:10]


def _short_day(value: Any) -> str:
    """面板标题用「5月14日」；年份见页眉的监测周期。"""
    day = _as_day(value)
    try:
        return f"{int(day[5:7])}月{int(day[8:10])}日"
    except ValueError:
        return day


def _setup_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    from app.reports.biweekly.fonts import resolve_yahei_fonts

    regular, bold = resolve_yahei_fonts()
    font_manager.fontManager.addfont(str(regular))
    font_manager.fontManager.addfont(str(bold))
    plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular)).get_name()
    plt.rcParams["axes.unicode_minus"] = False
    # 粗体直接指向字体文件，避免按字重查找失败后退回非中文字体。
    return plt, font_manager.FontProperties(fname=str(bold))


def _nice_bar_length(width_m: float) -> int:
    for length in (500, 200, 100, 50, 20, 10):
        if length <= 0.35 * width_m:
            return length
    return 10


def _draw_pixels(ax, xs, ys, values, *, half: float, cmap, norm, invalid=None):
    from matplotlib.collections import PolyCollection

    def squares(px, py):
        return np.stack(
            [
                np.column_stack([px - half, py - half]),
                np.column_stack([px + half, py - half]),
                np.column_stack([px + half, py + half]),
                np.column_stack([px - half, py + half]),
            ],
            axis=1,
        )

    finite = np.isfinite(values)
    if invalid is None:
        invalid = ~finite
    if invalid.any():
        ax.add_collection(
            PolyCollection(
                squares(xs[invalid], ys[invalid]), facecolors=MISSING_COLOR, edgecolors=MISSING_COLOR, linewidths=0.35
            )
        )
    # 描边与填充同色，消除相邻方块之间因反走样产生的白色细缝。
    mappable = PolyCollection(
        squares(xs[finite], ys[finite]), cmap=cmap, norm=norm, edgecolors="face", linewidths=0.35
    )
    mappable.set_array(values[finite])
    ax.add_collection(mappable)
    return mappable


def _frame(ax, xs, ys, *, half: float, title: str, bold, compact: bool = False):
    xmin, xmax = float(xs.min()) - half, float(xs.max()) + half
    ymin, ymax = float(ys.min()) - half, float(ys.max()) + half
    width, height = xmax - xmin, ymax - ymin
    pad_x, pad_top, pad_bottom = 0.06 * width, 0.06 * height, 0.20 * height
    ax.set_xlim(xmin - pad_x, xmax + pad_x)
    ax.set_ylim(ymin - pad_bottom, ymax + pad_top)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor("#C9CEC9")
        spine.set_linewidth(0.6)
    # 标题左对齐、限制在图框宽度内，较长的标题不会压到相邻面板或色标。
    ax.set_title(title, loc="left", fontsize=7.6 if compact else 8.6, color=INK, fontproperties=bold, pad=3 if compact else 5)
    # 比例尺和指北标记放在田块下方的留白里，不压在像元上。
    bar = _nice_bar_length(width)
    bar_y = ymin - 0.09 * height
    ax.plot([xmin, xmin + bar], [bar_y, bar_y], color=INK, lw=1.6, solid_capstyle="butt")
    ax.text(
        xmin + bar / 2,
        bar_y - 0.02 * height,
        f"{bar} 米",
        ha="center",
        va="top",
        fontsize=6.8 if compact else 7.5,
        color=INK,
    )
    ax.annotate(
        "北",
        xy=(xmax, ymin - 0.035 * height),
        xytext=(xmax, ymin - 0.165 * height),
        ha="center",
        va="center",
        fontsize=6.8 if compact else 7.5,
        color=INK,
        arrowprops={"arrowstyle": "-|>", "color": INK, "lw": 0.9},
    )


def _colorbar(fig, ax, mappable, *, ticks, extend, label, compact: bool = False):
    from matplotlib.ticker import FuncFormatter

    cbar = fig.colorbar(
        mappable,
        ax=ax,
        fraction=0.040 if compact else 0.046,
        pad=0.02 if compact else 0.03,
        extend=extend,
        ticks=ticks,
    )
    cbar.ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:g}"))
    size = 6.6 if compact else 7.5
    cbar.ax.tick_params(labelsize=size, length=2)
    cbar.set_label(label, fontsize=size, color=INK)
    cbar.outline.set_linewidth(0.5)
    return cbar


def analyse_metric(
    metric: str,
    latest: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Pixel-level numbers behind one metric's map. None when the latest date is unusable."""
    table = pixel_table(latest, metric)
    if table is None or int(table["valid"].sum()) < MIN_FIELD_PIXELS:
        return None
    spec = METRIC_SPECS[metric]
    xs, ys = to_local_meters(table["lon"], table["lat"])
    values = table["value"]
    stats = field_stats(latest, metric)
    median = float(stats["median"])
    relative = values - median
    flagged = tukey_low_mask(values)
    # 只有成片的显著偏低像元才用于描述位置；零星像元单独计数，不作为核查位置。
    spacing = pixel_spacing_m(xs, ys)
    clustered = flagged & (cluster_sizes(xs, ys, flagged, spacing) >= MIN_CLUSTER_PIXELS)
    labels = region_labels(xs, ys)
    n_valid = int(table["valid"].sum())
    flagged_n = int(flagged.sum())
    clustered_n = int(clustered.sum())
    flagged_below_ref = (
        int((clustered & (values < spec.low_reference)).sum()) if spec.low_reference is not None else None
    )

    out: dict[str, Any] = {
        "metric": metric,
        "date": _as_day(latest.get("date")),
        "stats": stats,
        "relative": {
            "rule": "tukey_lower_fence",
            "flagged_pixels": flagged_n,
            "flagged_fraction": flagged_n / n_valid,
            "min_cluster_pixels": MIN_CLUSTER_PIXELS,
            "clustered_pixels": clustered_n,
            "clustered_fraction": clustered_n / n_valid,
            "isolated_pixels": flagged_n - clustered_n,
            "clustered_below_low_reference": flagged_below_ref,
            "regions": [
                {"name": name, "share": share} for name, share in region_shares(labels, clustered)[:3]
            ],
            "deviation_p10": float(np.nanpercentile(relative, 10)),
            "deviation_p90": float(np.nanpercentile(relative, 90)),
        },
        "baseline_date": None,
        "baseline_stats": None,
        "change": None,
        "_arrays": {
            "xs": xs,
            "ys": ys,
            "values": values,
            "invalid": ~table["valid"],
            "relative": relative,
            "flagged": flagged,
            "clustered": clustered,
        },
    }
    if baseline is None:
        return out
    earlier = pixel_table(baseline, metric)
    if earlier is None or int(earlier["valid"].sum()) < MIN_FIELD_PIXELS:
        return out
    out["baseline_date"] = _as_day(baseline.get("date"))
    out["baseline_stats"] = field_stats(baseline, metric)
    exs, eys = to_local_meters(earlier["lon"], earlier["lat"])
    out["_arrays"]["baseline"] = {
        "xs": exs,
        "ys": eys,
        "values": earlier["value"],
        "invalid": ~earlier["valid"],
    }
    aligned = match_pixels(table, earlier)
    delta = values - aligned
    summary = change_stats(delta)
    if summary is not None:
        # 像元配对后的变化；只统计两次都有效的像元，任一次为云或缺测的不计入。
        out["change"] = summary
        out["_arrays"]["delta"] = delta
        # 显著偏低像元在基准日是否同样低于当时的田块显著偏低界限。
        earlier_flag = tukey_low_mask(aligned)
        out["relative"]["clustered_also_on_baseline"] = int((clustered & earlier_flag).sum())
    return out


def render_metric_figure(analysis: dict[str, Any], dest: str | Path, *, compact: bool = False) -> Path | None:
    """Draw the panels for one metric. Returns None if plotting is unavailable."""
    try:
        plt, bold = _setup_matplotlib()
    except Exception:  # noqa: BLE001 — 缺字体或绘图库时不出图，正文照常
        return None
    metric = analysis["metric"]
    spec = METRIC_SPECS[metric]
    arrays = analysis["_arrays"]
    xs, ys = arrays["xs"], arrays["ys"]
    half = pixel_spacing_m(xs, ys) / 2.0
    scale = absolute_scale(metric)
    lo, hi = spec.display_range
    # 合页时色标文字缩短，完整说法留在单指标大图上，避免标题和色标标签画出图外。
    if compact:
        abs_label = spec.abbr
        change_label = "变化"
        relative_label = "差值"
        fixed = f"{lo:g}–{hi:g}"
    else:
        abs_label = f"{spec.abbr}（{spec.unit}）"
        change_label = "变化量（0 居中）"
        relative_label = "比整块地高还是低"
        fixed = f"色标固定 {lo:g} 至 {hi:g}"

    panels: list[str] = []
    if "baseline" in arrays:
        panels.append("baseline")
    panels.append("latest")
    if "delta" in arrays:
        panels.append("change")
    panels.append("relative")
    ncols = 2
    nrows = (len(panels) + 1) // 2
    # 两个主要指标要放在同一页时用较矮的图。字号按这个尺寸来写，不再在 PDF 里把大图缩到看不清。
    if compact:
        # 两幅图加页眉和两三行说明要落在一页正文高度里，所以按这个高度排，而不是事后硬缩。
        fig_w, fig_h = 7.15, 1.62 * nrows + 0.58
    else:
        fig_w, fig_h = 7.4, 3.45 * nrows + 0.75
    fig, axes = plt.subplots(nrows, ncols, figsize=(fig_w, fig_h), dpi=160 if compact else 200)
    axes_list = np.atleast_1d(axes).ravel().tolist()
    order = "①②③④"
    for index, kind in enumerate(panels):
        ax = axes_list[index]
        mark = order[index]
        if kind == "baseline":
            base = arrays["baseline"]
            mappable = _draw_pixels(
                ax, base["xs"], base["ys"], base["values"], half=half,
                cmap=scale["cmap"], norm=scale["norm"], invalid=base["invalid"],
            )
            _frame(ax, base["xs"], base["ys"], half=half, bold=bold, compact=compact,
                   title=f"{mark} {_short_day(analysis['baseline_date'])} {spec.abbr}（{fixed}）")
            _colorbar(fig, ax, mappable, ticks=scale["ticks"], extend=scale["extend"],
                      label=abs_label, compact=compact)
        elif kind == "latest":
            mappable = _draw_pixels(
                ax, xs, ys, arrays["values"], half=half,
                cmap=scale["cmap"], norm=scale["norm"], invalid=arrays["invalid"],
            )
            _frame(ax, xs, ys, half=half, bold=bold, compact=compact,
                   title=f"{mark} {_short_day(analysis['date'])} {spec.abbr}（{fixed}）")
            _colorbar(fig, ax, mappable, ticks=scale["ticks"], extend=scale["extend"],
                      label=abs_label, compact=compact)
        elif kind == "change":
            delta = arrays["delta"]
            cscale = change_scale(delta)
            mappable = _draw_pixels(ax, xs, ys, delta, half=half, cmap=cscale["cmap"], norm=cscale["norm"])
            _frame(ax, xs, ys, half=half, bold=bold, compact=compact,
                   title=f"{mark} 变化量：{_short_day(analysis['date'])}减{_short_day(analysis['baseline_date'])}")
            _colorbar(fig, ax, mappable, ticks=cscale["ticks"], extend=cscale["extend"],
                      label=change_label, compact=compact)
        else:
            relative = arrays["relative"]
            rscale = relative_scale(relative)
            mappable = _draw_pixels(
                ax, xs, ys, relative, half=half,
                cmap=rscale["cmap"], norm=rscale["norm"], invalid=arrays["invalid"],
            )
            flagged = arrays["clustered"]
            if flagged.any():
                from matplotlib.collections import PolyCollection

                fx, fy = xs[flagged], ys[flagged]
                outline = np.stack(
                    [
                        np.column_stack([fx - half, fy - half]),
                        np.column_stack([fx + half, fy - half]),
                        np.column_stack([fx + half, fy + half]),
                        np.column_stack([fx - half, fy + half]),
                    ],
                    axis=1,
                )
                ax.add_collection(
                    PolyCollection(outline, facecolors="none", edgecolors=FLAG_EDGE_COLOR, linewidths=0.5)
                )
            _frame(ax, xs, ys, half=half, bold=bold, compact=compact,
                   title=f"{mark} {_short_day(analysis['date'])} 和整块地比")
            _colorbar(fig, ax, mappable, ticks=rscale["ticks"], extend=rscale["extend"],
                      label=relative_label, compact=compact)
    for ax in axes_list[len(panels):]:
        ax.axis("off")

    from matplotlib.patches import Patch

    handles = [Patch(facecolor=MISSING_COLOR, edgecolor="none", label="灰色：云、云影或缺测，不参与统计")]
    if spec.scale_kind == "sequential":
        handles.append(Patch(facecolor=BELOW_ZERO_COLOR, edgecolor="none", label="蓝灰色：小于 0（水体、积雪等非植被）"))
    handles.append(
Patch(
            facecolor="none",
            edgecolor=FLAG_EDGE_COLOR,
            linewidth=0.8,
            label="黑框：连成片、比整块地明显偏弱",
        )
    )
    fig.legend(
        handles=handles,
        loc="lower center",
        ncol=2,
        frameon=False,
        fontsize=6.6 if compact else 7.5,
        handlelength=1.4 if compact else 1.6,
        columnspacing=1.2 if compact else 1.8,
        bbox_to_anchor=(0.5, 0.0),
    )
    fig.subplots_adjust(
        left=0.02,
        right=0.86 if compact else 0.93,
        top=1 - (0.22 if compact else 0.28) / fig_h,
        bottom=(0.58 if compact else 0.72) / fig_h,
        wspace=0.28 if compact else 0.32,
        hspace=0.22 if compact else 0.16,
    )
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(dest_path, dpi=160 if compact else 200, facecolor="white")
    plt.close(fig)
    return dest_path if dest_path.is_file() else None


def public_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Drop numpy arrays so the analysis can be stored in facts / sidecar JSON."""
    return {key: value for key, value in analysis.items() if not key.startswith("_")}


def build_infield_metric(
    *,
    metric: str,
    latest: dict[str, Any],
    baseline: dict[str, Any] | None,
    dest: str | Path,
    compact: bool = False,
) -> dict[str, Any] | None:
    analysis = analyse_metric(metric, latest, baseline)
    if analysis is None:
        return None
    image = render_metric_figure(analysis, dest, compact=compact)
    out = public_analysis(analysis)
    out["image"] = str(image) if image else None
    out["panels"] = [
        name
        for name, present in (
            ("baseline", "baseline" in analysis["_arrays"]),
            ("latest", True),
            ("change", "delta" in analysis["_arrays"]),
            ("relative", True),
        )
        if present
    ]
    return out


__all__ = [
    "analyse_metric",
    "build_infield_metric",
    "public_analysis",
    "render_metric_figure",
    "summarize_values",
]
