"""In-field variation and 1–2 km imagery-farmland contrast for official PDFs.

In-field maps use the land's own parcel pixel JSON (NDVI/EVI/NDRE/NDMI).
Surrounding contrast follows the official v8 imagery-farmland rules: same-day
true-color, visible greenness, 1 km / 2 km rings outside the field pixels.
Project-registered parcels are never the comparison set or map labels.
Missing inputs are returned as missing; values are never invented.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

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

PLAIN = {"ndvi": "绿度", "evi": "增强绿度", "ndre": "叶片营养", "ndmi": "植被水分"}


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


def _finite_points(product: dict[str, Any], metric: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    columns = _columns(product)
    key = _metric_key(columns, metric)
    if not key:
        return np.array([]), np.array([]), np.array([])
    lons, lats, values = [], [], []
    for lon, lat, value in zip(columns.get("lon") or [], columns.get("lat") or [], columns[key]):
        if lon is None or lat is None or value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
        lons.append(float(lon))
        lats.append(float(lat))
    return np.asarray(lons), np.asarray(lats), np.asarray(values)


def _match_change(
    current: dict[str, Any], prior: dict[str, Any], metric: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    lon_a, lat_a, val_a = _finite_points(current, metric)
    lon_b, lat_b, val_b = _finite_points(prior, metric)
    if lon_a.size == 0 or lon_b.size == 0:
        return None
    index = {(round(float(lon), 6), round(float(lat), 6)): i for i, (lon, lat) in enumerate(zip(lon_b, lat_b))}
    lons, lats, delta = [], [], []
    for lon, lat, value in zip(lon_a, lat_a, val_a):
        hit = index.get((round(float(lon), 6), round(float(lat), 6)))
        if hit is None:
            continue
        lons.append(lon)
        lats.append(lat)
        delta.append(float(value) - float(val_b[hit]))
    if not delta:
        return None
    return np.asarray(lons), np.asarray(lats), np.asarray(delta)



def _to_local_meters(lons: np.ndarray, lats: np.ndarray):
    lon0 = float(np.mean(lons))
    lat0 = float(np.mean(lats))
    try:
        from pyproj import Transformer

        tr = Transformer.from_crs(
            "EPSG:4326",
            f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m +no_defs",
            always_xy=True,
        )
        xs, ys = tr.transform(lons, lats)
        return np.asarray(xs), np.asarray(ys)
    except Exception:  # noqa: BLE001
        # Approximate meters around the centroid if pyproj is unavailable.
        xs = (lons - lon0) * 111000.0 * max(0.2, abs(np.cos(np.radians(lat0))))
        ys = (lats - lat0) * 111000.0
        return xs, ys


def _region_name(x: float, y: float, xmin: float, xmax: float, ymin: float, ymax: float) -> str:
    fx = (x - xmin) / max(xmax - xmin, 1e-6)
    fy = (y - ymin) / max(ymax - ymin, 1e-6)
    ew = "西" if fx < 1 / 3 else ("东" if fx > 2 / 3 else "中")
    ns = "南" if fy < 1 / 3 else ("北" if fy > 2 / 3 else "中")
    table = {
        ("中", "中"): "中部",
        ("中", "东"): "中东部",
        ("中", "西"): "中西部",
        ("南", "中"): "南中部",
        ("北", "中"): "北中部",
        ("南", "东"): "南中部偏东",
        ("南", "西"): "南中部偏西",
        ("北", "东"): "北中部偏东",
        ("北", "西"): "北中部偏西",
    }
    return table.get((ns, ew), f"{ns}{ew}部")


def describe_infield_zones(
    xs: np.ndarray,
    ys: np.ndarray,
    values: np.ndarray,
    *,
    prior_values: np.ndarray | None = None,
    deltas: np.ndarray | None = None,
    name: str,
    dates: list[str],
) -> dict[str, Any]:
    """Locate persistently low or declining patches in field-relative language."""
    if xs.size < 10:
        return {"bullets": [], "summary": "", "next_week": ""}
    xmin, xmax, ymin, ymax = float(xs.min()), float(xs.max()), float(ys.min()), float(ys.max())
    low_cut = float(np.percentile(values, 33))
    mean = float(np.mean(values))
    regions: dict[str, dict[str, list[int]]] = {}
    for i, (x, y) in enumerate(zip(xs, ys)):
        label = _region_name(float(x), float(y), xmin, xmax, ymin, ymax)
        bucket = regions.setdefault(label, {"low": [], "down": [], "all": []})
        bucket["all"].append(i)
        if values[i] <= low_cut or values[i] < mean:
            if values[i] <= low_cut:
                bucket["low"].append(i)
        if deltas is not None and i < len(deltas) and deltas[i] < 0:
            bucket["down"].append(i)
    bullets: list[str] = []
    summary_bits: list[str] = []
    n = len(values)
    # Prefer larger, clearer zones first.
    ranked = sorted(regions.items(), key=lambda item: len(item[1]["low"]) + len(item[1]["down"]), reverse=True)
    for label, bucket in ranked[:3]:
        low_n, down_n, all_n = len(bucket["low"]), len(bucket["down"]), len(bucket["all"])
        if low_n < max(8, 0.08 * n) and down_n < max(8, 0.06 * n):
            continue
        share = 100.0 * low_n / n
        # Compact size of a declining cluster, if it is spatially tight.
        size_txt = ""
        focus_idx = bucket["down"] or bucket["low"]
        if focus_idx:
            fx, fy = xs[focus_idx], ys[focus_idx]
            width = float(fx.max() - fx.min())
            height = float(fy.max() - fy.min())
            side = max(width, height)
            if 25 <= side <= 400:
                size_txt = f"约{int(round(side / 10.0) * 10)}米见方的"
        if down_n >= max(8, 0.06 * n) and size_txt:
            already_low = 0
            if prior_values is not None:
                already_low = sum(1 for i in bucket["down"] if i < len(prior_values) and prior_values[i] <= low_cut)
            extra = "多数点是在原本偏低的基础上继续走弱。" if already_low >= 0.5 * down_n and down_n else ""
            bullets.append(f"{label}：{size_txt}集中变差斑块，{name}明显下降。{extra}".replace("。。", "。"))
            summary_bits.append(f"{label}{size_txt}斑块明显变差")
        elif low_n >= max(8, 0.08 * n):
            persist = ""
            if prior_values is not None:
                persist_n = sum(1 for i in bucket["low"] if i < len(prior_values) and prior_values[i] <= low_cut)
                if persist_n >= 0.6 * low_n:
                    persist = "两次观测都明显低于本田平均，属于原来就偏低。"
            bullets.append(
                f"{label}：相对偏低的范围约占田内观测点的{share:.0f}%。{persist}".strip()
            )
            summary_bits.append(f"{label}偏低，约占田内观测点的{share:.0f}%")
    if not bullets:
        share = 100.0 * float(np.mean(values <= low_cut))
        bullets.append(f"约 {share:.0f}% 的像元处于本期较低的三分之一，已在图上标出。")
        summary_bits.append(f"约{share:.0f}%的像元处于本期较低的三分之一")
    bullets.append("因此，相对上一周期的整体变化，不能理解为偏低或变差的部位已经好转。下图只用于标明位置，不换算受灾面积，也不推断病虫害或灌溉原因。")
    summary = "；".join(summary_bits) + "。整体读数升高，不等于各处都已好转。"
    place_bits = []
    for bit in summary_bits[:2]:
        if "偏低，约占" in bit:
            place_bits.append(bit.split("偏低，约占", 1)[0])
        elif "斑块明显变差" in bit:
            place_bits.append(bit.replace("斑块明显变差", "").rstrip("的"))
        else:
            place_bits.append(bit)
    if place_bits and all("约占" not in p and "像元" not in p for p in place_bits):
        next_week = "核对" + "、".join(place_bits) + "的偏低或变差部位，并核对这些部位的墒情。"
    elif summary_bits:
        next_week = "核对" + "、".join(summary_bits[:2]) + "，并核对这些部位的墒情。"
    else:
        next_week = "核对田内偏低或变差的部位，并核对这些部位的墒情。"
    return {"bullets": bullets, "summary": summary, "next_week": next_week, "dates": dates}


def _style_field_ax(ax, xs, ys, title, cbar=None, ylabel: bool = False):
    ax.set_title(title, fontsize=9, color="#1F4D38")
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_aspect("equal", adjustable="box")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.text(0.02, 0.98, "N", transform=ax.transAxes, ha="left", va="top", fontsize=8, fontweight="bold")
    # 100 m scale bar at the lower left.
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    x0 = xmin + 0.08 * (xmax - xmin)
    y0 = ymin + 0.08 * (ymax - ymin)
    ax.plot([x0, x0 + 100], [y0, y0], color="#222", lw=1.4)
    ax.text(x0 + 50, y0 + 0.02 * (ymax - ymin), "100 m", ha="center", va="bottom", fontsize=7)
    try:
        from scipy.spatial import ConvexHull

        hull = ConvexHull(np.column_stack([xs, ys]))
        verts = np.append(hull.vertices, hull.vertices[0])
        ax.plot(xs[verts], ys[verts], color="#7A3E2E", lw=0.8, alpha=0.8)
    except Exception:
        pass


def build_infield_variation(
    *,
    products: list[dict[str, Any]],
    period: dict[str, Any],
    phase: dict[str, Any],
    dest: str | Path,
    optical_days: list[str] | set[str] | None = None,
) -> dict[str, Any] | None:
    """Build an in-field map of the period's primary metric, with location bullets."""
    metrics = [m for m in (phase.get("primary_metrics") or ["ndvi"]) if m in PLAIN]
    if not metrics:
        metrics = ["ndvi"]
    metric = metrics[0]
    start = _as_day((period or {}).get("start"))
    end_exclusive = _as_day((period or {}).get("end_exclusive"))
    current = _in_period_products(products, start, end_exclusive, optical_days=optical_days)
    if not current:
        return None
    latest = current[-1]
    lons, lats, values = _finite_points(latest, metric)
    if values.size < 10:
        return None
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    name = PLAIN.get(metric, metric)
    latest_day = _as_day(latest.get("date"))
    xs, ys = _to_local_meters(lons, lats)

    # Prefer intra-period change when two eligible dates exist; else vs last prior date.
    earlier = current[-2] if len(current) >= 2 else None
    prior_day = _as_day(earlier.get("date")) if earlier else None
    change_vs = earlier
    if change_vs is None:
        prior_list = _prior_products(products, start, optical_days=optical_days)
        if prior_list:
            change_vs = prior_list[-1]
            prior_day = _as_day(change_vs.get("date"))
    change = _match_change(latest, change_vs, metric) if change_vs is not None else None

    # Align prior values onto latest coordinates when possible.
    prior_values = None
    deltas = None
    if change is not None:
        clon, clat, cdelta = change
        # Reindex deltas onto latest lon/lat by rounded coordinate.
        index = {(round(float(x), 6), round(float(y), 6)): i for i, (x, y) in enumerate(zip(clon, clat))}
        aligned = []
        for lon, lat in zip(lons, lats):
            hit = index.get((round(float(lon), 6), round(float(lat), 6)))
            aligned.append(float(cdelta[hit]) if hit is not None else np.nan)
        deltas = np.asarray(aligned)
        # Prior values = current - delta where available.
        prior_values = np.where(np.isfinite(deltas), values - deltas, np.nan)

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from app.reports.biweekly.fonts import resolve_yahei_fonts
        from matplotlib import font_manager

        regular, _bold = resolve_yahei_fonts()
        font_manager.fontManager.addfont(str(regular))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular)).get_name()
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:  # noqa: BLE001
        return None

    first = current[0] if len(current) >= 2 else None
    panels: list[tuple[str, np.ndarray, str]] = []
    if first is not None and first is not latest:
        flons, flats, fvals = _finite_points(first, metric)
        if fvals.size >= 10:
            fxs, fys = _to_local_meters(flons, flats)
            panels.append((_as_day(first.get("date")), fvals, "level"))
            first_xy = (fxs, fys)
        else:
            first_xy = None
    else:
        first_xy = None
    panels.append((latest_day, values, "level"))
    if change is not None:
        panels.append((f"这两周的变化" if first is not None else f"相对 {prior_day} 的变化", deltas, "delta"))
        # Persistently low: low on latest and, if available, on the comparison date.
        low_latest = values <= float(np.nanpercentile(values, 33))
        persist = low_latest
        if prior_values is not None and np.isfinite(prior_values).any():
            finite = np.isfinite(prior_values)
            persist = low_latest & finite & (prior_values <= np.nanpercentile(prior_values[finite], 33))
        panels.append(("持续相对偏低的位置", persist.astype(float), "persist"))

    nrows = 2 if len(panels) > 2 else 1
    ncols = 2 if len(panels) > 1 else 1
    fig, axes = plt.subplots(nrows, ncols, figsize=(8.6, 3.5 * nrows), dpi=150)
    axes_list = np.atleast_1d(axes).ravel().tolist()
    cmap_level = "YlGn"
    for i, (title, data, kind) in enumerate(panels):
        ax = axes_list[i]
        if kind == "level":
            if i == 0 and first_xy is not None:
                px, py, pv = first_xy[0], first_xy[1], data
            else:
                px, py, pv = xs, ys, data
            sc = ax.scatter(px, py, c=pv, s=7, cmap=cmap_level, linewidths=0, vmin=None)
            _style_field_ax(ax, px, py, f"{title}  {name}" if kind == "level" and "变化" not in str(title) else title)
            fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.03)
        elif kind == "delta":
            mask = np.isfinite(data)
            sc = ax.scatter(xs[mask], ys[mask], c=data[mask], s=7, cmap="RdYlGn", linewidths=0)
            ax.scatter(xs[mask & (data < 0)], ys[mask & (data < 0)], s=9, facecolors="none", edgecolors="#7A3E2E", linewidths=0.4)
            _style_field_ax(ax, xs, ys, title)
            fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.03)
        else:
            colors = np.where(data > 0.5, "#C9892D", "#E8E6E1")
            ax.scatter(xs, ys, c=colors, s=7, linewidths=0)
            ax.scatter([], [], c="#C9892D", s=12, label="两次都明显偏低")
            ax.scatter([], [], c="#E8E6E1", s=12, label="其余观测")
            _style_field_ax(ax, xs, ys, title)
            ax.legend(frameon=False, fontsize=7, loc="lower right")
    for ax in axes_list[len(panels):]:
        ax.axis("off")
    fig.suptitle(f"田内{name}分布与变化（真实观测点，不插值；偏低范围不作受灾面积）", fontsize=10, color="#1F4D38", y=0.995)
    fig.tight_layout()
    fig.savefig(dest_path, dpi=150, facecolor="white")
    plt.close(fig)
    if not dest_path.is_file():
        return None

    zone = describe_infield_zones(
        xs,
        ys,
        values,
        prior_values=prior_values,
        deltas=deltas,
        name=name,
        dates=[_as_day(row.get("date")) for row in current],
    )
    return {
        "text": zone["bullets"],
        "image": str(dest_path),
        "metric": metric,
        "date": latest_day,
        "prior_date": prior_day,
        "pixel_count": int(values.size),
        "summary": zone["summary"],
        "next_week": zone["next_week"],
        "bullets": zone["bullets"],
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

    from scipy import ndimage
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
) -> Path | None:
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from app.reports.biweekly.fonts import resolve_yahei_fonts
        from matplotlib import font_manager
        from matplotlib.patches import Patch, Circle
        from matplotlib.lines import Line2D

        regular, _bold = resolve_yahei_fonts()
        font_manager.fontManager.addfont(str(regular))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular)).get_name()
        plt.rcParams["axes.unicode_minus"] = False
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
    fig, ax = plt.subplots(figsize=(7.6, 6.4), dpi=150)
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
        ellipse = plt.matplotlib.patches.Ellipse((cx, cy), 2 * rx, 2 * ry, fill=False, edgecolor="white", lw=1.1, ls=ls)
        ax.add_patch(ellipse)
    ax.text(cx, cy, "本田", color="white", ha="center", va="center", fontsize=8, fontweight="bold")
    ax.text(0.04, 0.96, "N", transform=ax.transAxes, color="white", fontsize=9, fontweight="bold")
    ax.set_title(f"周边约1—2公里田地长势对照（底图为{_cn_md_safe(date)}真实卫星影像）", fontsize=11, color="#1F4D38", pad=8)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.legend(
        handles=[
            Patch(facecolor="#E6C200", label="本田"),
            Patch(facecolor="#D2BE46", label="影像中识别的田地（1公里范围）"),
            Line2D([0], [0], color="white", ls="--", label="1公里、2公里范围"),
        ],
        frameon=False,
        fontsize=8,
        loc="lower left",
        labelcolor="black",
    )
    fig.tight_layout()
    fig.savefig(dest_path, dpi=150, facecolor="white")
    plt.close(fig)
    return dest_path if dest_path.is_file() else None


