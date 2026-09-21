# -*- coding: utf-8 -*-
"""Matplotlib charts for land-assessment PDF."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.colors import Normalize

from app.reports.land_assessment.paths import FONT_PATH
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


def _setup_font() -> None:
    if FONT_PATH.exists():
        font_manager.fontManager.addfont(str(FONT_PATH))
        plt.rcParams["font.family"] = "WenQuanYi Zen Hei"
    plt.rcParams["axes.unicode_minus"] = False


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
    np.unique(np.round(lons, 6))
    np.unique(np.round(lats, 6))
    # denser grids → smaller markers; bias slightly large to avoid white gaps
    n = max(lons.size, 1)
    base = 3200.0 / max(n**0.52, 1.0)
    return float(np.clip(base, 10.0, 64.0))


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
    fig, ax = plt.subplots(figsize=(10.5, 3.6), dpi=130)
    window = stages.get("_window") or {}
    if window.get("observed_start") and window.get("observed_end"):
        ax.axvspan(
            datetime.fromisoformat(window["observed_start"]),
            datetime.fromisoformat(window["observed_end"]),
            color="#d8f3dc",
            alpha=0.45,
            zorder=0,
        )
    ax.plot(xs, ys, "-o", ms=3.5, lw=1.4, color="#1b4332", zorder=2)
    # Annotate stages
    for key, _, _, _ in STAGE_SPECS:
        st = stages.get(key)
        if not st:
            continue
        dt = datetime.fromisoformat(st["date"])
        val = float(st["ndvi"])
        ax.scatter(
            [dt],
            [val],
            s=70,
            color="#e63946",
            zorder=4,
            edgecolors="white",
            linewidths=0.6,
        )
        ax.annotate(
            f"{st['label']} {val:.2f}",
            xy=(dt, val),
            xytext=(0, 12),
            textcoords="offset points",
            ha="center",
            va="bottom",
            fontsize=9,
            color="#c1121f",
            fontweight="bold",
        )
    ax.set_ylim(0.0, 1.0)
    ax.set_ylabel("NDVI")
    ax.set_title(f"{year} 年关联生长周期：观测冠层绿度变化（农学阶段待确认）")
    ax.grid(True, alpha=0.3)
    ax.set_xlim(xs[0] - timedelta(days=7), xs[-1] + timedelta(days=7))
    fig.tight_layout()
    out_path = Path(out_path)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


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
        mean_bit = f" 均≈{mean_v:.2f}" if mean_v is not None else ""
        ax.set_title(f"{title}\n{date_s}{mean_bit}", fontsize=8.5, pad=2)
        return None

    # Fallback: compact NDVI scatter on white
    if lons.size == 0:
        ax.set_axis_off()
        ax.set_title(f"{title}\n{date_s}（无可用像元）", fontsize=8.5, pad=2)
        return None
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
    pad_x = max(float(np.ptp(lons)) * 0.03, 1e-5)
    pad_y = max(float(np.ptp(lats)) * 0.03, 1e-5)
    ax.set_xlim(float(lons.min()) - pad_x, float(lons.max()) + pad_x)
    ax.set_ylim(float(lats.min()) - pad_y, float(lats.max()) + pad_y)
    ax.set_aspect("equal", adjustable="box")
    ax.set_axis_off()
    ax.set_title(f"{title}\n{date_s} 均≈{mean_v:.2f}", fontsize=8.5, pad=2)
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

    # Compact 2×2 — less whitespace than prior 9.2×8.0 + large padding
    fig, axes = plt.subplots(2, 2, figsize=(7.6, 6.4), dpi=140)
    fig.suptitle(
        "作物不同生育阶段对比（真彩底图+绿度叠色 / 或色斑）",
        fontsize=11,
        y=0.995,
    )
    norm = Normalize(vmin=NDVI_VMIN, vmax=NDVI_VMAX)
    mappable = None
    for ax, key in zip(axes.ravel(), ordered_keys):
        st = stages.get(key)
        if not st:
            ax.set_axis_off()
            continue
        d = st["date"]
        sc = _draw_stage_axis(
            ax,
            title=st["title"],
            date_s=d,
            pixels=pixels_by_date.get(d) or [],
            rgb_path=rgb_paths.get(d),
            heatmap_path=heatmap_paths.get(d),
            norm=norm,
        )
        if sc is not None:
            mappable = sc

    if mappable is not None:
        cbar = fig.colorbar(
            mappable, ax=axes.ravel().tolist(), fraction=0.035, pad=0.02
        )
        cbar.set_label("NDVI", fontsize=8)
        cbar.ax.tick_params(labelsize=7)
        right = 0.90
    else:
        right = 0.98
    fig.subplots_adjust(
        left=0.02, right=right, top=0.90, bottom=0.02, wspace=0.06, hspace=0.18
    )
    out_path = Path(out_path)
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.08)
    plt.close(fig)
    return out_path


def render_stage_maps(
    stages: dict[str, dict[str, Any]],
    pixels_by_date: dict[str, list[dict[str, Any]]],
    out_dir: Path,
) -> dict[str, Path]:
    """Legacy per-stage maps (compact). Prefer the 2×2 panel in PDF."""
    written: dict[str, Path] = {}
    for key, _, title, _ in STAGE_SPECS:
        st = stages.get(key)
        if not st:
            continue
        pixels = pixels_by_date.get(st["date"]) or []
        lons, lats, vals = _pixels_xy_ndvi(pixels)
        if lons.size == 0:
            continue
        mean_v = float(np.mean(vals))
        fig, ax = plt.subplots(figsize=(4.2, 3.6), dpi=110)
        sc = ax.scatter(
            lons,
            lats,
            c=vals,
            s=_marker_size(lons, lats),
            marker="s",
            cmap="RdYlGn",
            vmin=NDVI_VMIN,
            vmax=NDVI_VMAX,
            linewidths=0,
            rasterized=True,
        )
        pad_x = max(float(np.ptp(lons)) * 0.04, 1e-5)
        pad_y = max(float(np.ptp(lats)) * 0.04, 1e-5)
        ax.set_xlim(float(lons.min()) - pad_x, float(lons.max()) + pad_x)
        ax.set_ylim(float(lats.min()) - pad_y, float(lats.max()) + pad_y)
        ax.set_aspect("equal", adjustable="box")
        ax.set_axis_off()
        ax.set_title(
            f"{title}\n{st['date']} · 无地图底图，只看地块 NDVI",
            fontsize=11,
        )
        cbar = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label("NDVI 绿度 (0→1，越绿通常越旺)")
        ax.text(
            0.02,
            0.03,
            f"地块均绿度 ≈ {mean_v:.2f}",
            transform=ax.transAxes,
            fontsize=9,
            bbox=dict(
                boxstyle="round,pad=0.3",
                facecolor="white",
                edgecolor="#333",
                alpha=0.9,
            ),
        )
        fname = f"ndvi_stage_{key}.png"
        path = Path(out_dir) / fname
        fig.tight_layout()
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        written[fname] = path
    return written


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
    fig, ax = plt.subplots(figsize=(5.2, 2.0), dpi=120)
    colors = [
        "#1d4e89" if v >= 20 else ("#4ea8de" if v >= 5 else "#a9d6e5") for v in ys
    ]
    ax.bar(range(len(xs)), ys, color=colors, width=0.8)
    ax.set_xticks(range(len(xs)))
    ax.set_xticklabels(xs, rotation=55, ha="right", fontsize=6.5)
    ax.set_ylabel("mm", fontsize=8)
    ax.set_title(title, fontsize=9)
    ax.grid(True, axis="y", alpha=0.3)
    if scene_date:
        ax.axvline(-0.5, color="#bbb", lw=0)  # noop keep layout stable
    fig.subplots_adjust(left=0.12, right=0.98, top=0.82, bottom=0.32)
    out_path = Path(out_path)
    fig.savefig(out_path, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    return out_path


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
            title=f"最湿场景 {top['date']} 前15日日降水 (mm)",
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
            title=f"{d} 前15日降水（累计≈{cum} mm）",
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
        fig, axes = plt.subplots(rows, cols, figsize=(2.6 * cols, 2.4 * rows), dpi=130)
        if rows == 1 and cols == 1:
            axes = np.array([[axes]])
        elif rows == 1:
            axes = np.array([axes])
        elif cols == 1:
            axes = np.array([[ax] for ax in axes])
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
            ax.set_title(f"{d}\n湿≈{wet}", fontsize=8, pad=2)
            ax.set_axis_off()
        fig.suptitle(
            f"明水面卫星预览（共 {flood_evidence.get('absolute_open_water_scenes')} 景，展示 {n} 景）",
            fontsize=10,
            y=0.995,
        )
        fig.subplots_adjust(
            left=0.02, right=0.98, top=0.86, bottom=0.02, wspace=0.08, hspace=0.25
        )
        path = out_dir / "flood_rgb_collage.png"
        fig.savefig(path, bbox_inches="tight", pad_inches=0.06)
        plt.close(fig)
        written["flood_rgb_collage.png"] = path

    return written


def render_ndvi_grade_pie(shares: dict[str, Any], out_path: Path) -> Path | None:
    """Pie chart for 优/良/中/差 shares."""
    if not shares or not shares.get("n"):
        return None
    _setup_font()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    labels = ["优", "良", "中", "差"]
    colors = ["#2d6a4f", "#52b788", "#f4a261", "#e76f51"]
    counts = [int((shares.get("counts") or {}).get(g, 0)) for g in labels]
    nz = [(g, c, col) for g, c, col in zip(labels, counts, colors) if c > 0]
    if not nz:
        return None
    fig, ax = plt.subplots(figsize=(5.2, 3.4), dpi=120)
    ax.pie(
        [c for _, c, _ in nz],
        labels=[f"{g}" for g, _, _ in nz],
        colors=[col for _, _, col in nz],
        autopct=lambda p: f"{p:.0f}%" if p >= 3 else "",
        startangle=90,
        textprops={"fontsize": 9},
    )
    rule = shares.get("rule_zh") or "优≥0.7 / 良0.5–0.7 / 中0.3–0.5 / 差<0.3"
    ax.set_title(f"生育期绿度等级占比（n={shares.get('n')}）\n{rule}", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


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
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    labels = [r.get("label") or r.get("key") for r in rows]
    means = [float(r["mean_ndvi"]) for r in rows]
    colors = []
    for r in rows:
        if r.get("likely_bare"):
            colors.append("#adb5bd")
        else:
            g = r.get("grade")
            colors.append(
                {
                    "优": "#2d6a4f",
                    "良": "#52b788",
                    "中": "#f4a261",
                    "差": "#e76f51",
                }.get(g, "#40916c")
            )
    fig, ax = plt.subplots(figsize=(6.5, 3.2), dpi=120)
    xs = range(len(labels))
    ax.bar(xs, means, color=colors, width=0.62)
    ax.plot(xs, means, "-o", color="#1b4332", lw=1.2, ms=5)
    for i, r in enumerate(rows):
        note = r.get("trend_vs_prev") or ""
        bare = "裸地?" if r.get("likely_bare") else ""
        ax.text(
            i,
            means[i] + 0.02,
            f"{means[i]:.2f}{(' ·' + note) if note else ''}{(' ' + bare) if bare else ''}",
            ha="center",
            fontsize=8,
        )
    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylim(0, max(0.9, max(means) + 0.15))
    ax.set_ylabel("NDVI")
    ax.set_title("生育阶段绿度走势（苗期→成熟）")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def render_weather_history_bars(
    weather_history: dict[str, Any], out_path: Path
) -> Path | None:
    """Season-month precip bars from weather history."""
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
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    labels = [m.get("ym") for m in rows]
    precip = [float(m.get("precip_mm") or 0) for m in rows]
    fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=120)
    ax.bar(range(len(labels)), precip, color="#4ea8de", width=0.7)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("降水 mm")
    title = weather_history.get("period_label") or "生育期"
    ax.set_title(f"历史天气·{title}月降水")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def render_score_radar(
    dimensions: list[dict[str, Any]] | dict[str, Any],
    out_path: Path,
    *,
    title: str = "六维综合评分（程序）",
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
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n = 6
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    scores_closed = scores + scores[:1]
    angles_closed = angles + angles[:1]

    fig, ax = plt.subplots(figsize=(5.6, 5.2), dpi=130, subplot_kw=dict(polar=True))
    ax.set_theta_offset(np.pi / 2)
    ax.set_theta_direction(-1)
    ax.set_thetagrids(np.degrees(angles), labels, fontsize=10)
    ax.set_ylim(0, 100)
    ax.set_yticks([40, 55, 70, 85, 100])
    ax.set_yticklabels(["40", "55", "70", "85", "100"], fontsize=7, color="#666")
    ax.plot(angles_closed, scores_closed, color="#1f4d38", lw=2.0)
    ax.fill(angles_closed, scores_closed, color="#52b788", alpha=0.28)
    for ang, sc, lab in zip(angles, scores, labels):
        ax.text(
            ang,
            min(100, sc + 8),
            f"{sc:.0f}",
            ha="center",
            va="center",
            fontsize=8,
            color="#143d2b",
            fontweight="bold",
        )
    # Reference rings for light thresholds
    ax.plot(angles_closed, [70] * (n + 1), color="#1b7a3d", ls="--", lw=0.7, alpha=0.55)
    ax.plot(angles_closed, [55] * (n + 1), color="#c48a00", ls="--", lw=0.7, alpha=0.45)
    ax.set_title(title, fontsize=11, pad=14, color="#143d2b")
    ax.grid(True, alpha=0.35)
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


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
    dates = sorted(by_date)
    ndvi_p30 = meta.get("ndvi_p30")
    evi_p30 = meta.get("evi_p30")
    ndwi_p85 = meta.get("ndwi_p85")
    month_hits = meta.get("month_hits") or {}
    written: dict[str, Path] = {}
    pixels_by_date = pixels_by_date or {}

    def series(layer: str):
        xs, ys = [], []
        for d in dates:
            if layer in by_date[d]:
                xs.append(datetime.fromisoformat(d))
                ys.append(by_date[d][layer])
            elif layer == "NDWI" and "MNDWI" in by_date[d]:
                xs.append(datetime.fromisoformat(d))
                ys.append(by_date[d]["MNDWI"])
        return xs, ys

    specs = [
        ("NDVI", "ndvi.png", ndvi_p30),
        ("EVI", "evi.png", evi_p30),
        ("NDWI", "ndwi.png", ndwi_p85),
    ]
    for layer, fname, thr in specs:
        xs, ys = series(layer)
        if not xs:
            continue
        fig, ax = plt.subplots(figsize=(9, 3.2), dpi=120)
        ax.plot(xs, ys, "-o", ms=3, lw=1.2, color="#2d6a4f")
        # 阴影采用同一批有效观测推断的真实日期窗，跨年作物也不套用夏季日历。
        for i, window in enumerate(_observed_windows(by_date)):
            ax.axvspan(
                datetime.fromisoformat(window["observed_start"]),
                datetime.fromisoformat(window["observed_end"]),
                color="#d8f3dc",
                alpha=0.35,
                label="观测生长窗口" if i == 0 else None,
            )
        if thr is not None:
            ax.axhline(
                thr, ls="--", color="#e76f51", lw=1, label=f"生育期阈值 {thr:.2f}"
            )
        ax.set_title(f"{layer}（阴影=观测生长窗口；阈值仅来自窗口内）")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8, loc="best")
        fig.tight_layout()
        path = out_dir / fname
        fig.savefig(path)
        plt.close(fig)
        written[fname] = path

    fig, ax = plt.subplots(figsize=(7, 3), dpi=120)
    ms = list(range(1, 13))
    vals = [int(month_hits.get(str(m), 0) or month_hits.get(m, 0) or 0) for m in ms]
    ax.bar(
        ms,
        vals,
        color=[
            "#95d5b2" if m in (meta.get("season_months") or []) else "#adb5bd"
            for m in ms
        ],
    )
    ax.set_xticks(ms)
    ax.set_title("生育期内风险命中月份分布（非全年）")
    ax.set_xlabel("月")
    fig.tight_layout()
    path = out_dir / "monthly.png"
    fig.savefig(path)
    plt.close(fig)
    written["monthly.png"] = path

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
