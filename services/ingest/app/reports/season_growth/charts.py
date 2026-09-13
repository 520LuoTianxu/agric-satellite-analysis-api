# -*- coding: utf-8 -*-
"""NDVI / NDMI / S1 VV season charts for season-growth PDF."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from app.core.agri_classify import FLOOD_VV_MAX, WATCH_VV_MAX
from app.reports.land_assessment.paths import FONT_PATH

_DROUGHT_MARKER_COLORS = {
    "normal": "#2e7d32",
    "mild": "#f9a825",
    "moderate": "#ef6c00",
    "severe": "#c62828",
    "unreliable": "#9e9e9e",
    "out_of_season": "#9e9e9e",
}

_FLOOD_MARKER_COLORS = {
    "dry": "#2e7d32",
    "watch": "#f9a825",
    "flood_moderate": "#ef6c00",
    "flood_severe": "#c62828",
}


def _setup_font() -> None:
    if FONT_PATH.exists():
        font_manager.fontManager.addfont(str(FONT_PATH))
        plt.rcParams["font.family"] = "WenQuanYi Zen Hei"
    plt.rcParams["axes.unicode_minus"] = False


def _drought_class_by_date(facts: dict[str, Any]) -> dict[str, str]:
    drought = facts.get("drought") or {}
    out: dict[str, str] = {}
    for sc in drought.get("scene_classes") or drought.get("classified") or []:
        d = sc.get("date")
        if d and d not in out:
            out[str(d)[:10]] = str(sc.get("class") or "")
    return out


def render_ndvi_ndmi_chart(
    facts: dict[str, Any],
    out_path: Path | str,
    *,
    scene_classes: list[dict[str, Any]] | None = None,
) -> Path | None:
    """Write NDVI (colored by drought class) + NDMI line chart. Returns path or None."""
    ndvi = list((facts.get("ndvi") or {}).get("series") or [])
    ndmi = list((facts.get("ndmi") or {}).get("series") or [])
    if not ndvi and not ndmi:
        return None

    class_by_date: dict[str, str] = {}
    if scene_classes is not None:
        for sc in scene_classes:
            d = sc.get("date")
            if d and d not in class_by_date:
                class_by_date[str(d)[:10]] = str(sc.get("class") or "")
    else:
        class_by_date = _drought_class_by_date(facts)

    _setup_font()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=140)
    if ndvi:
        xs = [datetime.fromisoformat(p["date"][:10]) for p in ndvi]
        ys = [float(p["value"]) for p in ndvi]
        # NDVI line (neutral) + drought-colored markers
        ax.plot(xs, ys, color="#66bb6a", linewidth=1.4, label="NDVI", zorder=2)
        colors = [
            _DROUGHT_MARKER_COLORS.get(
                class_by_date.get(p["date"][:10], "normal"), "#2e7d32"
            )
            for p in ndvi
        ]
        ax.scatter(xs, ys, c=colors, s=28, zorder=3, edgecolors="white", linewidths=0.4)
    if ndmi:
        xs2 = [datetime.fromisoformat(p["date"][:10]) for p in ndmi]
        ys2 = [float(p["value"]) for p in ndmi]
        ax.plot(
            xs2,
            ys2,
            color="#1565c0",
            marker="s",
            markersize=3,
            linewidth=1.4,
            label="NDMI",
            alpha=0.9,
            zorder=2,
        )
    win = facts.get("window") or {}
    title = "生育期 NDVI / NDMI 长势曲线"
    label = win.get("label")
    if label:
        title = f"{title}（{label}）"
    ax.set_title(title, fontsize=11)
    ax.set_ylabel("指数值")
    ax.set_xlabel("日期")
    ax.grid(True, alpha=0.25)
    # Compact drought legend
    from matplotlib.lines import Line2D

    handles, labels = ax.get_legend_handles_labels()
    extra = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="w",
            markerfacecolor=c,
            markersize=7,
            label=lab,
        )
        for lab, c in (
            ("正常", _DROUGHT_MARKER_COLORS["normal"]),
            ("轻度", _DROUGHT_MARKER_COLORS["mild"]),
            ("中度", _DROUGHT_MARKER_COLORS["moderate"]),
            ("重度", _DROUGHT_MARKER_COLORS["severe"]),
            ("不可靠/季外", _DROUGHT_MARKER_COLORS["unreliable"]),
        )
    ]
    ax.legend(handles + extra, labels + [h.get_label() for h in extra], loc="best", fontsize=7)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None


def render_s1_vv_chart(
    facts: dict[str, Any],
    out_path: Path | str,
) -> Path | None:
    """Plot S1 VV over time colored by flood class; hlines at flood/watch thresholds."""
    flood = facts.get("flood") or {}
    scenes = list(flood.get("scenes") or [])
    points = [s for s in scenes if s.get("vv") is not None and s.get("date")]
    if not points:
        return None

    _setup_font()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    xs = [datetime.fromisoformat(str(s["date"])[:10]) for s in points]
    ys = [float(s["vv"]) for s in points]
    colors = [
        _FLOOD_MARKER_COLORS.get(str(s.get("class") or "dry"), "#757575") for s in points
    ]

    fig, ax = plt.subplots(figsize=(7.2, 3.4), dpi=140)
    ax.plot(xs, ys, color="#90a4ae", linewidth=1.2, zorder=1)
    ax.scatter(xs, ys, c=colors, s=32, zorder=3, edgecolors="white", linewidths=0.4)
    ax.axhline(
        FLOOD_VV_MAX,
        color="#c62828",
        linestyle="--",
        linewidth=1.1,
        label=f"洪涝阈值 {FLOOD_VV_MAX:.1f} dB",
    )
    ax.axhline(
        WATCH_VV_MAX,
        color="#f9a825",
        linestyle="--",
        linewidth=1.1,
        label=f"关注阈值 {WATCH_VV_MAX:.1f} dB",
    )
    win = facts.get("window") or {}
    title = "Sentinel-1 VV 洪涝监测"
    label = win.get("label")
    if label:
        title = f"{title}（{label}）"
    ax.set_title(title, fontsize=11)
    ax.set_ylabel("VV (dB)")
    ax.set_xlabel("日期")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None


def render_season_charts(
    facts: dict[str, Any],
    out_dir: Path | str,
) -> dict[str, Path]:
    """Render available charts; keys like ndvi_ndmi, s1_vv (omit missing)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    ndvi_path = render_ndvi_ndmi_chart(facts, out_dir / "ndvi_ndmi.png")
    if ndvi_path is not None:
        paths["ndvi_ndmi"] = ndvi_path
    s1_path = render_s1_vv_chart(facts, out_dir / "s1_vv.png")
    if s1_path is not None:
        paths["s1_vv"] = s1_path
    return paths
