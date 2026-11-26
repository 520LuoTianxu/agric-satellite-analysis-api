"""Per-date field statistics from parcel pixel products, for the biweekly PDF.

只做描述统计，不做诊断：
- 有效像元 = 数值有限、落在指数有效范围内、且未被像元级云掩膜（clear=0）标为云或云影；
- 田块水平用中位数和 P10–P90 表示，不用最小/最大值，避免单个像元极值支配结论；
- 有效像元不足 MIN_FIELD_PIXELS 时不出统计，返回缺测（None），不以 0 代替；
- 「显著偏低」采用箱线图离群值规则（低于 Q1 − 1.5×IQR），是统计口径，不是农学阈值。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from agric_satellite_analysis_common.biweekly.metric_specs import METRIC_SPECS

# 与既有田内图逻辑一致：少于 10 个可用像元时不成图、不出统计。
MIN_FIELD_PIXELS = 10
TUKEY_K = 1.5
# 成片的最小规模：4 个相邻像元（约 2×2 像元、400 平方米）。哨兵二号的几何定位误差约为一个像元，
# 孤立的单个像元无法在田间可靠定位，也常来自田埂、树木等混合像元，因此不作为核查位置。
MIN_CLUSTER_PIXELS = 4


def columns_of(product: dict[str, Any]) -> dict[str, list[Any]]:
    return dict((product.get("pixel_data") or {}).get("columns") or {})


def metric_column_key(columns: dict[str, list[Any]], metric: str) -> str | None:
    spec = METRIC_SPECS.get(metric)
    candidates = [metric.upper(), metric.lower()]
    if spec is not None:
        candidates.insert(0, spec.abbr)
    # 产品列名并不全是大写（如 CIre），逐个按不区分大小写匹配。
    lowered = {str(key).lower(): key for key in columns}
    for candidate in candidates:
        if candidate in columns:
            return candidate
        hit = lowered.get(candidate.lower())
        if hit is not None:
            return hit
    return None


def _float_column(values: list[Any] | None, n: int) -> np.ndarray:
    out = np.full(n, np.nan, dtype=float)
    if not values:
        return out
    for i, value in enumerate(values[:n]):
        if value is None or isinstance(value, bool):
            continue
        try:
            out[i] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def pixel_table(product: dict[str, Any], metric: str) -> dict[str, np.ndarray] | None:
    """Coordinates, values and validity for one metric on one date.

    Returns None when the product has no coordinates or no column for the metric.
    ``valid`` marks pixels that may enter statistics; ``cloud`` marks pixels the
    per-pixel mask flagged as cloud/shadow; the remaining invalid ones are
    missing or out-of-range values.
    """
    columns = columns_of(product)
    lons = columns.get("lon") or []
    lats = columns.get("lat") or []
    n = min(len(lons), len(lats))
    key = metric_column_key(columns, metric)
    if n == 0 or key is None:
        return None
    lon = _float_column(lons, n)
    lat = _float_column(lats, n)
    values = _float_column(columns.get(key), n)
    located = np.isfinite(lon) & np.isfinite(lat)
    spec = METRIC_SPECS.get(metric)
    low, high = spec.valid_range if spec else (-np.inf, np.inf)
    in_range = np.isfinite(values) & (values >= low) & (values <= high)
    cloud = np.zeros(n, dtype=bool)
    clear_col = columns.get("clear")
    if clear_col:
        clear = _float_column(clear_col, n)
        # clear=0 是已知的云或云影；clear 缺测表示没有像元级分类，不当作云处理。
        cloud = np.isfinite(clear) & (clear == 0)
    valid = located & in_range & ~cloud
    return {
        "lon": lon[located],
        "lat": lat[located],
        "value": np.where(valid, values, np.nan)[located],
        "valid": valid[located],
        "cloud": cloud[located],
    }


def summarize_values(values: np.ndarray, *, low_reference: float | None = None) -> dict[str, Any] | None:
    """Robust summary of valid values, or None when too few to describe a field."""
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size < MIN_FIELD_PIXELS:
        return None
    q1, median, q3 = (float(v) for v in np.percentile(finite, [25, 50, 75]))
    p10, p90 = (float(v) for v in np.percentile(finite, [10, 90]))
    out: dict[str, Any] = {
        "n_valid": int(finite.size),
        "median": median,
        "mean": float(np.mean(finite)),
        "p10": p10,
        "p25": q1,
        "p75": q3,
        "p90": p90,
        "iqr": q3 - q1,
    }
    if low_reference is not None:
        out["below_low_reference_fraction"] = float(np.mean(finite < low_reference))
    return out


def field_stats(product: dict[str, Any], metric: str) -> dict[str, Any]:
    """Statistics for one metric on one date. Always reports pixel counts."""
    table = pixel_table(product, metric)
    if table is None:
        return {"status": "missing_metric", "n_total": 0, "n_valid": 0, "valid_fraction": None}
    n_total = int(table["valid"].size)
    n_valid = int(table["valid"].sum())
    base = {
        "n_total": n_total,
        "n_valid": n_valid,
        "n_cloud": int(table["cloud"].sum()),
        "valid_fraction": (n_valid / n_total) if n_total else None,
    }
    spec = METRIC_SPECS.get(metric)
    summary = summarize_values(
        table["value"][table["valid"]],
        low_reference=spec.low_reference if spec else None,
    )
    if summary is None:
        # 整景被云掩膜或数值全部无效时，只报告像元数，统计量保持缺测。
        return {"status": "insufficient_valid_pixels", **base}
    return {"status": "ok", **base, **summary}


def tukey_low_mask(values: np.ndarray) -> np.ndarray:
    """Pixels below Q1 − 1.5×IQR. Constant fields and tiny samples flag nothing."""
    data = np.asarray(values, dtype=float)
    finite = np.isfinite(data)
    flagged = np.zeros(data.shape, dtype=bool)
    if int(finite.sum()) < MIN_FIELD_PIXELS:
        return flagged
    q1, q3 = np.percentile(data[finite], [25, 75])
    iqr = float(q3 - q1)
    if iqr <= 0:
        return flagged
    flagged[finite] = data[finite] < (q1 - TUKEY_K * iqr)
    return flagged


def cluster_sizes(xs: np.ndarray, ys: np.ndarray, mask: np.ndarray, spacing: float) -> np.ndarray:
    """For each flagged pixel, the size of its 8-connected group; 0 elsewhere."""
    sizes = np.zeros(mask.shape, dtype=int)
    hits = np.flatnonzero(mask)
    if hits.size == 0:
        return sizes
    step = max(float(spacing), 1e-6)
    cells = {
        (int(round(float(xs[i]) / step)), int(round(float(ys[i]) / step))): int(i) for i in hits
    }
    seen: set[tuple[int, int]] = set()
    for start in cells:
        if start in seen:
            continue
        group = [start]
        seen.add(start)
        cursor = 0
        while cursor < len(group):
            cx, cy = group[cursor]
            cursor += 1
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nxt = (cx + dx, cy + dy)
                    if nxt in cells and nxt not in seen:
                        seen.add(nxt)
                        group.append(nxt)
        for cell in group:
            sizes[cells[cell]] = len(group)
    return sizes


def to_local_meters(lons: np.ndarray, lats: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """East/north metres around the pixel centroid (north is up on the maps)."""
    lon0 = float(np.mean(lons))
    lat0 = float(np.mean(lats))
    try:
        from pyproj import Transformer

        transformer = Transformer.from_crs(
            "EPSG:4326",
            f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m +no_defs",
            always_xy=True,
        )
        xs, ys = transformer.transform(lons, lats)
        return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
    except Exception:  # noqa: BLE001 — 没有 pyproj 时按球面近似换算
        xs = (lons - lon0) * 111000.0 * max(0.2, abs(np.cos(np.radians(lat0))))
        ys = (lats - lat0) * 111000.0
        return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def pixel_spacing_m(xs: np.ndarray, ys: np.ndarray) -> float:
    """Median nearest-neighbour distance, i.e. the analysis grid spacing."""
    n = int(xs.size)
    if n < 2:
        return 10.0
    # 大田块抽样估计即可，避免构造 n×n 距离矩阵。
    step = max(1, n // 400)
    sample = np.arange(0, n, step)
    dx = xs[sample, None] - xs[None, :]
    dy = ys[sample, None] - ys[None, :]
    dist = np.hypot(dx, dy)
    dist[dist <= 1e-6] = np.inf
    nearest = dist.min(axis=1)
    nearest = nearest[np.isfinite(nearest)]
    if nearest.size == 0:
        return 10.0
    return float(np.median(nearest))


_REGION_NAMES = {
    ("北", "西"): "西北部",
    ("北", "中"): "北部",
    ("北", "东"): "东北部",
    ("中", "西"): "西部",
    ("中", "中"): "中部",
    ("中", "东"): "东部",
    ("南", "西"): "西南部",
    ("南", "中"): "南部",
    ("南", "东"): "东南部",
}


def region_labels(xs: np.ndarray, ys: np.ndarray) -> list[str]:
    """Nine-grid position of each pixel within the field's bounding box."""
    if xs.size == 0:
        return []
    xmin, xmax = float(xs.min()), float(xs.max())
    ymin, ymax = float(ys.min()), float(ys.max())
    fx = (xs - xmin) / max(xmax - xmin, 1e-6)
    fy = (ys - ymin) / max(ymax - ymin, 1e-6)
    out = []
    for a, b in zip(fx, fy):
        ew = "西" if a < 1 / 3 else ("东" if a > 2 / 3 else "中")
        ns = "南" if b < 1 / 3 else ("北" if b > 2 / 3 else "中")
        out.append(_REGION_NAMES[(ns, ew)])
    return out


