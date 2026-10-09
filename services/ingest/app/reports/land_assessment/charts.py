# -*- coding: utf-8 -*-
"""Matplotlib charts for land-assessment PDF."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.colors import Normalize
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle

from app.reports.land_assessment.paths import FONT_PATH
from app.reports.land_assessment.scoring import NDVI_GRADE_THRESHOLDS
from agric_satellite_analysis_common.phenology import infer_phenology

# Phenology stage keys -> Chinese short label / panel title / target MM-DD
STAGE_SPECS: list[tuple[str, str, str, tuple[int, int]]] = [
    ("seedling", "绿度起升", "冠层绿度起升观测", (0, 0)),
    ("vegetative", "生长上升", "冠层绿度上升观测", (0, 0)),
    ("peak", "绿度峰值", "冠层绿度峰值观测", (0, 0)),
    ("maturity", "绿度回落", "冠层绿度回落观测（原因待核实）", (0, 0)),
]

NDVI_VMIN = 0.0
NDVI_VMAX = 0.9

# 报告统一配色：与 PDF 版式主色一致，各指数颜色固定，便于跨图对照。
INK = "#263238"
MUTED = "#607D8B"
GRID = "#E3E8E5"
PRIMARY = "#1F4D38"
SEASON_FILL = "#E6F2EA"
COLOR_NDVI = "#2E7D32"
COLOR_EVI = "#B7791F"
COLOR_NDWI = "#1F6FB2"
COLOR_ET0 = "#A7C4D6"
COLOR_ALERT = "#C0392B"
GRADE_ORDER = ("优", "良", "中", "差")
GRADE_COLORS = {"优": "#1B5E20", "良": "#66A86B", "中": "#E0A526", "差": "#D35400"}
BARE_COLOR = "#B0BEC5"
# 国家标准日降水等级（GB/T 28592）：小雨 <10、中雨 10–25、大雨 25–50、暴雨 ≥50 mm。
RAIN_LEVELS = (
    (50.0, "#0B3C6E", "暴雨 ≥50"),
    (25.0, "#1F6FB2", "大雨 25–50"),
    (10.0, "#5FA3D9", "中雨 10–25"),
    (0.0, "#B9D9F0", "小雨 <10"),
)
EXPORT_DPI = 200
# 出图尺寸贴近 PDF 版心宽度（约 165 mm），嵌入后字号不再被二次缩小。
FULL_W = 6.5


def _setup_font() -> None:
    if FONT_PATH.exists():
        font_manager.fontManager.addfont(str(FONT_PATH))
        plt.rcParams["font.family"] = "WenQuanYi Zen Hei"
    plt.rcParams.update(
        {
            "axes.unicode_minus": False,
            "font.size": 8,
            "axes.titlesize": 8.5,
            "axes.titlelocation": "left",
            "axes.labelsize": 8,
            "axes.labelcolor": INK,
            "axes.edgecolor": "#90A4AE",
            "axes.linewidth": 0.6,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "xtick.major.size": 2.5,
            "ytick.major.size": 2.5,
            "legend.fontsize": 7,
            "legend.frameon": False,
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )


def _save(fig, out_path: Path | str) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=EXPORT_DPI, bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)
    return out_path


def _y_grid(ax) -> None:
    ax.grid(True, axis="y", color=GRID, lw=0.6)
    ax.set_axisbelow(True)


def _date_axis(ax, xs: list[datetime]) -> None:
    """按时间跨度选择月刻度间隔，统一使用 YYYY-MM，避免刻度拥挤。"""
    span_months = max((max(xs) - min(xs)).days, 1) / 30.4
    step = 1 if span_months <= 8 else 2 if span_months <= 16 else 3 if span_months <= 30 else 6
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=step))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.set_xlim(min(xs) - timedelta(days=7), max(xs) + timedelta(days=7))


def _legend_top(ax, handles=None, ncol: int = 4) -> None:
    kwargs = {"handles": handles} if handles else {}
    ax.legend(
        loc="lower left",
        bbox_to_anchor=(0.0, 1.0),
        ncol=ncol,
        handlelength=1.8,
        columnspacing=1.2,
        borderaxespad=0.2,
        **kwargs,
    )


def _parse_date(d: str) -> date:
    return datetime.fromisoformat(d[:10]).date()


def _ndvi_series(by_date: dict[str, dict[str, float]]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for d in sorted(by_date):
        v = by_date[d].get("NDVI")
        if v is None:
            continue
        out.append((d, float(v)))
    return out


def _observed_windows(by_date: dict[str, dict[str, float]]) -> list[dict]:
    series = _ndvi_series(by_date)
    if not series:
        return []
    return infer_phenology(
        [{"date": d, "ndvi": value, "official": True} for d, value in series],
        start=_parse_date(series[0][0]),
        end=_parse_date(series[-1][0]),
    )["windows"]


def estimate_emergence(
    by_date: dict[str, dict[str, float]], year: int, **_legacy
) -> dict[str, Any]:
    """保留旧接口名称，但仅报告冠层起升证据，不把该日期解释成精确出苗日。"""
    windows = [
        w
        for w in _observed_windows(by_date)
        if int(w["peak_date"][:4]) == year and w["start_date"]
    ]
    window = windows[0] if windows else None
    day = window["start_date"] if window else None
    return {
        "date": day,
        "ndvi": by_date.get(day, {}).get("NDVI") if day else None,
        "method": "observed_greenup" if day else "insufficient",
        "year": year,
        "note_zh": f"冠层绿度起升观测：{day}；实际出苗日需现场记录确认"
        if day
        else "冠层生长起点依据不足",
        "interval": window["start_interval"] if window else None,
    }


def pick_phenology_year(by_date: dict[str, dict[str, float]]) -> int | None:
    """选择有效观测较充分的生长周期峰值年，允许跨年冬作。"""
    windows = _observed_windows(by_date)
    if not windows:
        return None
    best = max(
        windows,
        key=lambda w: (
            w["status"] == "complete",
            w["observation_count"],
            w["peak_date"],
        ),
    )
    return int(best["peak_date"][:4])


def pick_phenology_stages(
    by_date: dict[str, dict[str, float]],
    year: int,
    *,
    pixel_dates: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """展示实测曲线的起升、上升、峰值、回落，不按月份命名农学阶段。"""
    windows = [w for w in _observed_windows(by_date) if int(w["peak_date"][:4]) == year]
    if not windows:
        return {}
    window = max(
        windows,
        key=lambda w: (
            w["status"] == "complete",
            w["observation_count"],
            w["peak_date"],
        ),
    )
    series = [
        (d, v)
        for d, v in _ndvi_series(by_date)
        if window["observed_start"] <= d <= window["observed_end"]
    ]
    if not series:
        return {}
    stages = {"_window": window, "_emergence": estimate_emergence(by_date, year)}

    def add(key, candidates, target):
        if not candidates:
            return
        available = [
            p for p in candidates if pixel_dates and p[0] in pixel_dates
        ] or candidates
        day, value = min(
            available, key=lambda p: abs((_parse_date(p[0]) - target).days)
        )
        spec = next(s for s in STAGE_SPECS if s[0] == key)
        stages[key] = {
            "key": key,
            "date": day,
            "ndvi": value,
            "label": spec[1],
            "title": spec[2],
        }

    first, peak, last = (
        _parse_date(series[0][0]),
        _parse_date(window["peak_date"]),
        _parse_date(series[-1][0]),
    )
    if window["start_date"]:
        add("seedling", [p for p in series if p[0] <= window["peak_date"]], first)
    add(
        "vegetative",
        [p for p in series if first < _parse_date(p[0]) < peak],
        first + (peak - first) / 2,
    )
    add("peak", [p for p in series if abs((_parse_date(p[0]) - peak).days) <= 15], peak)
    if window["end_date"]:
        add("maturity", [p for p in series if _parse_date(p[0]) > peak], last)
    return stages


def _marker_size(lons: np.ndarray, lats: np.ndarray) -> float:
    """Square marker size so points roughly tile the parcel."""
    if lons.size < 2:
        return 28.0
    # denser grids → smaller markers; bias slightly large to avoid white gaps
    n = max(lons.size, 1)
    base = 2600.0 / max(n**0.52, 1.0)
    return float(np.clip(base, 6.0, 52.0))


def _pixels_xy_ndvi(
    pixels: list[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lons, lats, vals = [], [], []
    for p in pixels:
        try:
            lon = float(p["lon"])
            lat = float(p["lat"])
            ndvi = p.get("NDVI")
            if ndvi is None:
                ndvi = p.get("ndvi")
            if ndvi is None:
                continue
            v = float(ndvi)
        except (KeyError, TypeError, ValueError):
            continue
        if not np.isfinite(v):
            continue
        lons.append(lon)
        lats.append(lat)
        vals.append(v)
    return np.asarray(lons), np.asarray(lats), np.asarray(vals)


def _grid_step(values: np.ndarray) -> float | None:
    uniq = np.unique(np.round(values, 7))
    if uniq.size < 2:
        return None
    diffs = np.diff(uniq)
    diffs = diffs[diffs > 1e-7]
    return float(np.median(diffs)) if diffs.size else None


def _pixels_to_raster(
    lons: np.ndarray, lats: np.ndarray, vals: np.ndarray
) -> tuple[np.ndarray, list[float]] | None:
    """像元点按经纬度步长落回规则网格，供栅格方式无缝渲染；步长异常时返回 None。"""
    dx, dy = _grid_step(lons), _grid_step(lats)
    if not dx or not dy:
        return None
    ix = np.rint((lons - lons.min()) / dx).astype(int)
    iy = np.rint((lats - lats.min()) / dy).astype(int)
    nx, ny = int(ix.max()) + 1, int(iy.max()) + 1
    if nx * ny > 4_000_000 or nx * ny > vals.size * 6:
        return None
    grid = np.full((ny, nx), np.nan)
    grid[iy, ix] = vals
    extent = [
        float(lons.min()) - dx / 2,
        float(lons.min()) + (nx - 0.5) * dx,
        float(lats.min()) - dy / 2,
        float(lats.min()) + (ny - 0.5) * dy,
    ]
    return grid, extent


def _layer_series(
    by_date: dict[str, dict[str, float]], layer: str
) -> tuple[list[datetime], list[float]]:
    xs, ys = [], []
    for d in sorted(by_date):
        row = by_date[d]
        v = row.get(layer)
        if v is None and layer == "NDWI":
            v = row.get("MNDWI")
        if v is None:
            continue
        xs.append(datetime.fromisoformat(d))
        ys.append(float(v))
    return xs, ys


def _shade_windows(ax, by_date: dict[str, dict[str, float]], label: bool) -> None:
    # 阴影采用同一批有效观测推断的真实日期窗，跨年作物也不套用夏季日历。
    for i, window in enumerate(_observed_windows(by_date)):
        ax.axvspan(
            datetime.fromisoformat(window["observed_start"]),
            datetime.fromisoformat(window["observed_end"]),
            color=SEASON_FILL,
            lw=0,
            zorder=0,
            label="观测生长季" if (label and i == 0) else None,
        )


def _threshold_line(ax, value: float, color: str, label: str) -> None:
    ax.axhline(value, ls=(0, (4, 2)), color=color, lw=0.8, zorder=1, label=label)


def _plot_index(ax, xs, ys, color: str, label: str, marker: str = "o") -> None:
    ax.plot(xs, ys, "-", color=color, lw=1.3, zorder=3, label=label)
    ax.scatter(xs, ys, s=7, color=color, marker=marker, zorder=4, linewidths=0)


def render_index_timeseries(
    by_date: dict[str, dict[str, float]],
    meta: dict[str, Any],
    out_path: Path,
) -> Path | None:
    """植被指数（NDVI/EVI）与水分指数（NDWI）双面板时序图，共用时间轴。"""
    nd_x, nd_y = _layer_series(by_date, "NDVI")
    if len(nd_x) < 2:
        return None
    ev_x, ev_y = _layer_series(by_date, "EVI")
    nw_x, nw_y = _layer_series(by_date, "NDWI")
    has_wet = len(nw_x) >= 2
    if has_wet:
        fig, (ax, ax_w) = plt.subplots(
            2,
            1,
            figsize=(FULL_W, 3.7),
            sharex=True,
            gridspec_kw={"height_ratios": [1.7, 1.0], "hspace": 0.12},
        )
    else:
        fig, ax = plt.subplots(figsize=(FULL_W, 2.4))
        ax_w = None

    _shade_windows(ax, by_date, label=True)
    _plot_index(ax, nd_x, nd_y, COLOR_NDVI, "NDVI")
    if len(ev_x) >= 2:
        _plot_index(ax, ev_x, ev_y, COLOR_EVI, "EVI", marker="s")
    thr = meta.get("ndvi_p30")
    if thr is not None:
        _threshold_line(ax, float(thr), COLOR_ALERT, f"NDVI 生育期 P30 = {float(thr):.2f}")
    ax.set_ylabel("植被指数")
    ax.set_ylim(min(0.0, min(nd_y + (ev_y or [0.0])) - 0.05), 1.0)
    _y_grid(ax)
    _legend_top(ax)

    if ax_w is not None:
        _shade_windows(ax_w, by_date, label=False)
        ax_w.axhline(0.0, color="#B0BEC5", lw=0.6, zorder=1)
        _plot_index(ax_w, nw_x, nw_y, COLOR_NDWI, "NDWI")
        wet = meta.get("ndwi_p85")
        if wet is not None:
            _threshold_line(ax_w, float(wet), COLOR_ALERT, f"偏湿阈值 P85 = {float(wet):.2f}")
        ax_w.set_ylabel("NDWI")
        _y_grid(ax_w)
        ax_w.legend(loc="upper right", ncol=2)
        _date_axis(ax_w, nd_x + nw_x)
    else:
        _date_axis(ax, nd_x)
    return _save(fig, out_path)


def _render_single_index(
    by_date: dict[str, dict[str, float]],
    layer: str,
    threshold: float | None,
    out_path: Path,
) -> Path | None:
    xs, ys = _layer_series(by_date, layer)
    if not xs:
        return None
    color = {"NDVI": COLOR_NDVI, "EVI": COLOR_EVI, "NDWI": COLOR_NDWI}.get(layer, PRIMARY)
    fig, ax = plt.subplots(figsize=(FULL_W, 2.2))
    _shade_windows(ax, by_date, label=True)
    _plot_index(ax, xs, ys, color, layer)
    if threshold is not None:
        _threshold_line(ax, float(threshold), COLOR_ALERT, f"生育期阈值 = {float(threshold):.2f}")
    ax.set_ylabel(layer)
    _y_grid(ax)
    _legend_top(ax)
    _date_axis(ax, xs)
    return _save(fig, out_path)


def render_phenology_curve(
    by_date: dict[str, dict[str, float]],
    year: int,
    stages: dict[str, dict[str, Any]],
    out_path: Path,
) -> Path | None:
    window = stages.get("_window") or {}
    first = window.get("observed_start", f"{year}-01-01")
    last = window.get("observed_end", f"{year}-12-31")
    series = [(d, v) for d, v in _ndvi_series(by_date) if first <= d <= last]
    if len(series) < 3:
        return None
    xs = [datetime.fromisoformat(d) for d, _ in series]
    ys = [v for _, v in series]
    fig, ax = plt.subplots(figsize=(FULL_W, 2.4))
    if window.get("observed_start") and window.get("observed_end"):
        ax.axvspan(
            datetime.fromisoformat(window["observed_start"]),
            datetime.fromisoformat(window["observed_end"]),
            color=SEASON_FILL,
            lw=0,
            zorder=0,
        )
    _plot_index(ax, xs, ys, COLOR_NDVI, "NDVI")
    stage_no = 0
    for key, _, _, _ in STAGE_SPECS:
        st = stages.get(key)
        if not st:
            continue
        dt = datetime.fromisoformat(st["date"])
        val = float(st["ndvi"])
        ax.axvline(dt, color="#90A4AE", lw=0.6, ls=(0, (2, 2)), zorder=1)
        ax.scatter(
            [dt], [val], s=34, facecolor="white", edgecolor=COLOR_ALERT, lw=1.2, zorder=5
        )
        # 相邻阶段标注上下错开，避免日期接近时文字重叠。
        dy = 9 if stage_no % 2 == 0 else -18
        ax.annotate(
            f"{st['label']} {val:.2f}",
            xy=(dt, val),
            xytext=(0, dy),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=7,
            color=INK,
            path_effects=[path_effects.withStroke(linewidth=2.2, foreground="white")],
            zorder=6,
        )
        stage_no += 1
    ax.set_ylim(0.0, 1.0)
    ax.set_ylabel("NDVI")
    _y_grid(ax)
    _date_axis(ax, xs)
    return _save(fig, out_path)


def _try_load_image(path: Path | str | None):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        from PIL import Image

        img = Image.open(p).convert("RGBA")
        return np.asarray(img)
    except Exception:
        return None


def _north_arrow(ax, x: float = 0.93, y: float = 0.80) -> None:
    halo = [path_effects.withStroke(linewidth=2.4, foreground="white")]
    ax.annotate(
        "",
        xy=(x, y + 0.12),
        xytext=(x, y),
        xycoords="axes fraction",
        arrowprops=dict(arrowstyle="-|>", color=INK, lw=1.0, mutation_scale=9),
        zorder=10,
    )
    ax.text(
        x,
        y + 0.13,
        "N",
        transform=ax.transAxes,
        ha="center",
        va="bottom",
        fontsize=7.5,
        color=INK,
        path_effects=halo,
        zorder=10,
    )


def _nice_scale_length(target_m: float) -> float:
    options = (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000)
    fit = [v for v in options if v <= target_m]
    return float(fit[-1] if fit else options[0])


def _scale_bar(ax, lons: np.ndarray, lats: np.ndarray) -> None:
    """按地块中心纬度把经度差换算为米，绘制黑白分段比例尺。"""
    lat0 = float(np.mean(lats))
    m_per_deg = 111_320.0 * max(np.cos(np.radians(lat0)), 1e-6)
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    length_m = _nice_scale_length((x1 - x0) * m_per_deg * 0.28)
    length_deg = length_m / m_per_deg
    bx = x0 + (x1 - x0) * 0.05
    by = y0 + (y1 - y0) * 0.05
    h = (y1 - y0) * 0.018
    half = length_deg / 2
    ax.add_patch(Rectangle((bx, by), half, h, facecolor=INK, edgecolor=INK, lw=0.5, zorder=10))
    ax.add_patch(
        Rectangle((bx + half, by), half, h, facecolor="white", edgecolor=INK, lw=0.5, zorder=10)
    )
    label = f"{length_m / 1000:g} km" if length_m >= 1000 else f"{length_m:g} m"
    ax.text(
        bx + length_deg / 2,
        by + h * 1.6,
        label,
        ha="center",
        va="bottom",
        fontsize=6.5,
        color=INK,
        path_effects=[path_effects.withStroke(linewidth=2.0, foreground="white")],
        zorder=10,
    )


def _map_stat_box(ax, text: str) -> None:
    ax.text(
        0.97,
        0.04,
        text,
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=6.8,
        color=INK,
        bbox=dict(boxstyle="round,pad=0.25", facecolor="white", edgecolor="#CFD8DC", lw=0.5, alpha=0.92),
        zorder=10,
    )


def _draw_stage_axis(
    ax,
    *,
    title: str,
    date_s: str,
    pixels: list[dict[str, Any]],
    rgb_path: Path | str | None = None,
    heatmap_path: Path | str | None = None,
    norm: Normalize | None = None,
):
    """Draw one stage cell: prefer RGB(+heatmap) like frontend 图三; else NDVI scatter."""
    norm = norm or Normalize(vmin=NDVI_VMIN, vmax=NDVI_VMAX)
    rgb = _try_load_image(rgb_path)
    hm = _try_load_image(heatmap_path)
    lons, lats, vals = _pixels_xy_ndvi(pixels)
    mean_v = float(np.mean(vals)) if vals.size else None
    stat = f"均值 {mean_v:.2f}" if mean_v is not None else ""
    if vals.size >= 5:
        stat += f" · CV {float(np.std(vals) / max(abs(mean_v), 1e-6)) * 100:.0f}%"
    ax.set_title(f"{title} · {date_s}", fontsize=8, pad=3, color=INK)

    if rgb is not None:
        ax.imshow(rgb, aspect="equal", interpolation="bilinear", zorder=1)
        if hm is not None:
            # translucent NDVI/color film over RGB (frontend-style)
            ax.imshow(
                hm, aspect="equal", interpolation="bilinear", alpha=0.55, zorder=2
            )
        elif lons.size:
            # project scatter roughly onto image extent if we lack geo-ref
            h, w = rgb.shape[0], rgb.shape[1]
            xs = (lons - float(lons.min())) / max(float(np.ptp(lons)), 1e-9) * (w - 1)
            ys = (1.0 - (lats - float(lats.min())) / max(float(np.ptp(lats)), 1e-9)) * (
                h - 1
            )
            ax.scatter(
                xs,
                ys,
                c=vals,
                s=max(6.0, min(28.0, 1800.0 / max(lons.size**0.5, 1.0))),
                marker="s",
                cmap="RdYlGn",
                norm=norm,
                linewidths=0,
                alpha=0.65,
                rasterized=True,
                zorder=3,
            )
        ax.set_axis_off()
        _north_arrow(ax)
        if stat:
            _map_stat_box(ax, stat)
        return None

    if lons.size == 0:
        ax.set_axis_off()
        ax.text(
            0.5, 0.5, "该日期无可用像元", transform=ax.transAxes,
            ha="center", va="center", fontsize=8, color=MUTED,
        )
        return None
    raster = _pixels_to_raster(lons, lats, vals)
    if raster is not None:
        grid, extent = raster
        sc = ax.imshow(
            grid,
            origin="lower",
            extent=extent,
            cmap="RdYlGn",
            norm=norm,
            interpolation="nearest",
        )
    else:
        sc = ax.scatter(
            lons,
            lats,
            c=vals,
            s=_marker_size(lons, lats),
            marker="s",
            cmap="RdYlGn",
            norm=norm,
            linewidths=0,
            rasterized=True,
        )
    pad_x = max(float(np.ptp(lons)) * 0.06, 1e-5)
    pad_y = max(float(np.ptp(lats)) * 0.10, 1e-5)
    ax.set_xlim(float(lons.min()) - pad_x, float(lons.max()) + pad_x)
    ax.set_ylim(float(lats.min()) - pad_y * 1.6, float(lats.max()) + pad_y)
    # 经纬度坐标按中心纬度校正纵横比，地块形状不被东西向拉伸。
    lat0 = float(np.mean(lats))
    ax.set_aspect(1.0 / max(np.cos(np.radians(lat0)), 1e-6), adjustable="box")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color("#CFD8DC")
        spine.set_linewidth(0.5)
    _north_arrow(ax)
    _scale_bar(ax, lons, lats)
    _map_stat_box(ax, stat)
    return sc


def render_stages_panel(
    stages: dict[str, dict[str, Any]],
    pixels_by_date: dict[str, list[dict[str, Any]]],
    out_path: Path,
    *,
    rgb_paths: dict[str, Path] | None = None,
    heatmap_paths: dict[str, Path] | None = None,
) -> Path | None:
    ordered_keys = [k for k, *_ in STAGE_SPECS]
    rgb_paths = rgb_paths or {}
    heatmap_paths = heatmap_paths or {}
    usable = []
    for k in ordered_keys:
        if k not in stages:
            continue
        d = stages[k]["date"]
        has_pix = bool(pixels_by_date.get(d))
        has_rgb = d in rgb_paths and Path(rgb_paths[d]).exists()
        if has_pix or has_rgb:
            usable.append(k)
    if len(usable) < 2:
        return None

    # 只排有影像的阶段：2–3 个阶段单行，4 个阶段 2×2，不留空白格。
    n = len(usable)
    rows, cols = (2, 2) if n == 4 else (1, n)
    fig, axes = plt.subplots(rows, cols, figsize=(FULL_W, 5.6 if rows == 2 else FULL_W / cols * 1.05 + 0.6))
    norm = Normalize(vmin=NDVI_VMIN, vmax=NDVI_VMAX)
    mappable = None
    for idx, (ax, key) in enumerate(zip(np.atleast_1d(axes).ravel(), usable)):
        st = stages[key]
        d = st["date"]
        sc = _draw_stage_axis(
            ax,
            title=f"({'abcd'[idx]}) {st['label']}",
            date_s=d,
            pixels=pixels_by_date.get(d) or [],
            rgb_path=rgb_paths.get(d),
            heatmap_path=heatmap_paths.get(d),
            norm=norm,
        )
        if sc is not None:
            mappable = sc

    bottom = 0.12 if rows == 2 else 0.2
    fig.subplots_adjust(left=0.02, right=0.98, top=0.93, bottom=bottom, wspace=0.08, hspace=0.18)
    if mappable is not None:
        cax = fig.add_axes([0.25, bottom * 0.45, 0.5, 0.018 if rows == 2 else 0.035])
        cbar = fig.colorbar(mappable, cax=cax, orientation="horizontal")
        cbar.set_label("NDVI（低 → 高）", fontsize=7.5, color=INK)
        cbar.ax.tick_params(labelsize=6.5, length=2)
        cbar.outline.set_linewidth(0.4)
    return _save(fig, out_path)


def render_stage_maps(
    stages: dict[str, dict[str, Any]],
    pixels_by_date: dict[str, list[dict[str, Any]]],
    out_dir: Path,
) -> dict[str, Path]:
    """Legacy per-stage maps (compact). Prefer the 2×2 panel in PDF."""
    written: dict[str, Path] = {}
    norm = Normalize(vmin=NDVI_VMIN, vmax=NDVI_VMAX)
    for key, label, _, _ in STAGE_SPECS:
        st = stages.get(key)
        if not st:
            continue
        pixels = pixels_by_date.get(st["date"]) or []
        if not _pixels_xy_ndvi(pixels)[0].size:
            continue
        fig, ax = plt.subplots(figsize=(3.4, 3.2))
        sc = _draw_stage_axis(ax, title=label, date_s=st["date"], pixels=pixels, norm=norm)
        if sc is not None:
            cbar = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.03)
            cbar.set_label("NDVI", fontsize=7)
            cbar.ax.tick_params(labelsize=6.5)
        fname = f"ndvi_stage_{key}.png"
        written[fname] = _save(fig, Path(out_dir) / fname)
    return written


def _rain_color(value: float) -> str:
    for floor, color, _ in RAIN_LEVELS:
        if value >= floor:
            return color
    return RAIN_LEVELS[-1][1]


def render_precip_bars(
    days: list[dict[str, Any]],
    out_path: Path,
    *,
    title: str,
    scene_date: str | None = None,
) -> Path | None:
    """Small precip bar chart for a 15-day prior window."""
    if not days:
        return None
    _setup_font()
    xs = [d["date"][5:] for d in days]  # MM-DD
    ys = [float(d.get("precipitation_sum") or 0) for d in days]
    fig, ax = plt.subplots(figsize=(FULL_W * 0.62, 1.7))
    ax.bar(range(len(xs)), ys, color=[_rain_color(v) for v in ys], width=0.72)
    ax.set_xticks(range(len(xs)))
    ax.set_xticklabels(xs, rotation=55, ha="right", fontsize=6)
    ax.set_ylabel("日降水 (mm)", fontsize=7)
    ax.set_title(title, fontsize=7.5, color=INK)
    _y_grid(ax)
    used = {lvl[2] for lvl in RAIN_LEVELS if any(_rain_color(v) == lvl[1] for v in ys)}
    handles = [Patch(color=c, label=lab) for _, c, lab in RAIN_LEVELS if lab in used]
    if handles:
        ax.legend(handles=handles, loc="upper left", fontsize=6, ncol=len(handles))
    return _save(fig, out_path)


def render_flood_evidence_charts(
    flood_evidence: dict[str, Any],
    out_dir: Path,
) -> dict[str, Path]:
    """Write flood collage + per-scene precip bars for PDF."""
    _setup_font()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    scenes = flood_evidence.get("scenes") or []
    if not scenes:
        return written

    # Combined precip for wettest scene (primary)
    top = scenes[0]
    days = (top.get("precip_prior_15d") or {}).get("days") or []
    if days:
        p = render_precip_bars(
            days,
            out_dir / "flood_precip_top.png",
            title=f"最湿场景 {top['date']} 前 15 日逐日降水",
            scene_date=top["date"],
        )
        if p:
            written["flood_precip_top.png"] = p

    # Per-scene precip charts (cap 6)
    for sc in scenes:
        d = sc["date"]
        days = (sc.get("precip_prior_15d") or {}).get("days") or []
        if not days:
            continue
        cum = (sc.get("precip_prior_15d") or {}).get("cumulative_mm")
        p = render_precip_bars(
            days,
            out_dir / f"flood_precip_{d}.png",
            title=f"{d} 前 15 日降水（累计 {cum} mm）",
            scene_date=d,
        )
        if p:
            written[f"flood_precip_{d}.png"] = p

    # RGB collage (up to 6)
    thumbs: list[tuple[str, Any]] = []
    for sc in scenes:
        media = sc.get("media") or {}
        local = media.get("local_rgb_path")
        if local and Path(local).exists():
            arr = _try_load_image(local)
            if arr is not None:
                thumbs.append((sc["date"], arr))
    if thumbs:
        n = len(thumbs)
        cols = 3 if n >= 3 else n
        rows = int(np.ceil(n / cols))
        fig, axes = plt.subplots(
            rows, cols, figsize=(FULL_W, FULL_W / cols * 0.95 * rows), squeeze=False
        )
        for i in range(rows * cols):
            r, c = divmod(i, cols)
            ax = axes[r][c]
            if i >= n:
                ax.set_axis_off()
                continue
            d, arr = thumbs[i]
            ax.imshow(arr, aspect="equal", interpolation="bilinear")
            sc = next(s for s in scenes if s["date"] == d)
            wet = sc.get("wet_mean")
            try:
                wet_txt = f" · 水分指数 {float(wet):.2f}"
            except (TypeError, ValueError):
                wet_txt = ""
            ax.set_title(f"({'abcdef'[i]}) {d}{wet_txt}", fontsize=7.5, pad=2, color=INK)
            ax.set_axis_off()
            _north_arrow(ax)
        fig.subplots_adjust(left=0.01, right=0.99, top=0.93, bottom=0.01, wspace=0.05, hspace=0.18)
        written["flood_rgb_collage.png"] = _save(fig, out_dir / "flood_rgb_collage.png")

    return written


def render_ndvi_grade_pie(shares: dict[str, Any], out_path: Path) -> Path | None:
    """生育期绿度等级构成：100% 堆叠条（替代饼图，便于精确比较占比）。"""
    if not shares or not shares.get("n"):
        return None
    _setup_font()
    counts = [int((shares.get("counts") or {}).get(g, 0)) for g in GRADE_ORDER]
    total = sum(counts)
    if total <= 0:
        return None
    fig, ax = plt.subplots(figsize=(FULL_W, 0.95))
    left = 0.0
    for g, c in zip(GRADE_ORDER, counts):
        if c <= 0:
            continue
        width = c * 100.0 / total
        ax.barh(0, width, left=left, height=0.62, color=GRADE_COLORS[g], edgecolor="white", lw=1.2)
        if width >= 8:
            ax.text(
                left + width / 2,
                0,
                f"{g} {width:.0f}%",
                ha="center",
                va="center",
                fontsize=7.5,
                color="white" if g in ("优", "良", "差") else INK,
            )
        left += width
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.5, 0.5)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0", "25%", "50%", "75%", "100%"])
    ax.spines["left"].set_visible(False)
    t = NDVI_GRADE_THRESHOLDS
    ranges = {
        "优": f"≥{t['优']:.1f}",
        "良": f"{t['良']:.1f}–{t['优']:.1f}",
        "中": f"{t['中']:.1f}–{t['良']:.1f}",
        "差": f"<{t['中']:.1f}",
    }
    handles = [
        Patch(color=GRADE_COLORS[g], label=f"{g}（NDVI {ranges[g]}）{c} 景")
        for g, c in zip(GRADE_ORDER, counts)
    ]
    _legend_top(ax, handles=handles, ncol=4)
    return _save(fig, out_path)


def render_stage_trend_bar(
    phenology: list[dict[str, Any]], out_path: Path
) -> Path | None:
    """Bar chart of mean NDVI across phenology stages."""
    if not phenology:
        return None
    rows = [r for r in phenology if r.get("mean_ndvi") is not None]
    if not rows:
        return None
    _setup_font()
    labels = []
    for r in rows:
        name = r.get("label") or r.get("key") or ""
        day = str(r.get("date") or "")
        labels.append(f"{name}\n{day}" if day else name)
    means = [float(r["mean_ndvi"]) for r in rows]
    colors = [
        BARE_COLOR if r.get("likely_bare") else GRADE_COLORS.get(r.get("grade"), COLOR_NDVI)
        for r in rows
    ]
    fig, ax = plt.subplots(figsize=(FULL_W * 0.8, 2.3))
    xs = np.arange(len(rows))
    ax.bar(xs, means, color=colors, width=0.5, zorder=3)
    top = max(0.95, max(means) + 0.12)
    for g in ("优", "良", "中"):
        thr = NDVI_GRADE_THRESHOLDS[g]
        ax.axhline(thr, color="#B0BEC5", lw=0.6, ls=(0, (3, 2)), zorder=1)
        ax.text(len(rows) - 0.55, thr, f"{g} {thr:.1f}", va="bottom", ha="right", fontsize=6.3, color=MUTED)
    for i, r in enumerate(rows):
        note = "疑似裸地" if r.get("likely_bare") else (r.get("trend_vs_prev") or "")
        text = f"{means[i]:.2f}" + (f"\n{note}" if note else "")
        ax.text(i, means[i] + 0.015, text, ha="center", va="bottom", fontsize=7, color=INK, zorder=4)
    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, fontsize=7)
    ax.set_xlim(-0.6, len(rows) - 0.4)
    ax.set_ylim(0, top)
    ax.set_ylabel("阶段 NDVI 均值")
    _y_grid(ax)
    return _save(fig, out_path)


def render_weather_history_bars(
    weather_history: dict[str, Any], out_path: Path
) -> Path | None:
    """生育期逐月 降水 / 参考蒸散 柱 + 月均气温折线（双轴气候图）。"""
    months = (weather_history or {}).get("season_totals") or (
        weather_history or {}
    ).get("months")
    if not months:
        return None
    # prefer in-season months only for clarity
    rows = [m for m in months if m.get("in_season")] or list(months)
    if not rows:
        return None
    _setup_font()
    labels = [str(m.get("ym") or "") for m in rows]
    precip = [float(m.get("precip_mm") or 0) for m in rows]
    et0 = [m.get("et0_mm") for m in rows]
    temps = [m.get("avg_temp") for m in rows]
    has_et0 = any(v is not None for v in et0)
    xs = np.arange(len(rows))
    width = 0.38 if has_et0 else 0.6
    fig, ax = plt.subplots(figsize=(FULL_W, 2.5))
    ax.bar(
        xs - (width / 2 if has_et0 else 0), precip, width, color=COLOR_NDWI, label="降水量", zorder=3
    )
    if has_et0:
        ax.bar(
            xs + width / 2,
            [float(v or 0) for v in et0],
            width,
            color=COLOR_ET0,
            label="参考蒸散 ET0",
            zorder=3,
        )
    ax.set_ylabel("水量 (mm)")
    _y_grid(ax)
    handles, legend_labels = ax.get_legend_handles_labels()
    if any(v is not None for v in temps):
        ax2 = ax.twinx()
        ax2.spines["right"].set_visible(True)
        tx = [x for x, v in zip(xs, temps) if v is not None]
        ty = [float(v) for v in temps if v is not None]
        ax2.plot(tx, ty, "-o", color=COLOR_ALERT, lw=1.2, ms=3, zorder=5)
        ax2.set_ylabel("月均气温 (℃)")
        ax2.set_ylim(min(ty) - 5, max(ty) + 5)
        handles.append(Line2D([0], [0], color=COLOR_ALERT, marker="o", ms=3, lw=1.2))
        legend_labels.append("月均气温")
    ax.set_xticks(xs)
    rotate = len(labels) > 8
    ax.set_xticklabels(labels, rotation=45 if rotate else 0, ha="right" if rotate else "center")
    ax.legend(
        handles, legend_labels, loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=3, borderaxespad=0.2
    )
    return _save(fig, out_path)


def render_score_radar(
    dimensions: list[dict[str, Any]] | dict[str, Any],
    out_path: Path,
    *,
    title: str = "",
) -> Path | None:
    """Matplotlib hexagon/radar chart for the six assessment dimensions.

    ``dimensions`` may be the scorecard ``dimensions`` list (with key/score)
    or a mapping of key -> score. Order follows DIM keys used in the PDF.
    """
    order = (
        ("crop", "作物匹配"),
        ("soil", "土壤条件"),
        ("vigor", "遥感长势"),
        ("weather", "天气适宜"),
        ("wet_safety", "抗渍/洪涝"),
        ("drought_safety", "抗旱安全"),
    )
    by_key: dict[str, float] = {}
    if isinstance(dimensions, dict):
        for k, v in dimensions.items():
            try:
                by_key[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    else:
        for item in dimensions or []:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            try:
                by_key[str(item["key"])] = float(item["score"])
            except (TypeError, ValueError):
                continue
    scores = []
    labels = []
    for key, label in order:
        if key not in by_key:
            return None
        scores.append(by_key[key])
        labels.append(label)
    if len(scores) != 6:
        return None

    _setup_font()
    n = 6
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    closed = angles + angles[:1]
    scores_closed = scores + scores[:1]

    fig, ax = plt.subplots(figsize=(4.0, 3.9), subplot_kw=dict(polar=True))
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_ylim(0, 100)
    ax.grid(False)
    ax.spines["polar"].set_visible(False)
    ax.set_yticks([])
    # 六边形分区底色与评分灯阈值一致：≥70 适宜、55–69 基本适宜、<55 需关注。
    for radius, color in ((100, "#EDF6F0"), (70, "#FDF6E4"), (55, "#FCEDEB")):
        ax.fill(closed, [radius] * (n + 1), color=color, lw=0, zorder=0)
    for radius in (20, 40, 55, 70, 85, 100):
        ax.plot(closed, [radius] * (n + 1), color="#CFD8D3", lw=0.5, zorder=1)
    for a in angles:
        ax.plot([a, a], [0, 100], color="#CFD8D3", lw=0.5, zorder=1)
    ax.plot(closed, scores_closed, color=PRIMARY, lw=1.6, zorder=4)
    ax.fill(closed, scores_closed, color=PRIMARY, alpha=0.08, zorder=3)
    ax.scatter(angles, scores, s=14, color=PRIMARY, zorder=5)
    ax.set_xticks(angles)
    ax.set_xticklabels([f"{lab}\n{sc:.0f}" for lab, sc in zip(labels, scores)], fontsize=8, color=INK)
    ax.tick_params(axis="x", pad=6)
    if title:
        ax.set_title(title, fontsize=9, pad=14, color=INK)
    fig.legend(
        handles=[
            Patch(color="#E8F4EC", label="≥70 适宜"),
            Patch(color="#FDF3DA", label="55–69 基本适宜"),
            Patch(color="#FBE4E1", label="<55 需关注"),
        ],
        loc="lower center",
        ncol=3,
        fontsize=7,
        bbox_to_anchor=(0.5, -0.02),
    )
    return _save(fig, out_path)


def render_charts(
    by_date: dict[str, dict[str, float]],
    meta: dict[str, Any],
    out_dir: Path,
    *,
    pixels_by_date: dict[str, list[dict[str, Any]]] | None = None,
    stage_rgb_paths: dict[str, Path] | None = None,
    stage_heatmap_paths: dict[str, Path] | None = None,
    flood_evidence: dict[str, Any] | None = None,
    write_individual_stage_maps: bool = False,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Path]:
    """Write ndvi/evi/ndwi/monthly + phenology/stage + flood charts; return name->path map."""
    _setup_font()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    month_hits = meta.get("month_hits") or {}
    written: dict[str, Path] = {}
    pixels_by_date = pixels_by_date or {}

    combo = render_index_timeseries(by_date, meta, out_dir / "indices_timeseries.png")
    if combo:
        written["indices_timeseries.png"] = combo
    for layer, fname, thr in (
        ("NDVI", "ndvi.png", meta.get("ndvi_p30")),
        ("EVI", "evi.png", meta.get("evi_p30")),
        ("NDWI", "ndwi.png", meta.get("ndwi_p85")),
    ):
        path = _render_single_index(by_date, layer, thr, out_dir / fname)
        if path:
            written[fname] = path

    fig, ax = plt.subplots(figsize=(FULL_W * 0.75, 2.0))
    ms = list(range(1, 13))
    vals = [int(month_hits.get(str(m), 0) or month_hits.get(m, 0) or 0) for m in ms]
    season = set(meta.get("season_months") or [])
    ax.bar(ms, vals, color=[COLOR_NDVI if m in season else BARE_COLOR for m in ms], width=0.65, zorder=3)
    ax.set_xticks(ms)
    ax.set_xticklabels([f"{m}月" for m in ms])
    ax.set_ylabel("风险命中次数")
    _y_grid(ax)
    _legend_top(
        ax,
        handles=[Patch(color=COLOR_NDVI, label="生育期月份"), Patch(color=BARE_COLOR, label="非生育期")],
        ncol=2,
    )
    written["monthly.png"] = _save(fig, out_dir / "monthly.png")

    # ---- Phenology curve + stage maps ----
    year = pick_phenology_year(by_date)
    if year is not None:
        stages = pick_phenology_stages(
            by_date, year, pixel_dates=set(pixels_by_date.keys()) or None
        )
        curve_name = f"ndvi_phenology_{year}.png"
        curve_path = render_phenology_curve(by_date, year, stages, out_dir / curve_name)
        if curve_path:
            written[curve_name] = curve_path
            written["ndvi_phenology.png"] = curve_path  # stable alias for PDF

        # Spatial panel only when we have pixels for stage dates
        stage_pixels = {
            stages[k]["date"]: pixels_by_date[stages[k]["date"]]
            for k in stages
            if not k.startswith("_") and stages[k]["date"] in pixels_by_date
        }
        stage_ok = len(stage_pixels) >= 2 or (
            stage_rgb_paths
            and sum(
                1
                for key, st in stages.items()
                if not key.startswith("_") and st["date"] in (stage_rgb_paths or {})
            )
            >= 2
        )
        if stage_ok:
            panel = render_stages_panel(
                stages,
                pixels_by_date,
                out_dir / "ndvi_stages_panel.png",
                rgb_paths=stage_rgb_paths,
                heatmap_paths=stage_heatmap_paths,
            )
            if panel:
                written["ndvi_stages_panel.png"] = panel
            # Individual stage maps are optional — PDF prefers compact 2×2 panel only
            if write_individual_stage_maps:
                written.update(render_stage_maps(stages, pixels_by_date, out_dir))

    analysis = analysis or {}
    shares = analysis.get("ndvi_grade_shares")
    if shares:
        p = render_ndvi_grade_pie(shares, out_dir / "ndvi_grade_shares.png")
        if p:
            written["ndvi_grade_shares.png"] = p
    pheno = analysis.get("phenology_stage_summary") or []
    if pheno:
        p = render_stage_trend_bar(pheno, out_dir / "ndvi_stage_trend.png")
        if p:
            written["ndvi_stage_trend.png"] = p
    whist = analysis.get("weather_history") or {}
    if whist:
        p = render_weather_history_bars(whist, out_dir / "weather_history.png")
        if p:
            written["weather_history.png"] = p

    if flood_evidence and flood_evidence.get("scenes"):
        written.update(render_flood_evidence_charts(flood_evidence, out_dir))

    return written
