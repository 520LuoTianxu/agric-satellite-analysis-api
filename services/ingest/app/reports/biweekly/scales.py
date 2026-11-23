"""Fixed colour scales for biweekly report maps.

色标规则（跨期、跨田块一致，不随单幅图的最大最小值伸缩）：
- 绝对值图用 metric_specs 里的固定显示范围。NDVI 为 0 至 1.0，上限是理论最大值，
  不是本田或本期的最高像元；
- 植被类指数色带：低值为棕色、低值参考线附近为浅黄、高值才是绿色，
  因此整块田低于参考线时不会被涂成深绿；小于 0 的像元单独用蓝灰色；
- NDMI 等以 0 为物理分界的指数用以 0 为中心的双向色带；
- 变化图和田内相对差异图以 0 为中心、正负对称；
- 缺测、云或云影像元用灰色，不参与配色。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from agric_satellite_analysis_common.biweekly.metric_specs import METRIC_SPECS, MetricSpec

MISSING_COLOR = "#BDBDBD"
BELOW_ZERO_COLOR = "#8FA9C4"
FLAG_EDGE_COLOR = "#111111"

# 低值端（显示下界 → 低值参考线）：棕 → 浅黄；高值端（参考线 → 显示上界）：黄绿 → 深绿。
_LOW_SIDE = ("#8C510A", "#D8B365", "#F6E8A6")
_HIGH_SIDE = ("#D9E69A", "#A6D36B", "#5DB25C", "#238443", "#00441B")

# 对称色标半幅的候选取值，取不小于稳健幅度（P98）的最小一档。
_SYMMETRIC_STEPS = (0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0)


def absolute_scale(metric: str) -> dict[str, Any]:
    """Colormap, norm and ticks for a metric's absolute-value map."""
    from matplotlib.colors import LinearSegmentedColormap, Normalize, TwoSlopeNorm

    spec: MetricSpec = METRIC_SPECS[metric]
    vmin, vmax = spec.display_range
    if spec.scale_kind == "diverging":
        cmap = LinearSegmentedColormap.from_list(
            f"report_{metric}",
            ["#8C510A", "#D8B365", "#F6E8C3", "#F5F5F5", "#C7EAE5", "#5AB4AC", "#01665E"],
        )
        norm = TwoSlopeNorm(vmin=vmin, vcenter=0.0, vmax=vmax)
        extend = "both"
    else:
        span = vmax - vmin
        ref = spec.low_reference if spec.low_reference is not None else vmin + 0.3 * span
        ref_fraction = min(max((ref - vmin) / span, 0.05), 0.9)
        stops: list[tuple[float, str]] = []
        for i, color in enumerate(_LOW_SIDE):
            stops.append((ref_fraction * i / (len(_LOW_SIDE) - 1), color))
        for i, color in enumerate(_HIGH_SIDE, start=1):
            stops.append((ref_fraction + (1.0 - ref_fraction) * i / len(_HIGH_SIDE), color))
        cmap = LinearSegmentedColormap.from_list(f"report_{metric}", stops)
        norm = Normalize(vmin=vmin, vmax=vmax)
        cmap = cmap.with_extremes(under=BELOW_ZERO_COLOR)
        extend = "min" if spec.valid_range[1] <= vmax else "both"
    cmap = cmap.with_extremes(bad=MISSING_COLOR)
    step = 0.2 if (vmax - vmin) >= 0.8 else 0.1
    ticks = [round(vmin + step * i, 2) for i in range(int(round((vmax - vmin) / step)) + 1)]
    return {"cmap": cmap, "norm": norm, "ticks": ticks, "extend": extend, "spec": spec}


def symmetric_limit(values: Any, *, floor: float = 0.05) -> float:
    """Half-range for a zero-centred scale, from the robust P98 of |values|."""
    data = np.asarray(values, dtype=float)
    data = np.abs(data[np.isfinite(data)])
    robust = float(np.percentile(data, 98)) if data.size else 0.0
    target = max(robust, floor)
    for step in _SYMMETRIC_STEPS:
        if step >= target - 1e-9:
            return step
    return float(math.ceil(target))


def _symmetric_ticks(limit: float) -> list[float]:
    """Five ticks when the half-steps are round numbers, otherwise three."""
    half = limit / 2
    if abs(round(half, 2) - half) < 1e-9:
        return [-limit, -half, 0.0, half, limit]
    return [-limit, 0.0, limit]


def change_scale(values: Any) -> dict[str, Any]:
    """Zero-centred symmetric scale for a later-minus-earlier change map."""
    from matplotlib.colors import LinearSegmentedColormap, Normalize

    limit = symmetric_limit(values)
    cmap = LinearSegmentedColormap.from_list(
        "report_change",
        ["#B2182B", "#EF8A62", "#FDDBC7", "#F7F7F7", "#D1E5F0", "#67A9CF", "#2166AC"],
    ).with_extremes(bad=MISSING_COLOR)
    return {
        "cmap": cmap,
        "norm": Normalize(vmin=-limit, vmax=limit),
        "limit": limit,
        "ticks": _symmetric_ticks(limit),
        "extend": "both",
    }


def relative_scale(values: Any) -> dict[str, Any]:
    """Zero-centred symmetric scale for value-minus-field-median maps.

    用橙—紫色带，与绝对值图的棕—绿色带、变化图的红—蓝色带都不同，
    避免把「相对本田偏低」读成「绝对值低」或「较上次下降」。
    """
    from matplotlib.colors import LinearSegmentedColormap, Normalize

    limit = symmetric_limit(values)
    cmap = LinearSegmentedColormap.from_list(
        "report_relative",
        ["#B35806", "#F1A340", "#FEE0B6", "#F7F7F7", "#D8DAEB", "#998EC3", "#542788"],
    ).with_extremes(bad=MISSING_COLOR)
    return {
        "cmap": cmap,
        "norm": Normalize(vmin=-limit, vmax=limit),
        "limit": limit,
        "ticks": _symmetric_ticks(limit),
        "extend": "both",
    }


def color_for(metric: str, value: float | None) -> tuple[float, float, float, float]:
    """RGBA the absolute scale assigns to one value; missing maps to gray."""
    from matplotlib.colors import to_rgba

    if value is None or not np.isfinite(value):
        return to_rgba(MISSING_COLOR)
    scale = absolute_scale(metric)
    return tuple(float(c) for c in scale["cmap"](scale["norm"](float(value))))