def _cn_md_safe(day: str) -> str:
    raw = str(day or "")[:10]
    if len(raw) != 10:
        return raw
    return f"{int(raw[5:7])}月{int(raw[8:10])}日"


def render_greenness_contrast(by_date: dict[str, dict], dest: str | Path) -> Path | None:
    if not by_date:
        return None
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from app.reports.biweekly.fonts import resolve_yahei_fonts
        from matplotlib import font_manager

        regular, bold = resolve_yahei_fonts()
        font_manager.fontManager.addfont(str(regular))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(regular)).get_name()
        plt.rcParams["axes.unicode_minus"] = False
    except Exception:  # noqa: BLE001
        return None
    def _stats(block: dict) -> dict:
        if not isinstance(block, dict):
            return {}
        return block.get("stats") or block

    days: list[str] = []
    field: list[float] = []
    r1: list[float] = []
    r2: list[float | None] = []
    for day in sorted(by_date):
        st = _stats(by_date[day])
        fv = (st.get("field") or {}).get("mean")
        n1 = (st.get("surrounding_1km") or {}).get("mean")
        n2 = (st.get("surrounding_2km") or {}).get("mean")
        if fv is None or n1 is None:
            continue
        days.append(day)
        field.append(float(fv))
        r1.append(float(n1))
        r2.append(None if n2 is None else float(n2))
    if not days:
        return None
    x = np.arange(len(days))
    width = 0.24
    fig, ax = plt.subplots(figsize=(8.4, 2.5), dpi=150)
    ax.bar(x - width, field, width, color="#1F4D38", label="本田")
    ax.bar(x, r1, width, color="#7AA86D", label="周边1公里田地")
    if any(v is not None for v in r2):
        ax.bar(x + width, [0 if v is None else v for v in r2], width, color="#C5D5B8", label="周边2公里田地")
    def _label(x_pos: float, value: float, color: str) -> None:
        va = "top" if value < 0 else "bottom"
        ax.text(x_pos, value, f"{value:.1f}", ha="center", va=va, fontsize=8, color=color)

    ax.axhline(0, color="#D0D0D0", lw=0.6)
    for i, (fv, n1) in enumerate(zip(field, r1)):
        _label(i - width, fv, "#1F4D38")
        _label(i, n1, "#3E6B3C")
        if r2[i] is not None:
            _label(i + width, r2[i], "#5A6A52")
    ax.set_xticks(x, [_cn_md_safe(d) for d in days])
    ax.set_ylabel("可见光绿度")
    ax.set_title("本田与周边田地绿度对比（同一天影像，只比相对高低）", loc="left", color="#1F4D38", fontsize=11, pad=8)
    diffs = []
    for d, fv, n1 in zip(days, field, r1):
        if fv is not None and n1 is not None:
            diffs.append(f"{_cn_md_safe(d)} {fv-n1:+.1f}")
    if diffs:
        ax.text(1.0, 1.02, "本田减周边1公里：" + "，".join(diffs), transform=ax.transAxes, ha="right", va="bottom", fontsize=8, color="#1F4D38")
    ax.legend(frameon=False, ncol=3, fontsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    fig.savefig(dest_path, dpi=150, facecolor="white")
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
                f"{_cn_md_safe(date)}的真彩色窗口未能识别出足够的周边影像农田像元，"
                "因此本期不对「本田是否高于周边」作出判断。对照没有改用项目登记地块。"
            ),
            "image": None,
            "date": date,
            "stats": stats,
            "cover_1km": classified["cover_1km"],
            "cover_2km": classified["cover_2km"],
        }
    image = render_surrounding_map(
        rgb,
        classified,
        dest,
        date=date,
        bounds=bounds,
        field_lons=field_lons,
        field_lats=field_lats,
    )
    series = dict(by_date or {})
    series.setdefault(date, {"stats": stats})
    bits: list[str] = []
    snippets = []
    higher_all = True
    for day, block in sorted(series.items()):
        st = block.get("stats") or block
        fmean = (st.get("field") or {}).get("mean")
        rmean = (st.get("surrounding_1km") or {}).get("mean")
        if fmean is None or rmean is None:
            continue
        if fmean <= rmean:
            higher_all = False
        delta = fmean - rmean
        word = "高出" if delta > 0 else "低于"
        snippets.append(f"{_cn_md_safe(day)} {fmean:.1f} 对一公里 {rmean:.1f}（{word} {abs(delta):.1f}）")
    if snippets:
        lead = "本田绿度两次均高于周边田地：" if (higher_all and len(snippets) > 1) else (
            "本田绿度高于周边田地：" if higher_all else "本田可见光绿度不高于周边田地："
        )
        bits.append(lead + "；".join(snippets) + "。此项与判断页的本田跨周期对比不是同一套数值。")
    bits.append("黄色轮廓标出本田范围，浅黄色覆盖为同一天影像中识别出的周边田地，不是项目登记地块。作物类型未按本田已关联记录确认，不作同作物排名。")
    contrast = None
    if contrast_dest is not None and series:
        contrast = render_greenness_contrast(series, contrast_dest)
    return {
        "text": bits,
        "image": str(image) if image else None,
        "contrast_image": str(contrast) if contrast else None,
        "date": date,
        "stats": stats,
        "by_date": {d: (block.get("stats") or block) for d, block in series.items()},
        "cover_1km": classified["cover_1km"],
        "cover_2km": classified["cover_2km"],
        "judgment": bits[0] if bits else "",
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
