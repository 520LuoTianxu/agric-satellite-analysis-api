"""In-field variation and 1–2 km imagery-farmland contrast for official PDFs.

In-field maps use the land's own parcel pixel JSON (NDVI/EVI/NDRE/NDMI).
Surrounding contrast follows the official v8 imagery-farmland rules: same-day
true-color, excess green index (ExG), 1 km / 2 km rings outside the field pixels.
Project-registered parcels are never the comparison set or map labels.
Missing inputs are returned as missing; values are never invented.

周边对照用的是真彩色影像算出的超绿指数（ExG = 2×绿 − 红 − 蓝，按 8 位影像除以 2.55），
与本田的 NDVI 等指数不是同一指标，数值不能互相比较。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from agric_satellite_analysis_common.biweekly.metric_specs import METRIC_SPECS

# Official v8 imagery-farmland thresholds (true-color 8-bit).
CLOUD_BRIGHT = 200
WATER_BRIGHT_MAX = 12
WATER_EXG_MAX = 5
SETTLE_TEX_MIN = 28
SETTLE_BRIGHT_MIN = 40
SETTLE_WINDOW = 11
FARM_BRIGHT_MIN = 2
FARM_BRIGHT_MAX = 180
VALID_BRIGHT_MIN = 2
VALID_EXG_MIN = -40
VALID_EXG_MAX = 100
MIN_PATCH_PX = 25
GREENNESS_SCALE = 2.55



def _as_day(value: Any) -> str:
    return str(value or "")[:10]


def _columns(product: dict[str, Any]) -> dict[str, list[Any]]:
    return dict((product.get("pixel_data") or {}).get("columns") or {})


CLOUD_LIMIT_PCT = 30.0


def is_eligible_optical(
    product: dict[str, Any], *, optical_days: list[str] | set[str] | None = None
) -> bool:
    """Same official optical rule: S2, parcel cloud else scene cloud <= 30, a metric present."""
    if str(product.get("sensor") or "S2").upper() != "S2":
        return False
    day = _as_day(product.get("date"))
    parcel = product.get("parcel_cloud_cover_pct")
    scene = product.get("cloud_cover")
    cloud = parcel if parcel is not None else scene
    if cloud is not None:
        try:
            if float(cloud) > CLOUD_LIMIT_PCT:
                return False
        except (TypeError, ValueError):
            return False
    elif optical_days is not None:
        if day not in { _as_day(item) for item in optical_days }:
            return False
    else:
        return False
    columns = _columns(product)
    for metric in ("ndvi", "evi", "ndre", "ndmi"):
        key = _metric_key(columns, metric)
        if key and any(value is not None for value in (columns.get(key) or [])):
            return True
        for field in (f"{metric}_avg", metric, metric.upper()):
            if product.get(field) is not None:
                return True
    return False


def _metric_key(columns: dict[str, list[Any]], metric: str) -> str | None:
    for candidate in (metric.upper(), f"{metric.upper()}_AVG", f"{metric}_avg", metric.lower()):
        if candidate in columns:
            return candidate
    return None


def _in_period_products(
    products: list[dict[str, Any]],
    start: str,
    end_exclusive: str,
    *,
    optical_days: list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    out = []
    for product in products or []:
        if str(product.get("sensor") or "S2").upper() != "S2":
            continue
        day = _as_day(product.get("date"))
        if not (start <= day < end_exclusive):
            continue
        if not is_eligible_optical(product, optical_days=optical_days):
            continue
        out.append(product)
    out.sort(key=lambda row: _as_day(row.get("date")))
    return out


def _prior_products(
    products: list[dict[str, Any]],
    start: str,
    *,
    optical_days: list[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    earlier = [
        product
        for product in products or []
        if str(product.get("sensor") or "S2").upper() == "S2"
        and _as_day(product.get("date")) < start
        and _columns(product)
        and is_eligible_optical(product, optical_days=optical_days)
    ]
    earlier.sort(key=lambda row: _as_day(row.get("date")))
    return earlier


def last_prior_period_products(
    products: list[dict[str, Any]],
    start: str,
    *,
    optical_days: list[str] | set[str] | None = None,
    window_days: int = 14,
) -> list[dict[str, Any]]:
    """Keep only the last usable-optics period, not earlier isolated dates."""
    from datetime import date, timedelta

    earlier = _prior_products(products, start, optical_days=optical_days)
    if not earlier or not start:
        return earlier
    start_d = date.fromisoformat(str(start)[:10])
    window_start = (start_d - timedelta(days=window_days)).isoformat()
    in_window = [product for product in earlier if _as_day(product.get("date")) >= window_start]
    if in_window:
        return in_window
    chosen = [earlier[-1]]
    last = date.fromisoformat(_as_day(earlier[-1].get("date")))
    for product in reversed(earlier[:-1]):
        day = date.fromisoformat(_as_day(product.get("date")))
        if (last - day).days <= window_days:
            chosen.append(product)
            last = day
        else:
            break
    return list(reversed(chosen))


def comparison_pair(
    products: list[dict[str, Any]],
    period: dict[str, Any],
    *,
    optical_days: list[str] | set[str] | None = None,
    metric: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The latest usable in-period product and the usable observation before it.

    对比口径全报告统一：本期最近一次有效观测，对比它之前最近的一次有效观测
    （本期内有就取本期内的，否则取本期开始之前的）。图和文字都用这一对日期。
    传入 metric 时，只把该指标有效像元足够的日期算作有效观测。
    """
    from app.reports.biweekly.metric_stats import field_stats

    def usable(product: dict[str, Any]) -> bool:
        if metric is None:
            return True
        return field_stats(product, metric).get("status") == "ok"

    start = _as_day((period or {}).get("start"))
    end_exclusive = _as_day((period or {}).get("end_exclusive"))
    current = [
        row
        for row in _in_period_products(products, start, end_exclusive, optical_days=optical_days)
        if usable(row)
    ]
    if not current:
        return None, None
    latest = current[-1]
    if len(current) >= 2:
        return latest, current[-2]
    earlier = [row for row in _prior_products(products, start, optical_days=optical_days) if usable(row)]
    return latest, (earlier[-1] if earlier else None)