# 方位中位数和全田中位数至少差这么多，才算这一方位分开。半个四分位距更大时用那个，
# 避免把噪声写成一块高、一块低。这不是出苗或收获阈值。
ZONE_SPLIT_ABS = 0.05
ZONE_SPLIT_IQR = 0.5
# 这一方位里能读到的像元不到一半时，中位数不能代表这一块是否出苗或已收获。
ZONE_UNREADABLE_FRACTION = 0.5


def describe_zones(
    xs: np.ndarray,
    ys: np.ndarray,
    values: np.ndarray,
    valid: np.ndarray,
    *,
    field_median: float | None,
    iqr: float | None,
    delta: np.ndarray | None = None,
) -> dict[str, Any]:
    """Where the field splits, using positions already drawn on the in-field map.

    Only zones that sit clearly above or below the field median, or that are
    mostly unreadable, are returned. An even field comes back with no zones.
    """
    labels = region_labels(xs, ys)
    n_pixels = len(labels)
    if n_pixels == 0:
        return {"spatial_split": "even", "zones": []}
    valid_mask = np.asarray(valid, dtype=bool)
    readings = np.asarray(values, dtype=float)
    groups: dict[str, list[int]] = {}
    for index, name in enumerate(labels):
        groups.setdefault(name, []).append(index)
    try:
        median = None if field_median is None else float(field_median)
    except (TypeError, ValueError):
        median = None
    if median is not None and not np.isfinite(median):
        median = None
    try:
        spread = 0.0 if iqr is None else abs(float(iqr))
    except (TypeError, ValueError):
        spread = 0.0
    if not np.isfinite(spread):
        spread = 0.0
    gap = max(ZONE_SPLIT_ABS, ZONE_SPLIT_IQR * spread)
    zones: list[dict[str, Any]] = []
    for name, members in groups.items():
        idx = np.asarray(members, dtype=int)
        n_total = int(idx.size)
        if n_total < MIN_CLUSTER_PIXELS:
            continue
        zone_valid = valid_mask[idx]
        n_valid = int(zone_valid.sum())
        readable = n_valid / n_total
        entry: dict[str, Any] = {
            "name": name,
            "level": "unreadable" if readable < ZONE_UNREADABLE_FRACTION else "similar",
            "valid_fraction": readable,
            "area_fraction": n_total / n_pixels,
        }
        if n_valid >= MIN_CLUSTER_PIXELS:
            zone_median = float(np.median(readings[idx][zone_valid]))
            entry["median"] = zone_median
            if entry["level"] != "unreadable" and median is not None:
                if zone_median < median - gap:
                    entry["level"] = "lower"
                elif zone_median > median + gap:
                    entry["level"] = "higher"
        if delta is not None:
            paired = np.asarray(delta, dtype=float)[idx]
            paired = paired[np.isfinite(paired)]
            if paired.size >= MIN_CLUSTER_PIXELS:
                entry["delta_median"] = float(np.median(paired))
        if entry["level"] != "similar":
            zones.append(entry)
    levels = {item["level"] for item in zones}
    if levels & {"lower", "higher"}:
        split = "split"
    elif "unreadable" in levels:
        split = "unreadable"
    else:
        split = "even"
    order = {"lower": 0, "unreadable": 1, "higher": 2}
    zones.sort(key=lambda item: (order.get(str(item["level"]), 9), -float(item["area_fraction"])))
    return {"spatial_split": split, "zones": zones}


