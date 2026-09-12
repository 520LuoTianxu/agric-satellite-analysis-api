# -*- coding: utf-8 -*-
"""NDVI / NDMI season charts for season-growth PDF."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

from app.reports.land_assessment.paths import FONT_PATH


def _setup_font() -> None:
    if FONT_PATH.exists():
        font_manager.fontManager.addfont(str(FONT_PATH))
        plt.rcParams["font.family"] = "WenQuanYi Zen Hei"
    plt.rcParams["axes.unicode_minus"] = False


def render_ndvi_ndmi_chart(
    facts: dict[str, Any],
    out_path: Path | str,
) -> Path | None:
    """Write a dual-axis / dual-line NDVI+NDMI season chart. Returns path or None."""
    ndvi = list((facts.get("ndvi") or {}).get("series") or [])
    ndmi = list((facts.get("ndmi") or {}).get("series") or [])
    if not ndvi and not ndmi:
        return None

    _setup_font()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(7.2, 3.6), dpi=140)
    if ndvi:
        xs = [datetime.fromisoformat(p["date"][:10]) for p in ndvi]
        ys = [float(p["value"]) for p in ndvi]
        ax.plot(xs, ys, color="#2e7d32", marker="o", markersize=3, linewidth=1.6, label="NDVI")
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
    ax.legend(loc="best", fontsize=8)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None
