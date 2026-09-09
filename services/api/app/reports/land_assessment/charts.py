# -*- coding: utf-8 -*-
"""Matplotlib charts for land-assessment PDF."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.colors import Normalize

from app.reports.land_assessment.paths import FONT_PATH
from app.reports.land_assessment.scoring import PEAK_MONTHS, SEASON_MONTHS

# Phenology stage keys -> Chinese short label / panel title / target MM-DD
STAGE_SPECS: list[tuple[str, str, str, tuple[int, int]]] = [
    ("seedling", "苗期", "苗期/早期 (绿度偏低，正常)", (6, 15)),
    ("vegetative", "拔节—抽雄", "拔节—抽雄前后 (快速变绿)", (7, 7)),
    ("peak", "旺长", "旺长期 (绿度最高)", (8, 10)),
    ("maturity", "成熟回落", "成熟期附近 (绿度回落，正常)", (9, 25)),
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


def pick_phenology_year(by_date: dict[str, dict[str, float]]) -> int | None:
    """Pick year with best maize-season (Jun–Sep) NDVI coverage / peak."""
    series = _ndvi_series(by_date)
    if not series:
        return None
    by_year: dict[int, list[tuple[str, float]]] = {}
    for d, v in series:
        y = int(d[:4])
        m = int(d[5:7])
        if m in SEASON_MONTHS:
            by_year.setdefault(y, []).append((d, v))
    if not by_year:
        # fall back to year with most NDVI points
        counts: dict[int, int] = {}
        for d, _ in series:
            counts[int(d[:4])] = counts.get(int(d[:4]), 0) + 1
        return max(counts, key=counts.get) if counts else None

    def score(y: int) -> tuple[int, float, int]:
        pts = by_year[y]
        peak_vals = [v for d, v in pts if int(d[5:7]) in PEAK_MONTHS]
        peak = max(peak_vals) if peak_vals else (max(v for _, v in pts) if pts else 0.0)
        return (len(pts), peak, y)

    return max(by_year.keys(), key=score)


def _nearest_date(
    candidates: list[tuple[str, float]],
    target: date,
    *,
    window_days: int = 18,
    prefer_high: bool | None = None,
) -> tuple[str, float] | None:
    """Pick candidate nearest to target within window; optional prefer higher NDVI."""
    scored: list[tuple[int, float, str, float]] = []
    for d, v in candidates:
        dd = _parse_date(d)
        delta = abs((dd - target).days)
        if delta > window_days:
            continue
        # lower delta better; if prefer_high, higher NDVI breaks ties
        scored.append((delta, -(v if prefer_high else 0.0), d, v))
    if not scored:
        return None
    scored.sort()
    return scored[0][2], scored[0][3]


def pick_phenology_stages(
    by_date: dict[str, dict[str, float]],
    year: int,
    *,
    pixel_dates: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Choose real observation dates for 4 maize phenology stages.

    Heuristics:
    - seedling ~ mid-Jun (low but rising)
    - vegetative early-Jul (rising)
    - peak = max NDVI in Jul–Aug
    - maturity late-Sep after peak (prefer drop from peak)
    Prefers dates that have usable pixel_data when pixel_dates is provided.
    """
    series = [(d, v) for d, v in _ndvi_series(by_date) if int(d[:4]) == year]
    season = [(d, v) for d, v in series if int(d[5:7]) in SEASON_MONTHS]
    if not season:
        season = series
    if not season:
        return {}

    def pool(month_set: set[int] | None = None) -> list[tuple[str, float]]:
        pts = (
            season
            if month_set is None
            else [(d, v) for d, v in season if int(d[5:7]) in month_set]
        )
        if pixel_dates:
            with_pix = [(d, v) for d, v in pts if d in pixel_dates]
            if with_pix:
                return with_pix
        return pts

    stages: dict[str, dict[str, Any]] = {}

    # Peak: max in Jul–Aug (user heuristic)
    peak_pool = pool(PEAK_MONTHS) or pool()
    if peak_pool:
        max_v = max(v for _, v in peak_pool)
        # Among near-max values, prefer closer to Aug 10
        top = [(d, v) for d, v in peak_pool if v >= max_v - 0.03]
        target_peak = date(year, 8, 10)
        best = min(top, key=lambda x: abs((_parse_date(x[0]) - target_peak).days))
        stages["peak"] = {
            "key": "peak",
            "label": "旺长",
            "title": "旺长期 (绿度最高)",
            "date": best[0],
            "ndvi": best[1],
        }

    peak_date = (
        _parse_date(stages["peak"]["date"]) if "peak" in stages else date(year, 8, 10)
    )
    peak_ndvi = float(stages["peak"]["ndvi"]) if "peak" in stages else 0.0

    # Seedling: near mid-Jun, prefer lower NDVI than peak
    seed_pool = pool({6}) or [(d, v) for d, v in pool() if _parse_date(d) < peak_date]
    hit = _nearest_date(seed_pool, date(year, 6, 15), window_days=20, prefer_high=False)
    if hit is None and seed_pool:
        hit = min(
            seed_pool, key=lambda x: abs((_parse_date(x[0]) - date(year, 6, 15)).days)
        )
    if hit:
        stages["seedling"] = {
            "key": "seedling",
            "label": "苗期",
            "title": "苗期/早期 (绿度偏低，正常)",
            "date": hit[0],
            "ndvi": hit[1],
        }

    # Vegetative: early Jul, rising between seedling and peak
    veg_lo = (
        _parse_date(stages["seedling"]["date"])
        if "seedling" in stages
        else date(year, 6, 20)
    )
    veg_pool = pool({7}) or [
        (d, v) for d, v in pool() if veg_lo < _parse_date(d) < peak_date
    ]
    # Prefer mid-rise: not as low as seedling, not peak
    hit = _nearest_date(veg_pool, date(year, 7, 7), window_days=18, prefer_high=True)
    if hit is None and veg_pool:
        hit = min(
            veg_pool, key=lambda x: abs((_parse_date(x[0]) - date(year, 7, 7)).days)
        )
    if hit:
        stages["vegetative"] = {
            "key": "vegetative",
            "label": "拔节—抽雄",
            "title": "拔节—抽雄前后 (快速变绿)",
            "date": hit[0],
            "ndvi": hit[1],
        }

    # Maturity: late Sep after peak; prefer drop below peak
    mat_pool = [(d, v) for d, v in (pool({9}) or pool()) if _parse_date(d) > peak_date]
    if not mat_pool:
        mat_pool = [(d, v) for d, v in pool({9}) if _parse_date(d) >= date(year, 9, 10)]
    target_mat = date(year, 9, 25)
    if mat_pool:
        # Score: prefer lower NDVI than peak, then near target date
        def mat_key(item: tuple[str, float]) -> tuple:
            d, v = item
            dropped = 0 if v < peak_ndvi - 0.02 else 1
            return (dropped, abs((_parse_date(d) - target_mat).days), v)

        hit = min(mat_pool, key=mat_key)
        stages["maturity"] = {
            "key": "maturity",
            "label": "成熟回落",
            "title": "成熟期附近 (绿度回落，正常)",
            "date": hit[0],
            "ndvi": hit[1],
        }

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
    series = [(d, v) for d, v in _ndvi_series(by_date) if int(d[:4]) == year]
    if len(series) < 3:
        return None
    xs = [datetime.fromisoformat(d) for d, _ in series]
    ys = [v for _, v in series]
    fig, ax = plt.subplots(figsize=(10.5, 3.6), dpi=130)
    ax.axvspan(
        datetime(year, 6, 1),
        datetime(year, 9, 30),
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
    ax.set_title(f"{year} 年地块平均绿度：先升后降（成熟附近下降是正常的）")
    ax.grid(True, alpha=0.3)
    ax.set_xlim(datetime(year, 1, 1), datetime(year + 1, 1, 5))
    fig.tight_layout()
    out_path = Path(out_path)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def render_stages_panel(
    stages: dict[str, dict[str, Any]],
    pixels_by_date: dict[str, list[dict[str, Any]]],
    out_path: Path,
) -> Path | None:
    ordered_keys = [k for k, *_ in STAGE_SPECS]
    usable = [
        k
        for k in ordered_keys
        if k in stages
        and stages[k]["date"] in pixels_by_date
        and pixels_by_date[stages[k]["date"]]
    ]
    if len(usable) < 2:
        return None

    fig, axes = plt.subplots(2, 2, figsize=(9.2, 8.0), dpi=130)
    fig.suptitle(
        "夏玉米不同生育阶段 NDVI 长势对比（同一色标，便于看升降）",
        fontsize=12,
        y=0.98,
    )
    norm = Normalize(vmin=NDVI_VMIN, vmax=NDVI_VMAX)
    mappable = None
    for ax, key in zip(axes.ravel(), ordered_keys):
        st = stages.get(key)
        if not st:
            ax.set_axis_off()
            continue
        d = st["date"]
        pixels = pixels_by_date.get(d) or []
        lons, lats, vals = _pixels_xy_ndvi(pixels)
        if lons.size == 0:
            ax.set_axis_off()
            ax.set_title(f"{st['title']}\n{d}（无可用像元）", fontsize=9)
            continue
        mean_v = float(np.mean(vals))
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
        mappable = sc
        pad_x = max(float(np.ptp(lons)) * 0.04, 1e-5)
        pad_y = max(float(np.ptp(lats)) * 0.04, 1e-5)
        ax.set_xlim(float(lons.min()) - pad_x, float(lons.max()) + pad_x)
        ax.set_ylim(float(lats.min()) - pad_y, float(lats.max()) + pad_y)
        ax.set_aspect("equal", adjustable="box")
        ax.set_axis_off()
        ax.set_title(f"{st['title']}\n{d} 均≈{mean_v:.2f}", fontsize=9)

    if mappable is not None:
        cbar = fig.colorbar(
            mappable, ax=axes.ravel().tolist(), fraction=0.046, pad=0.04
        )
        cbar.set_label("NDVI")
    fig.subplots_adjust(
        left=0.04, right=0.88, top=0.90, bottom=0.04, wspace=0.12, hspace=0.28
    )
    out_path = Path(out_path)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def render_stage_maps(
    stages: dict[str, dict[str, Any]],
    pixels_by_date: dict[str, list[dict[str, Any]]],
    out_dir: Path,
) -> dict[str, Path]:
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
        fig, ax = plt.subplots(figsize=(6.2, 5.4), dpi=130)
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


def render_charts(
    by_date: dict[str, dict[str, float]],
    meta: dict[str, Any],
    out_dir: Path,
    *,
    pixels_by_date: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Path]:
    """Write ndvi/evi/ndwi/monthly + phenology/stage charts; return name->path map."""
    _setup_font()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dates = sorted(by_date)
    years = meta.get("years") or sorted({int(d[:4]) for d in dates})
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
        for y in years:
            ax.axvspan(
                datetime(y, 6, 1),
                datetime(y, 9, 30),
                color="#d8f3dc",
                alpha=0.35,
                label="玉米生育期" if y == years[0] else None,
            )
            ax.axvspan(
                datetime(y, 7, 1),
                datetime(y, 8, 31),
                color="#95d5b2",
                alpha=0.25,
                label="峰值期7–8月" if y == years[0] else None,
            )
        if thr is not None:
            ax.axhline(
                thr, ls="--", color="#e76f51", lw=1, label=f"生育期阈值 {thr:.2f}"
            )
        ax.set_title(f"{layer}（阴影=玉米生育期；阈值仅来自生育期）")
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
    ax.bar(ms, vals, color=["#95d5b2" if m in SEASON_MONTHS else "#adb5bd" for m in ms])
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
            if stages[k]["date"] in pixels_by_date
        }
        if len(stage_pixels) >= 2:
            panel = render_stages_panel(
                stages, pixels_by_date, out_dir / "ndvi_stages_panel.png"
            )
            if panel:
                written["ndvi_stages_panel.png"] = panel
            written.update(render_stage_maps(stages, pixels_by_date, out_dir))

    return written
