# -*- coding: utf-8 -*-
"""NDVI / NDMI / S1 VV season charts for season-growth PDF."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

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

_PHENO_BAND_COLORS = (
    "#e8f5e9",
    "#fff8e1",
    "#e3f2fd",
    "#fce4ec",
    "#f3e5f5",
    "#efebe9",
)


def _setup_font() -> None:
    if FONT_PATH.exists():
        font_manager.fontManager.addfont(str(FONT_PATH))
        plt.rcParams["font.family"] = "WenQuanYi Zen Hei"
    plt.rcParams["axes.unicode_minus"] = False


def _drought_class_by_date(facts: dict[str, Any]) -> dict[str, str]:
    drought = facts.get("drought") or {}
    out: dict[str, str] = {}
    source = (
        drought.get("usable_scene_classes")
        or drought.get("scene_classes")
        or drought.get("classified")
        or []
    )
    # Prefer higher-priority class if duplicates sneak in
    prio = {
        "severe": 60,
        "moderate": 50,
        "mild": 40,
        "normal": 30,
        "out_of_season": 20,
        "unreliable": 10,
    }
    for sc in source:
        d = sc.get("date")
        if not d:
            continue
        key = str(d)[:10]
        cls = str(sc.get("class") or "")
        prev = out.get(key)
        if prev is None or prio.get(cls, 0) > prio.get(prev, 0):
            out[key] = cls
    return out


def _to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def point_is_reliable(point: dict[str, Any], class_by_date: dict[str, str] | None = None) -> bool:
    """Official/usable points participate in the trend line; others are hollow."""
    if point.get("official") is False:
        return False
    if point.get("official") is True:
        return True
    q = str(point.get("quality") or point.get("decloud_quality") or "").lower()
    if q in ("bad", "raw", "fair", "poor"):
        return False
    return True


def _split_series(
    series: list[dict[str, Any]], class_by_date: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reliable: list[dict[str, Any]] = []
    unreliable: list[dict[str, Any]] = []
    for p in series:
        if p.get("value") is None or not p.get("date"):
            continue
        if point_is_reliable(p, class_by_date):
            reliable.append(p)
        else:
            unreliable.append(p)
    return reliable, unreliable


def _draw_phenology_bands(ax, facts: dict[str, Any]) -> None:
    bands = list(facts.get("phenology_estimate") or [])
    if not bands:
        return
    ymin, ymax = ax.get_ylim()
    for i, band in enumerate(bands):
        a = _to_dt(band.get("start"))
        b = _to_dt(band.get("end"))
        if not a or not b:
            continue
        color = _PHENO_BAND_COLORS[i % len(_PHENO_BAND_COLORS)]
        ax.axvspan(a, b + timedelta(days=1), color=color, alpha=0.35, zorder=0)
        mid = a + (b - a) / 2
        label = str(band.get("label") or "")
        if label:
            ax.text(
                mid,
                ymax - (ymax - ymin) * 0.04,
                label,
                ha="center",
                va="top",
                fontsize=6.5,
                color="#5d6d5e",
                zorder=4,
            )


def _dry_spells(class_by_date: dict[str, str]) -> list[tuple[datetime, datetime]]:
    drought_dates = sorted(
        _to_dt(d)
        for d, c in class_by_date.items()
        if c in ("mild", "moderate", "severe") and _to_dt(d)
    )
    drought_dates = [d for d in drought_dates if d is not None]
    if len(drought_dates) < 2:
        return []
    spells: list[tuple[datetime, datetime]] = []
    run_start = drought_dates[0]
    prev = drought_dates[0]
    for d in drought_dates[1:]:
        if (d - prev).days <= 8:
            prev = d
            continue
        if prev != run_start:
            spells.append((run_start, prev))
        run_start = d
        prev = d
    if prev != run_start:
        spells.append((run_start, prev))
    return spells


def render_ndvi_ndmi_chart(
    facts: dict[str, Any],
    out_path: Path | str,
    *,
    scene_classes: list[dict[str, Any]] | None = None,
) -> Path | None:
    """NDVI/NDMI: solid line through reliable points only; unreliable = hollow gray."""
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

    fig, ax = plt.subplots(figsize=(7.4, 3.55), dpi=140)

    rel_ndvi, unrel_ndvi = _split_series(ndvi, class_by_date)
    rel_ndmi, unrel_ndmi = _split_series(ndmi, class_by_date)

    if rel_ndvi:
        xs = [datetime.fromisoformat(p["date"][:10]) for p in rel_ndvi]
        ys = [float(p["value"]) for p in rel_ndvi]
        ax.plot(xs, ys, color="#1b4332", linewidth=2.4, label="NDVI（可靠）", zorder=3)
        ax.scatter(
            xs, ys, c="#2e7d32", s=22, zorder=4, edgecolors="white", linewidths=0.4
        )
    if unrel_ndvi:
        xu = [datetime.fromisoformat(p["date"][:10]) for p in unrel_ndvi]
        yu = [float(p["value"]) for p in unrel_ndvi]
        ax.scatter(
            xu,
            yu,
            facecolors="none",
            edgecolors="#cfd8dc",
            s=20,
            linewidths=0.6,
            alpha=0.45,
            zorder=2,
            label="不可靠（不连线）",
        )
    if rel_ndmi:
        xs2 = [datetime.fromisoformat(p["date"][:10]) for p in rel_ndmi]
        ys2 = [float(p["value"]) for p in rel_ndmi]
        ax.plot(
            xs2,
            ys2,
            color="#1565c0",
            linewidth=1.1,
            linestyle="--",
            label="NDMI（可靠）",
            alpha=0.9,
            zorder=3,
        )
    if unrel_ndmi:
        xu2 = [datetime.fromisoformat(p["date"][:10]) for p in unrel_ndmi]
        yu2 = [float(p["value"]) for p in unrel_ndmi]
        ax.scatter(
            xu2,
            yu2,
            facecolors="none",
            edgecolors="#90a4ae",
            s=14,
            marker="s",
            linewidths=0.5,
            alpha=0.35,
            zorder=2,
        )

    # Annotate peak / latest official
    peak = (facts.get("ndvi") or {}).get("peak") or {}
    if peak.get("date") and peak.get("value") is not None:
        pdt = _to_dt(peak["date"])
        if pdt is not None:
            ax.annotate(
                f"峰值 {float(peak['value']):.3f}",
                xy=(pdt, float(peak["value"])),
                xytext=(8, 10),
                textcoords="offset points",
                fontsize=7.5,
                color="#1b4332",
                arrowprops={"arrowstyle": "->", "color": "#1b4332", "lw": 0.7},
            )
    latest = (facts.get("ndvi") or {}).get("latest") or {}
    if latest.get("date") and latest.get("value") is not None:
        ldt = _to_dt(latest["date"])
        peak_date = str(peak.get("date") or "")
        if ldt is not None and str(latest.get("date")) != peak_date:
            ax.annotate(
                f"最新 {float(latest['value']):.3f}",
                xy=(ldt, float(latest["value"])),
                xytext=(-28, -14),
                textcoords="offset points",
                fontsize=7.5,
                color="#37474f",
            )

    # Annotate September dry if present
    sep_dry = [
        (d, c)
        for d, c in class_by_date.items()
        if d[5:7] == "09" and c in ("mild", "moderate", "severe")
    ]
    if sep_dry:
        sdt = _to_dt(sep_dry[0][0])
        if sdt is not None:
            ax.annotate(
                "九月偏干",
                xy=(sdt, ax.get_ylim()[0] + 0.02),
                xytext=(0, 18),
                textcoords="offset points",
                fontsize=7,
                color="#c62828",
                ha="center",
            )

    win = facts.get("window") or {}
    title = "生育期 NDVI / NDMI（可靠点连线）"
    label = win.get("label")
    if label:
        title = f"{title}（{label}）"
    ax.set_title(title, fontsize=10)
    ax.set_ylabel("指数值")
    ax.set_xlabel("日期")
    ax.grid(True, alpha=0.22, zorder=0)
    _draw_phenology_bands(ax, facts)

    # Bottom drought event strip (not rainbow point colors)
    ymin, ymax = ax.get_ylim()
    strip_y = ymin - (ymax - ymin) * 0.08
    ax.set_ylim(strip_y - (ymax - ymin) * 0.02, ymax)
    for d, c in class_by_date.items():
        if c not in ("mild", "moderate", "severe"):
            continue
        dt = _to_dt(d)
        if dt is None:
            continue
        ax.plot(
            [dt, dt],
            [strip_y, strip_y + (ymax - ymin) * 0.05],
            color=_DROUGHT_MARKER_COLORS.get(c, "#c62828"),
            linewidth=2.2,
            solid_capstyle="round",
            zorder=5,
        )
    ax.axhline(strip_y, color="#eceff1", linewidth=6, zorder=1, alpha=0.9)

    handles, labels = ax.get_legend_handles_labels()
    extra = [
        Line2D([0], [0], color=_DROUGHT_MARKER_COLORS["mild"], lw=2, label="干旱事件·轻"),
        Line2D([0], [0], color=_DROUGHT_MARKER_COLORS["moderate"], lw=2, label="干旱事件·中"),
        Line2D([0], [0], color=_DROUGHT_MARKER_COLORS["severe"], lw=2, label="干旱事件·重"),
        Line2D(
            [0], [0], marker="o", color="w", markerfacecolor="none",
            markeredgecolor="#cfd8dc", markersize=6, label="不可靠（不连线）",
        ),
        Patch(facecolor="#e8f5e9", edgecolor="none", label="物候带（估计）"),
    ]
    ax.legend(
        handles + extra,
        labels + [h.get_label() for h in extra],
        loc="lower left",
        fontsize=6.0,
        ncol=3,
        framealpha=0.9,
    )
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None


def render_s1_vv_chart(
    facts: dict[str, Any],
    out_path: Path | str,
) -> Path | None:
    """Plot S1 VV; hlines at flood/watch thresholds -17.0 / -15.0."""
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
        _FLOOD_MARKER_COLORS.get(str(s.get("class") or "dry"), "#757575")
        for s in points
    ]

    fig, ax = plt.subplots(figsize=(7.4, 2.85), dpi=140)
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
    ax.set_title(title, fontsize=10)
    ax.set_ylabel("VV (dB)")
    ax.set_xlabel("日期")
    ax.grid(True, alpha=0.25)
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
            ("正常", _FLOOD_MARKER_COLORS["dry"]),
            ("关注", _FLOOD_MARKER_COLORS["watch"]),
            ("洪涝", _FLOOD_MARKER_COLORS["flood_moderate"]),
            ("洪涝(重)", _FLOOD_MARKER_COLORS["flood_severe"]),
        )
    ]
    ax.legend(
        handles + extra,
        labels + [h.get_label() for h in extra],
        loc="best",
        fontsize=6.5,
    )
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None



# ── Product charts (matplotlib, ECharts-like clean style) ─────────────

_PRODUCT_COLORS = {
    "severe": "#c62828",
    "moderate": "#ef6c00",
    "mild": "#f9a825",
    "normal": "#2e7d32",
    "unreliable": "#9e9e9e",
    "out_of_season": "#bdbdbd",
    "dry": "#2e7d32",
    "watch": "#f9a825",
    "flood_moderate": "#ef6c00",
    "flood_severe": "#c62828",
    "较好": "#2e7d32",
    "正常": "#f9a825",
    "偏弱": "#c62828",
}


def _style_ax(ax, title: str) -> None:
    ax.set_title(title, fontsize=10, color="#143d2b", pad=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#cfd8dc")
    ax.spines["bottom"].set_color("#cfd8dc")
    ax.tick_params(colors="#455a64", labelsize=8)
    ax.grid(True, axis="y", alpha=0.22, linestyle="--")


def render_drought_grade_chart(
    facts: dict[str, Any],
    out_path: Path | str,
    *,
    kind: str = "bar",
) -> Path | None:
    """Drought grade bar/pie from program counts (重度/中度/轻度/正常)."""
    counts = dict((facts.get("drought") or {}).get("counts") or {})
    order = [
        ("severe", "重度"),
        ("moderate", "中度"),
        ("mild", "轻度"),
        ("normal", "正常"),
    ]
    labels = []
    values = []
    colors = []
    for key, label in order:
        n = int(counts.get(key) or 0)
        if n <= 0:
            continue
        labels.append(label)
        values.append(n)
        colors.append(_PRODUCT_COLORS.get(key, "#90a4ae"))
    if not values:
        return None
    _setup_font()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(5.6, 3.2), dpi=140)
    if kind == "pie":
        ax.pie(
            values,
            labels=labels,
            colors=colors,
            autopct=lambda p: f"{p:.0f}%" if p >= 4 else "",
            startangle=90,
            textprops={"fontsize": 8.5},
            wedgeprops={"linewidth": 1, "edgecolor": "white"},
        )
        ax.set_title("干旱等级分布（官方可用景）", fontsize=10, color="#143d2b")
    else:
        bars = ax.bar(labels, values, color=colors, width=0.62, edgecolor="white")
        for b, v in zip(bars, values):
            ax.text(
                b.get_x() + b.get_width() / 2,
                b.get_height() + 0.15,
                str(v),
                ha="center",
                va="bottom",
                fontsize=8.5,
                color="#37474f",
            )
        _style_ax(ax, "干旱等级分布（官方可用景）")
        ax.set_ylabel("景数", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None


def render_yoy_peak_chart(
    facts: dict[str, Any],
    out_path: Path | str,
) -> Path | None:
    """YoY peak comparison bar (2025 vs 2026) with date annotations."""
    yoy = facts.get("yoy") or {}
    this_v = yoy.get("this_peak_value")
    prior_v = yoy.get("prior_peak_value")
    if this_v is None and prior_v is None:
        return None
    try:
        this_f = float(this_v) if this_v is not None else None
        prior_f = float(prior_v) if prior_v is not None else None
    except (TypeError, ValueError):
        return None
    if this_f is None and prior_f is None:
        return None
    _setup_font()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    labels = []
    vals = []
    dates = []
    colors = []
    if prior_f is not None:
        labels.append("上年峰值")
        vals.append(prior_f)
        dates.append(str(yoy.get("prior_peak_date") or "—"))
        colors.append("#1565c0")
    if this_f is not None:
        labels.append("本年峰值")
        vals.append(this_f)
        dates.append(str(yoy.get("this_peak_date") or "—"))
        colors.append("#1b4332")
    fig, ax = plt.subplots(figsize=(5.4, 3.2), dpi=140)
    bars = ax.bar(labels, vals, color=colors, width=0.5, edgecolor="white")
    for b, v, d in zip(bars, vals, dates):
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() + max(vals) * 0.02,
            f"{v:.3f}\n{d}",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#37474f",
        )
    shift = (yoy.get("peak_date_shift") or {}).get("label") or ""
    title = "年度峰值对比（程序）"
    if shift:
        title = f"{title} · {shift}"
    _style_ax(ax, title)
    ax.set_ylabel("NDVI", fontsize=8)
    ax.set_ylim(0, max(vals) * 1.28 if vals else 1)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None


def render_monthly_ndvi_bar(
    facts: dict[str, Any],
    out_path: Path | str,
) -> Path | None:
    """Monthly NDVI mean bar from timeline."""
    timeline = list(facts.get("timeline") or [])
    rows = [r for r in timeline if r.get("ndvi_mean") is not None]
    if not rows:
        # derive from series if timeline lacks means
        series = list((facts.get("ndvi") or {}).get("series") or [])
        by_m: dict[str, list[float]] = {}
        for p in series:
            d = str(p.get("date") or "")
            v = p.get("value")
            if len(d) < 7 or v is None:
                continue
            if p.get("official") is False:
                continue
            by_m.setdefault(d[:7], []).append(float(v))
        rows = [
            {"month": m, "period_label": f"{int(m[5:7])}月", "ndvi_mean": sum(vs) / len(vs)}
            for m, vs in sorted(by_m.items())
            if vs
        ]
    if not rows:
        return None
    _setup_font()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    labels = [str(r.get("period_label") or r.get("month") or "") for r in rows]
    vals = [float(r["ndvi_mean"]) for r in rows]
    fig, ax = plt.subplots(figsize=(5.8, 3.1), dpi=140)
    bars = ax.bar(labels, vals, color="#2e7d32", width=0.58, edgecolor="white", alpha=0.9)
    for b, v in zip(bars, vals):
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() + 0.01,
            f"{v:.2f}",
            ha="center",
            va="bottom",
            fontsize=7.5,
            color="#37474f",
        )
    _style_ax(ax, "月均 NDVI（官方/可用）")
    ax.set_ylabel("NDVI", fontsize=8)
    ax.set_ylim(0, max(vals) * 1.25 if vals else 1)
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None


def render_s1_status_doughnut(
    facts: dict[str, Any],
    out_path: Path | str,
) -> Path | None:
    """S1 status doughnut: 正常/关注/洪涝."""
    counts = dict((facts.get("flood") or {}).get("counts") or {})
    mapping = [
        ("dry", "正常", _PRODUCT_COLORS["dry"]),
        ("watch", "关注", _PRODUCT_COLORS["watch"]),
        ("flood_moderate", "洪涝", _PRODUCT_COLORS["flood_moderate"]),
        ("flood_severe", "洪涝(重)", _PRODUCT_COLORS["flood_severe"]),
    ]
    labels, values, colors = [], [], []
    # merge flood_moderate + flood_severe into display if both small — keep separate
    for key, label, col in mapping:
        n = int(counts.get(key) or 0)
        if n > 0:
            labels.append(label)
            values.append(n)
            colors.append(col)
    if not values:
        return None
    _setup_font()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(4.8, 3.2), dpi=140)
    wedges, texts, autotexts = ax.pie(
        values,
        labels=labels,
        colors=colors,
        autopct=lambda p: f"{p:.0f}%" if p >= 4 else "",
        startangle=90,
        pctdistance=0.75,
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 1.2},
        textprops={"fontsize": 8},
    )
    centre = plt.Circle((0, 0), 0.35, fc="white")
    ax.add_artist(centre)
    total = sum(values)
    ax.text(0, 0, f"{total}\n景", ha="center", va="center", fontsize=11, color="#143d2b")
    ax.set_title("S1 洪涝状态分布", fontsize=10, color="#143d2b")
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None


def render_growth_grade_pie(
    facts: dict[str, Any],
    out_path: Path | str,
) -> Path | None:
    """Pie for spatial.grade_shares (较好/正常/偏弱)."""
    shares = (facts.get("spatial") or {}).get("grade_shares") or {}
    if not shares or not shares.get("n"):
        return None
    labels = list(shares.get("labels") or ("较好", "正常", "偏弱"))
    counts = shares.get("counts") or {}
    nz = [
        (lab, int(counts.get(lab) or 0), _PRODUCT_COLORS.get(lab, "#90a4ae"))
        for lab in labels
        if int(counts.get(lab) or 0) > 0
    ]
    if not nz:
        return None
    _setup_font()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(4.8, 3.2), dpi=140)
    ax.pie(
        [c for _, c, _ in nz],
        labels=[lab for lab, _, _ in nz],
        colors=[col for _, _, col in nz],
        autopct=lambda p: f"{p:.0f}%" if p >= 3 else "",
        startangle=90,
        textprops={"fontsize": 8.5},
        wedgeprops={"linewidth": 1, "edgecolor": "white"},
    )
    rule = shares.get("rule_zh") or ""
    title = f"像元长势等级占比（n={shares.get('n')}）"
    if rule:
        title = f"{title}\n{rule}"
    ax.set_title(title, fontsize=9, color="#143d2b")
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out if out.exists() else None


def render_ndvi_spatial_map(
    facts: dict[str, Any],
    out_path: Path | str,
) -> Path | None:
    """Scatter heatmap from lonlat_v1 pixel_points (sparse, not a grid)."""
    spatial = facts.get("spatial") or {}
    points = list(spatial.get("pixel_points") or [])
    if len(points) < 3:
        return None
    lons = [float(p["lon"]) for p in points]
    lats = [float(p["lat"]) for p in points]
    vals = [float(p["ndvi"]) for p in points]
    _setup_font()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(4.8, 4.2), dpi=140)
    sc = ax.scatter(
        lons,
        lats,
        c=vals,
        cmap="RdYlGn",
        s=42,
        vmin=max(-0.1, min(vals) - 0.05),
        vmax=min(1.0, max(vals) + 0.05),
        edgecolors="white",
        linewidths=0.35,
        zorder=3,
    )
    cb = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.04)
    cb.set_label("NDVI", fontsize=8)
    cb.ax.tick_params(labelsize=7)
    date_s = spatial.get("pixel_date") or spatial.get("latest_rgb_date") or ""
    title = "地块 NDVI 空间分布（稀疏像元）"
    if date_s:
        title = f"{title}\n{date_s}"
    ax.set_title(title, fontsize=9, color="#143d2b")
    ax.set_xlabel("经度", fontsize=7)
    ax.set_ylabel("纬度", fontsize=7)
    ax.tick_params(labelsize=6.5)
    ax.set_aspect("equal", adjustable="datalim")
    ax.grid(True, alpha=0.2, linestyle="--")
    for spine in ax.spines.values():
        spine.set_color("#cfd8dc")
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    # store path hint on spatial for PDF
    spatial["ndvi_map_path"] = str(out)
    spatial["ndvi_local_path"] = str(out)
    return out if out.exists() else None


def render_season_charts(
    facts: dict[str, Any],
    out_dir: Path | str,
) -> dict[str, Path]:
    """Render available charts; omit missing. Product keys + legacy ndvi_ndmi/s1_vv."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    ndvi_path = render_ndvi_ndmi_chart(facts, out_dir / "ndvi_ndmi.png")
    if ndvi_path is not None:
        paths["ndvi_ndmi"] = ndvi_path
    s1_path = render_s1_vv_chart(facts, out_dir / "s1_vv.png")
    if s1_path is not None:
        paths["s1_vv"] = s1_path
    drought = render_drought_grade_chart(facts, out_dir / "drought_grades.png", kind="bar")
    if drought is not None:
        paths["drought_grades"] = drought
    yoy = render_yoy_peak_chart(facts, out_dir / "yoy_peak.png")
    if yoy is not None:
        paths["yoy_peak"] = yoy
    monthly = render_monthly_ndvi_bar(facts, out_dir / "monthly_ndvi.png")
    if monthly is not None:
        paths["monthly_ndvi"] = monthly
    s1_donut = render_s1_status_doughnut(facts, out_dir / "s1_status.png")
    if s1_donut is not None:
        paths["s1_status"] = s1_donut
    grade_pie = render_growth_grade_pie(facts, out_dir / "growth_grades.png")
    if grade_pie is not None:
        paths["growth_grades"] = grade_pie
    ndvi_map = render_ndvi_spatial_map(facts, out_dir / "ndvi_spatial.png")
    if ndvi_map is not None:
        paths["ndvi_spatial"] = ndvi_map
        # keep facts.spatial paths in sync for PDF dual visual
        spatial = facts.get("spatial")
        if isinstance(spatial, dict):
            spatial["ndvi_map_path"] = str(ndvi_map)
            spatial["ndvi_local_path"] = str(ndvi_map)
    return paths
