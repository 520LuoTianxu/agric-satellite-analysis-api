# -*- coding: utf-8 -*-
"""Matplotlib charts for land-assessment PDF."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from app.reports.land_assessment.paths import FONT_PATH
from app.reports.land_assessment.scoring import PEAK_MONTHS, SEASON_MONTHS


def _setup_font() -> None:
    if FONT_PATH.exists():
        font_manager.fontManager.addfont(str(FONT_PATH))
        plt.rcParams["font.family"] = "WenQuanYi Zen Hei"
    plt.rcParams["axes.unicode_minus"] = False


def render_charts(
    by_date: dict[str, dict[str, float]],
    meta: dict[str, Any],
    out_dir: Path,
) -> dict[str, Path]:
    """Write ndvi/evi/ndwi/monthly charts; return name->path map."""
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
            ax.axhline(thr, ls="--", color="#e76f51", lw=1, label=f"生育期阈值 {thr:.2f}")
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
    return written