def build_infield_variation(
    *,
    products: list[dict[str, Any]],
    period: dict[str, Any],
    phase: dict[str, Any],
    dest: str | Path,
    optical_days: list[str] | set[str] | None = None,
    metrics: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build one in-field figure per primary metric of the period's stage.

    每个主要指标各出一幅图（绝对值、变化量、相对本田差异），不再只画第一个指标。
    dest 是图件路径模板：``spatial_zones.png`` 会生成 ``spatial_zones_ndvi.png`` 等。
    """
    from app.reports.biweekly.infield_maps import build_infield_metric

    chosen = [m for m in (metrics or phase.get("primary_metrics") or ["ndvi"]) if m in METRIC_SPECS]
    if not chosen:
        chosen = ["ndvi"]
    dest_path = Path(dest)
    built: list[dict[str, Any]] = []
    compact_maps = len(chosen) > 1
    for metric in chosen:
        latest, baseline = comparison_pair(products, period, optical_days=optical_days, metric=metric)
        if latest is None:
            continue
        target = dest_path.with_name(f"{dest_path.stem}_{metric}{dest_path.suffix or '.png'}")
        item = build_infield_metric(
            metric=metric, latest=latest, baseline=baseline, dest=target, compact=compact_maps
        )
        if item is not None:
            built.append(item)
    if not built:
        return None
    first = built[0]
    return {
        "metrics": built,
        # 以下几个键保留给旧调用方：取第一个主要指标。
        "image": first.get("image"),
        "metric": first["metric"],
        "date": first["date"],
        "prior_date": first.get("baseline_date"),
        "pixel_count": first["stats"]["n_valid"],
    }


def _features(image: np.ndarray) -> dict[str, np.ndarray]:
    red = image[:, :, 0].astype(np.float32)
    green = image[:, :, 1].astype(np.float32)
    blue = image[:, :, 2].astype(np.float32)
    exg = 2 * green - red - blue
    bright = (red + green + blue) / 3.0
    return {
        "R": red,
        "G": green,
        "B": blue,
        "exg": exg,
        "bright": bright,
        "vis": exg / GREENNESS_SCALE,
    }


def _cloud(features: dict[str, np.ndarray]) -> np.ndarray:
    return features["bright"] >= CLOUD_BRIGHT


def _water(features: dict[str, np.ndarray]) -> np.ndarray:
    return (features["bright"] <= WATER_BRIGHT_MAX) & (features["exg"] <= WATER_EXG_MAX)


def _valid(features: dict[str, np.ndarray]) -> np.ndarray:
    return (
        (~_cloud(features))
        & (features["bright"] >= VALID_BRIGHT_MIN)
        & (features["exg"] >= VALID_EXG_MIN)
        & (features["exg"] <= VALID_EXG_MAX)
        & ((features["R"] + features["G"] + features["B"]) > 6)
    )


def _stats(mask: np.ndarray, features: dict[str, np.ndarray]) -> dict[str, Any]:
    chosen = mask & _valid(features)
    values = features["vis"][chosen]
    if values.size == 0:
        return {"n": 0, "mean": None, "median": None, "p25": None, "p75": None}
    return {
        "n": int(values.size),
        "mean": round(float(np.mean(values)), 2),
        "median": round(float(np.median(values)), 2),
        "p25": round(float(np.percentile(values, 25)), 2),
        "p75": round(float(np.percentile(values, 75)), 2),
    }


def classify_imagery_farmland(
    rgb: np.ndarray,
    *,
    bounds: tuple[float, float, float, float],
    field_lons: np.ndarray,
    field_lats: np.ndarray,
) -> dict[str, Any] | None:
    """Classify 1–2 km imagery farmland on a georeferenced true-color window."""
    if rgb.ndim != 3 or rgb.shape[2] < 3 or field_lons.size == 0:
        return None
    if not np.isfinite(rgb).all():
        rgb = np.nan_to_num(np.asarray(rgb, dtype=np.float32), nan=0.0)
    height, width = rgb.shape[:2]
    minx, miny, maxx, maxy = [float(v) for v in bounds]
    if maxx <= minx or maxy <= miny or height < 8 or width < 8:
        return None
    features = _features(rgb)
    lat_c = 0.5 * (miny + maxy)
    pixel_m_x = abs(maxx - minx) * 111000.0 * max(0.2, abs(math.cos(math.radians(lat_c)))) / width
    pixel_m_y = abs(maxy - miny) * 111000.0 / height
    # Field mask: analysis pixels placed on the window grid.
    col = np.clip(((field_lons - minx) / (maxx - minx) * width).astype(int), 0, width - 1)
    row = np.clip(((maxy - field_lats) / (maxy - miny) * height).astype(int), 0, height - 1)
    target = np.zeros((height, width), dtype=bool)
    target[row, col] = True
    if int(target.sum()) < 5:
        return None

    try:
        from scipy import ndimage
    except ImportError:
        # 没有 scipy 时不做周边识别，由调用方标记为缺失，不让整份报告失败。
        return None
    from numpy.lib.stride_tricks import sliding_window_view

    window = SETTLE_WINDOW
    padded = np.pad(features["bright"], window // 2, mode="edge")
    tex = sliding_window_view(padded, (window, window)).std(axis=(-2, -1))
    settle = (tex >= SETTLE_TEX_MIN) & (features["bright"] >= SETTLE_BRIGHT_MIN)
    settle = ndimage.binary_closing(settle, iterations=1)
    settle = ndimage.binary_dilation(settle, iterations=1)
    settle = ndimage.binary_opening(settle, iterations=1)

    dist = ndimage.distance_transform_edt(~target, sampling=(pixel_m_y, pixel_m_x))
    # Honest coverage: share of field pixels whose 1/2 km rings stay inside the window.
    reach = _window_reach_m(target, pixel_m_x, pixel_m_y)
    cover_2km = float(np.mean(reach[target] >= 2000.0))
    cover_1km = float(np.mean(reach[target] >= 1000.0))

    base = (
        (dist > 0)
        & (dist <= 2000)
        & (~target)
        & (~settle)
        & (~_water(features))
        & (features["bright"] >= FARM_BRIGHT_MIN)
        & (features["bright"] <= FARM_BRIGHT_MAX)
        & (~_cloud(features))
    )
    labeled, count = ndimage.label(base)
    keep = np.zeros(count + 1, dtype=bool)
    if count:
        sizes = np.array(ndimage.sum(base, labeled, index=range(1, count + 1)))
        keep[1:] = sizes >= MIN_PATCH_PX
    farm = keep[labeled]
    ring1 = farm & (dist > 0) & (dist <= 1000)
    ring2 = farm & (dist > 0) & (dist <= 2000)
    return {
        "features": features,
        "target": target,
        "ring1": ring1,
        "ring2": ring2,
        "settle": settle,
        "dist": dist,
        "stats_field": _stats(target, features),
        "stats_1km": _stats(ring1, features),
        "stats_2km": _stats(ring2, features),
        "cover_1km": cover_1km,
        "cover_2km": cover_2km,
        "pixel_m": (pixel_m_x, pixel_m_y),
    }


def _window_reach_m(target: np.ndarray, pixel_m_x: float, pixel_m_y: float) -> np.ndarray:
    """Approx. distance from each field pixel to the nearest window edge."""
    rows, cols = np.where(target)
    height, width = target.shape
    reach_x = np.minimum(cols, width - 1 - cols) * pixel_m_x
    reach_y = np.minimum(rows, height - 1 - rows) * pixel_m_y
    out = np.zeros(target.shape, dtype=np.float32)
    out[rows, cols] = np.minimum(reach_x, reach_y)
    return out



def render_surrounding_map(
    rgb: np.ndarray,
    classified: dict[str, Any],
    dest: str | Path,
    *,
    date: str,
    bounds: tuple[float, float, float, float],
    field_lons: np.ndarray,
    field_lats: np.ndarray,
    basemap_note: str | None = None,
) -> Path | None:
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        from matplotlib.lines import Line2D
        from matplotlib.patches import Ellipse, Patch

        from app.reports.biweekly.fonts import resolve_yahei_fonts

        regular, bold_path = resolve_yahei_fonts()
        font_manager.fontManager.addfont(str(regular))
        font_manager.fontManager.addfont(str(bold_path))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular)).get_name()
        plt.rcParams["axes.unicode_minus"] = False
        bold = font_manager.FontProperties(fname=str(bold_path))
    except Exception:  # noqa: BLE001
        return None

    canvas = np.array(rgb, copy=True)
    if canvas.dtype != np.uint8:
        vmax = float(np.nanmax(canvas)) if np.isfinite(canvas).any() else 1.0
        if vmax <= 1.5:
            canvas = np.clip(canvas * 255.0, 0, 255).astype(np.uint8)
        else:
            canvas = np.clip(canvas, 0, 255).astype(np.uint8)
    overlay = canvas.astype(np.float32)
    ring2 = classified["ring2"]
    ring1 = classified["ring1"]
    target = classified["target"]
    # Pale yellow farmland wash, darker for the inner ring — not a solid oval.
    overlay[ring2] = overlay[ring2] * 0.62 + np.array([232, 210, 96], dtype=np.float32) * 0.38
    overlay[ring1] = overlay[ring1] * 0.55 + np.array([210, 190, 70], dtype=np.float32) * 0.45
    # Crop to about 2.5 km around the field so the 1–2 km rings fill the frame.
    rows, cols = np.where(target)
    px, py = classified["pixel_m"]
    pad_r = int(max(2500.0 / max(py, 1.0), 40))
    pad_c = int(max(2500.0 / max(px, 1.0), 40))
    r0, r1 = max(0, rows.min() - pad_r), min(overlay.shape[0], rows.max() + pad_r)
    c0, c1 = max(0, cols.min() - pad_c), min(overlay.shape[1], cols.max() + pad_c)
    view = np.clip(overlay[r0:r1, c0:c1], 0, 255).astype(np.uint8)
    fig, ax = plt.subplots(figsize=(7.2, 6.3), dpi=200)
    ax.imshow(view)
    # Field outline in the cropped pixel space.
    tr = target[r0:r1, c0:c1]
    try:
        from skimage import measure

        contours = measure.find_contours(tr.astype(float), 0.5)
        for contour in contours:
            ax.plot(contour[:, 1], contour[:, 0], color="#E6C200", lw=1.6)
    except Exception:
        ys, xs = np.where(tr)
        if xs.size:
            ax.plot(xs, ys, ".", color="#E6C200", markersize=0.6)
    # 1 km / 2 km rings centered on the field.
    cy, cx = float(np.mean(rows) - r0), float(np.mean(cols) - c0)
    for radius_m, ls in ((1000, "--"), (2000, ":")):
        rx, ry = radius_m / max(px, 1.0), radius_m / max(py, 1.0)
        ax.add_patch(Ellipse((cx, cy), 2 * rx, 2 * ry, fill=False, edgecolor="white", lw=1.2, ls=ls))
    # 比例尺：1 公里，白底黑字，保证打印时在影像上可读。
    view_h, view_w = view.shape[:2]
    bar_px = 1000.0 / max(px, 1.0)
    bx, by = 0.05 * view_w, 0.95 * view_h
    box = {"facecolor": "white", "edgecolor": "none", "alpha": 0.85, "pad": 1.5}
    ax.plot([bx, bx + bar_px], [by, by], color="white", lw=3.2, solid_capstyle="butt")
    ax.plot([bx, bx + bar_px], [by, by], color="black", lw=1.4, solid_capstyle="butt")
    ax.text(bx + bar_px / 2, by - 0.015 * view_h, "1 公里", ha="center", va="bottom", fontsize=8, bbox=box)
    ax.annotate(
        "北",
        xy=(0.955 * view_w, 0.03 * view_h),
        xytext=(0.955 * view_w, 0.11 * view_h),
        ha="center",
        va="center",
        fontsize=8,
        bbox=box,
        arrowprops={"arrowstyle": "-|>", "color": "white", "lw": 1.4},
    )
    ax.set_xlim(0, view_w)
    ax.set_ylim(view_h, 0)
    note = basemap_note or f"底图为{_cn_md_safe(date)}哨兵二号真彩色影像"
    ax.set_title(f"本田与周边1—2公里影像识别农田（{note}）", fontsize=10.5, color="#1F2A24", pad=8,
                 fontproperties=bold)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.legend(
        handles=[
            Line2D([0], [0], color="#E6C200", lw=1.6, label="本田范围"),
            Patch(facecolor="#D2BE46", label="周边1公里内影像识别农田"),
            Patch(facecolor="#E8D260", alpha=0.6, label="周边1—2公里影像识别农田"),
            Line2D([0], [0], color="#888888", ls="--", label="1公里范围"),
            Line2D([0], [0], color="#888888", ls=":", label="2公里范围"),
        ],
        frameon=False,
        fontsize=8,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.02),
        ncol=3,
    )
    fig.tight_layout()
    fig.savefig(dest_path, dpi=200, facecolor="white")
    plt.close(fig)
    return dest_path if dest_path.is_file() else None


def _cn_md_safe(day: str) -> str:
    raw = str(day or "")[:10]
    if len(raw) != 10:
        return raw
    return f"{int(raw[5:7])}月{int(raw[8:10])}日"


def _block_stats(block: Any) -> dict:
    if not isinstance(block, dict):
        return {}
    return block.get("stats") or block


def _central(stats: dict | None) -> float | None:
    """Median when present (robust to bright roofs and bare patches), else mean."""
    if not isinstance(stats, dict):
        return None
    value = stats.get("median")
    if value is None:
        value = stats.get("mean")
    return None if value is None else float(value)


def render_greenness_contrast(by_date: dict[str, dict], dest: str | Path) -> Path | None:
    """Bar chart of ExG medians for the field and its 1 km / 2 km farmland rings."""
    if not by_date:
        return None
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager

        from app.reports.biweekly.fonts import resolve_yahei_fonts

        regular, bold_path = resolve_yahei_fonts()
        font_manager.fontManager.addfont(str(regular))
        font_manager.fontManager.addfont(str(bold_path))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular)).get_name()
        plt.rcParams["axes.unicode_minus"] = False
        bold = font_manager.FontProperties(fname=str(bold_path))
    except Exception:  # noqa: BLE001
        return None

    days: list[str] = []
    field: list[float] = []
    r1: list[float] = []
    r2: list[float | None] = []
    for day in sorted(by_date):
        st = _block_stats(by_date[day])
        fv = _central(st.get("field"))
        n1 = _central(st.get("surrounding_1km"))
        n2 = _central(st.get("surrounding_2km"))
        if fv is None or n1 is None:
            continue
        days.append(day)
        field.append(fv)
        r1.append(n1)
        r2.append(n2)
    if not days:
        return None
    x = np.arange(len(days))
    width = 0.24
    fig, ax = plt.subplots(figsize=(7.2, 2.5), dpi=200)
    ax.bar(x - width, field, width, color="#1F4D38", label="本田")
    ax.bar(x, r1, width, color="#7AA86D", label="周边1公里内农田")
    # 2 公里范围缺测的日期不画柱，不用 0 代替。
    have_r2 = [i for i, value in enumerate(r2) if value is not None]
    if have_r2:
        ax.bar(x[have_r2] + width, [r2[i] for i in have_r2], width, color="#C5D5B8", label="周边1—2公里农田")

    def _label(x_pos: float, value: float) -> None:
        va = "top" if value < 0 else "bottom"
        ax.text(x_pos, value, f"{value:.1f}", ha="center", va=va, fontsize=8, color="#1F2A24")

    ax.axhline(0, color="#9AA39D", lw=0.6)
    for i, (fv, n1) in enumerate(zip(field, r1)):
        _label(i - width, fv)
        _label(i, n1)
        if r2[i] is not None:
            _label(i + width, r2[i])
        else:
            ax.text(i + width, 0, "缺测", ha="center", va="bottom", fontsize=7.5, color="#7A7A7A")
    ax.set_xticks(x, days)
    ax.set_ylabel("超绿指数ExG（无量纲）", fontsize=8.5)
    ax.tick_params(labelsize=8.5)
    ax.set_title("本田与周边农田的超绿指数（ExG）中位数，同日影像", loc="left", color="#1F2A24",
                 fontsize=10.5, pad=8, fontproperties=bold)
    ax.margins(y=0.18)
    ax.legend(frameon=False, ncol=3, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.16))
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(dest_path, dpi=200, facecolor="white")
    plt.close(fig)
    return dest_path if dest_path.is_file() else None


def build_surrounding_comparison(
    *,
    rgb: np.ndarray,
    bounds: tuple[float, float, float, float],
    field_lons: np.ndarray,
    field_lats: np.ndarray,
    dest: str | Path,
    date: str,
    by_date: dict[str, dict] | None = None,
    contrast_dest: str | Path | None = None,
    basemap_note: str | None = None,
) -> dict[str, Any] | None:
    classified = classify_imagery_farmland(
        rgb, bounds=bounds, field_lons=field_lons, field_lats=field_lats
    )
    if classified is None:
        return None
    field = classified["stats_field"]
    r1 = classified["stats_1km"]
    r2 = classified["stats_2km"]
    stats = {"field": field, "surrounding_1km": r1, "surrounding_2km": r2}
    if not field["n"] or not r1["n"]:
        return {
            "text": (
                f"{date}的真彩色影像中未能识别出足够的周边农田像元，"
                "本期不作本田与周边农田的对照。对照未改用项目登记地块。"
            ),
            "image": None,
            "date": date,
            "stats": stats,
            "cover_1km": classified["cover_1km"],
            "cover_2km": classified["cover_2km"],
            "comparisons": [],
        }
    image = render_surrounding_map(
        rgb,
        classified,
        dest,
        date=date,
        bounds=bounds,
        field_lons=field_lons,
        field_lats=field_lats,
        basemap_note=basemap_note,
    )
    series = dict(by_date or {})
    series.setdefault(date, {"stats": stats})
    comparisons: list[dict[str, Any]] = []
    for day, block in sorted(series.items()):
        st = _block_stats(block)
        fval = _central(st.get("field"))
        rval = _central(st.get("surrounding_1km"))
        if fval is None or rval is None:
            # 任一侧缺测就不比较，不以 0 代入。
            continue
        comparisons.append(
            {
                "date": day,
                "field": fval,
                "surrounding_1km": rval,
                "surrounding_2km": _central(st.get("surrounding_2km")),
                "difference": fval - rval,
            }
        )
    bits: list[str] = []
    if comparisons:
        parts = []
        for row in comparisons:
            diff = row["difference"]
            word = "高于" if diff > 0 else ("低于" if diff < 0 else "等于")
            parts.append(
                f"{row['date']}本田 {row['field']:.1f}，周边1公里内农田 {row['surrounding_1km']:.1f}，"
                f"本田{word}周边" + (f" {abs(diff):.1f}" if diff else "")
            )
        bits.append("超绿指数（ExG）中位数：" + "；".join(parts) + "。")
    bits.append(
        "周边农田由同日真彩色影像自动识别，不是项目登记地块，作物类型未经确认；"
        "对照只说明相对高低，不作同作物排名。"
    )
    contrast = None
    if contrast_dest is not None and series:
        contrast = render_greenness_contrast(series, contrast_dest)
    return {
        "text": bits,
        "image": str(image) if image else None,
        "contrast_image": str(contrast) if contrast else None,
        "date": date,
        "stats": stats,
        "by_date": {d: _block_stats(block) for d, block in series.items()},
        "comparisons": comparisons,
        "cover_1km": classified["cover_1km"],
        "cover_2km": classified["cover_2km"],
        "judgment": bits[0] if comparisons else "",
    }


def looks_like_earth_search_item_id(item_id: str | None) -> bool:
    raw = str(item_id or "").strip()
    return bool(raw) and "_L2A" in raw.upper() and "stac_bridge" not in raw.lower()


def bounds_from_field_pixels(
    field_lons,
    field_lats,
    *,
    side_km: float | None = None,
) -> tuple[float, float, float, float] | None:
    """Official 10 km processing square from this land's pixels, not another land."""
    try:
        from shapely.geometry import MultiPoint

        from app.core.processing_window import build_processing_window
    except Exception:  # noqa: BLE001
        return None
    if field_lons is None or field_lats is None or len(field_lons) < 5:
        return None
    geom = MultiPoint([(float(lon), float(lat)) for lon, lat in zip(field_lons, field_lats)])
    try:
        square = build_processing_window(geom, side_km)
    except Exception:  # noqa: BLE001
        return None
    minx, miny, maxx, maxy = square.bounds
    return float(minx), float(miny), float(maxx), float(maxy)


def resolve_stac_item_id(
    product: dict[str, Any],
    bounds: tuple[float, float, float, float],
    *,
    search_items=None,
) -> str | None:
    """Use a stored Earth Search id, or search the official catalogue for that day."""
    stored = str(product.get("stac_item_id") or "").strip()
    if looks_like_earth_search_item_id(stored):
        return stored
    day = _as_day(product.get("date"))
    if not day or not bounds:
        return None
    try:
        from datetime import date

        from app.tasks.project_cogs import group_items_by_date, search_items as _search

        getter = search_items or _search
        items = getter(bounds, date.fromisoformat(day), date.fromisoformat(day))
        grouped = group_items_by_date(items or [])
        candidates = grouped.get(day) or list(items or [])
        if not candidates:
            return None
        item_id = str((candidates[0] or {}).get("id") or "").strip()
        return item_id or None
    except Exception:  # noqa: BLE001
        return None


def fetch_processing_window_truecolor(
    *,
    stac_item_id: str,
    bounds: tuple[float, float, float, float],
    max_px: int = 1024,
) -> np.ndarray | None:
    """Download a georeferenced true-color window from the existing STAC source."""
    try:
        from app.core.stac_client import open_stac_client
        from app.tasks.pipeline import STAC_API_URL, read_bands_windowed_parallel, read_rgb_windowed
        from rasterio.transform import from_bounds
    except Exception:  # noqa: BLE001
        return None
    try:
        client = open_stac_client(STAC_API_URL)
        item = None
        try:
            item = client.get_collection("sentinel-2-l2a").get_item(stac_item_id)
        except Exception:
            search = client.search(collections=["sentinel-2-l2a"], ids=[stac_item_id], max_items=1)
            items = list(search.items())
            item = items[0] if items else None
        if item is None:
            return None
        assets = item.assets if hasattr(item, "assets") else (item.get("assets") if isinstance(item, dict) else {})
        def href(name: str) -> str | None:
            asset = assets.get(name) if hasattr(assets, "get") else None
            if asset is None:
                return None
            return getattr(asset, "href", None) or (asset.get("href") if isinstance(asset, dict) else None)

        minx, miny, maxx, maxy = bounds
        width = max_px
        height = max(int(max_px * abs(maxy - miny) / max(abs(maxx - minx), 1e-9)), 8)
        if height > max_px:
            width = max(int(max_px * abs(maxx - minx) / max(abs(maxy - miny), 1e-9)), 8)
            height = max_px
        transform = from_bounds(minx, miny, maxx, maxy, width, height)
        visual = href("visual") or href("visual-preview")
        if visual:
            rgb = read_rgb_windowed(visual, bounds, (height, width), transform)
            if rgb is not None:
                return np.asarray(rgb)
        hrefs = {band: href(band) for band in ("B04", "B03", "B02")}
        if all(hrefs.values()):
            bands = read_bands_windowed_parallel(hrefs, bounds, (height, width), transform, scene_workers=1)
            stacked = np.stack([bands["B04"], bands["B03"], bands["B02"]], axis=-1)
            finite = stacked[np.isfinite(stacked)]
            if finite.size == 0:
                return None
            if float(np.nanmax(finite)) > 1.5:
                stacked = stacked / 10000.0
            lo, hi = 0.02, 0.35
            stretched = np.clip((stacked - lo) / (hi - lo), 0, 1)
            return (stretched ** (1 / 1.2) * 255.0).astype(np.uint8)
    except Exception:  # noqa: BLE001 — surrounding contrast is optional
        return None
    return None