def region_shares(labels: list[str], mask: np.ndarray) -> list[tuple[str, float]]:
    """Where flagged pixels sit: (region, share of flagged pixels), largest first."""
    flagged = [label for label, hit in zip(labels, mask) if hit]
    if not flagged:
        return []
    counts: dict[str, int] = {}
    for label in flagged:
        counts[label] = counts.get(label, 0) + 1
    total = len(flagged)
    return sorted(((name, count / total) for name, count in counts.items()), key=lambda item: -item[1])


def match_pixels(
    current: dict[str, np.ndarray], earlier: dict[str, np.ndarray]
) -> np.ndarray:
    """Earlier values aligned onto the current pixels; NaN where there is no pair."""
    index = {
        (round(float(lon), 6), round(float(lat), 6)): i
        for i, (lon, lat) in enumerate(zip(earlier["lon"], earlier["lat"]))
    }
    aligned = np.full(current["lon"].shape, np.nan, dtype=float)
    for i, (lon, lat) in enumerate(zip(current["lon"], current["lat"])):
        hit = index.get((round(float(lon), 6), round(float(lat), 6)))
        if hit is not None and earlier["valid"][hit]:
            aligned[i] = earlier["value"][hit]
    return aligned


def change_stats(delta: np.ndarray) -> dict[str, Any] | None:
    """Summary of pixel-wise change (later minus earlier) over paired valid pixels."""
    finite = np.asarray(delta, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size < MIN_FIELD_PIXELS:
        return None
    p10, q1, median, q3, p90 = (float(v) for v in np.percentile(finite, [10, 25, 50, 75, 90]))
    return {
        "n_paired": int(finite.size),
        "median": median,
        "p10": p10,
        "p25": q1,
        "p75": q3,
        "p90": p90,
        "decrease_fraction": float(np.mean(finite < 0)),
        "increase_fraction": float(np.mean(finite > 0)),
    }
