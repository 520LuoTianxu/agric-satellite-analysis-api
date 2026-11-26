"""Within-field patterns that reuse pixels already loaded for the biweekly PDF.

三件事都只读本期事实和已经拿到的像元产品，不访问数据库、对象存储或新的采集接口：

1. 持续偏弱：同一坐标上的有效像元，在不少于三期里每一期都低于该期可比样本的
   箱线图离群界限。界限随当期样本计算，不用固定的生物学阈值。云、缺测和田块
   边界像元先剔除；配准不上或样本不够就写不足，不把两期都偏低说成持续。
2. 均匀度：用第10到第90百分位的跨度比较最早和最近的有效观测。跨度变化小于
   0.02（报告只印两位小数）或小于原跨度的 15% 时，只记为接近。不评价条带、
   设施或管理。
3. 历史同期：只读事实里已经附上的 ``historical_same_period``。当前出报接口
   不会查询往年景，上一期和周边地块也不能当作往年。没有这份归档，或作物、
   播期、样本不可比时，写明暂不评价，不编造数值或产量。

归档契约（由已经持有数据的调用方放入事实，本模块不主动拉取）::

    facts["historical_same_period"] = [
        {
            "land_id": "<与本期相同>",
            "year": 2024,
            "crop_type": "冬小麦",
            "sowing_date": "2024-10-08",
            "observations": [
                {
                    "date": "2024-05-18",
                    "metrics": {
                        "ndre": {"median": 0.62, "n_valid": 400, "statistic": "median"}
                    },
                }
            ],
        }
    ]
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

from agric_satellite_analysis_common.biweekly.metric_specs import METRIC_SPECS, metric_abbr
from app.reports.biweekly.metric_stats import (
    MIN_CLUSTER_PIXELS,
    pixel_spacing_m,
    pixel_table,
    region_labels,
    region_shares,
    to_local_meters,
    tukey_low_mask,
)
from app.reports.biweekly.report_text import cn_md, fmt_index, fmt_signed

# 少于三期无法把「每次都偏低」和「只偏低一次」分开。
MIN_PERSISTENT_DATES = 3
# 配准后的共同有效像元、以及再扣除边界后的像元，少于此数就不描述位置。
MIN_SUPPORTED_PIXELS = 30
# 最近一期有效像元里，各期都能配上的比例低于此值，视为配准或云掩膜支持不足。
MIN_PAIR_FRACTION = 0.5
# 8 邻域里至少 5 个也在可比样本中，才算田块内部；外圈留给边界混合像元。
MIN_INTERIOR_NEIGHBORS = 5
# 均匀度：有效像元少于此数只报告样本少，不判断趋同或分化。
MIN_UNIFORM_PIXELS = 30
# 跨度变化要同时超过两位小数的打印精度，和原跨度的这个比例，才称趋同或分化。
UNIFORM_ABS_FLOOR = 0.02
UNIFORM_REL_FLOOR = 0.15
# 往年至少比本期开始早这么多天。14 天或一个生长季内的上一次观测不是历史同期。
MIN_HISTORY_GAP_DAYS = 300
# 同月附近：月-日相差超过这个天数，不叫同期。
MAX_DOY_GAP_DAYS = 21
# 播期相差超过这个天数，不把两年的指数直接拿来分高低。
MAX_SOWING_GAP_DAYS = 21
ARCHIVE_KEY = "historical_same_period"
HISTORY_ABSENT = "历史可比数据不足，暂不评价。"


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _parse_day(value: Any) -> date | None:
    text = _day(value)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _abbr(metric: str) -> str:
    spec = METRIC_SPECS.get(metric)
    return spec.abbr if spec is not None else metric_abbr(metric)


def _metric_name(metric: str) -> str:
    """Printed pattern lines use the same index abbreviation as the rest of the report."""
    return _abbr(metric)


def _coord_key(lon: float, lat: float) -> tuple[float, float]:
    return (round(float(lon), 6), round(float(lat), 6))


def _dated_tables(products: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    """One usable pixel table per day. A later product for the same day replaces the earlier one."""
    by_day: dict[str, dict[str, Any]] = {}
    for product in products or []:
        if not isinstance(product, dict):
            continue
        day = _day(product.get("date"))
        if not day:
            continue
        table = pixel_table(product, metric)
        if table is None or int(table["valid"].sum()) < MIN_SUPPORTED_PIXELS:
            continue
        by_day[day] = {"date": day, "table": table}
    return [by_day[day] for day in sorted(by_day)]


def _lookup(table: dict[str, np.ndarray]) -> dict[tuple[float, float], int]:
    index: dict[tuple[float, float], int] = {}
    valid = table["valid"]
    for i, (lon, lat) in enumerate(zip(table["lon"], table["lat"])):
        if valid[i]:
            index[_coord_key(lon, lat)] = i
    return index


def _supported_mask(rows: list[dict[str, Any]]) -> np.ndarray:
    """Pixels that are valid at the same rounded coordinate on every date."""
    ref = rows[-1]["table"]
    others = [_lookup(row["table"]) for row in rows[:-1]]
    mask = np.array(ref["valid"], copy=True)
    for i, (lon, lat) in enumerate(zip(ref["lon"], ref["lat"])):
        if not mask[i]:
            continue
        key = _coord_key(lon, lat)
        if any(key not in index for index in others):
            mask[i] = False
    return mask


def _grid_cells(
    xs: np.ndarray, ys: np.ndarray, mask: np.ndarray, spacing: float
) -> dict[tuple[int, int], list[int]]:
    """Bin pixels on a grid anchored at the field, not at the map origin.

    绝对坐标直接除以间距再四舍五入时，间距落在半格上会把相邻像元并进同一格，
    整块田会被当成没有邻域。从可比像元的最小坐标起算，一格对应一个像元。
    """
    hits = np.flatnonzero(mask)
    cells: dict[tuple[int, int], list[int]] = {}
    if hits.size == 0:
        return cells
    step = max(float(spacing), 1e-6)
    origin_x = float(np.min(xs[hits]))
    origin_y = float(np.min(ys[hits]))
    for i in hits:
        cell = (
            int(np.rint((float(xs[i]) - origin_x) / step)),
            int(np.rint((float(ys[i]) - origin_y) / step)),
        )
        cells.setdefault(cell, []).append(int(i))
    return cells


def _component_sizes(cells: dict[tuple[int, int], list[int]], n: int) -> np.ndarray:
    """8-connected component size for every pixel that occupies a cell."""
    sizes = np.zeros(n, dtype=int)
    seen: set[tuple[int, int]] = set()
    for start in cells:
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        members: list[int] = []
        while stack:
            cell = stack.pop()
            members.extend(cells[cell])
            cx, cy = cell
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nxt = (cx + dx, cy + dy)
                    if nxt in cells and nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
        for i in members:
            sizes[i] = len(members)
    return sizes


def _interior_mask(xs: np.ndarray, ys: np.ndarray, supported: np.ndarray, spacing: float) -> np.ndarray:
    """Drop the field edge. Mixed pixels along the boundary are not called weak areas."""
    interior = np.zeros(supported.shape, dtype=bool)
    cells = _grid_cells(xs, ys, supported, spacing)
    if not cells:
        return interior
    for cell, members in cells.items():
        neighbors = -len(members)  # 自身所在格不算邻域
        cx, cy = cell
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbors += len(cells.get((cx + dx, cy + dy), ()))
        if neighbors >= MIN_INTERIOR_NEIGHBORS:
            for i in members:
                interior[i] = True
    return interior


def _aligned_values(rows: list[dict[str, Any]], interior: np.ndarray) -> list[np.ndarray]:
    """Each date's values on the reference pixels. Non-interior stays NaN."""
    ref = rows[-1]["table"]
    keys = [_coord_key(lon, lat) for lon, lat in zip(ref["lon"], ref["lat"])]
    out: list[np.ndarray] = []
    for row in rows:
        table = row["table"]
        index = _lookup(table)
        values = np.full(interior.shape, np.nan, dtype=float)
        if row is rows[-1]:
            values[interior] = table["value"][interior]
        else:
            for i in np.flatnonzero(interior):
                hit = index.get(keys[i])
                if hit is not None and table["valid"][hit]:
                    values[i] = table["value"][hit]
        out.append(values)
    return out


def _persistent_for_metric(products: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    rows = _dated_tables(products, metric)
    if len(rows) < MIN_PERSISTENT_DATES:
        return {
            "metric": metric,
            "status": "insufficient",
            "dates": [row["date"] for row in rows],
            "line": (
                f"持续偏弱（{_metric_name(metric)}）：能对上的观测只有{len(rows)}期，少于{MIN_PERSISTENT_DATES}期，"
                "分不清是一直偏弱，还是只弱了一期。"
            ),
        }
    supported = _supported_mask(rows)
    n_supported = int(supported.sum())
    latest_valid = int(rows[-1]["table"]["valid"].sum())
    pair_fraction = n_supported / latest_valid if latest_valid else 0.0
    if n_supported < MIN_SUPPORTED_PIXELS or pair_fraction < MIN_PAIR_FRACTION:
        return {
            "metric": metric,
            "status": "insufficient",
            "dates": [row["date"] for row in rows],
            "n_supported": n_supported,
            "pair_fraction": pair_fraction,
            "line": (
                f"持续偏弱（{_metric_name(metric)}）：这几期对不上同一块地方，"
                "分不清是一直偏弱，还是只弱了一期。"
            ),
        }
    ref = rows[-1]["table"]
    xs, ys = to_local_meters(ref["lon"], ref["lat"])
    spacing = pixel_spacing_m(xs[supported], ys[supported])
    interior = _interior_mask(xs, ys, supported, spacing)
    n_interior = int(interior.sum())
    n_boundary = n_supported - n_interior
    if n_interior < MIN_SUPPORTED_PIXELS:
        return {
            "metric": metric,
            "status": "insufficient",
            "dates": [row["date"] for row in rows],
            "n_supported": n_supported,
            "n_interior": n_interior,
            "line": (
                f"持续偏弱（{_metric_name(metric)}）：能用的范围太小，看不出哪里一直偏弱。"
            ),
        }
    aligned = _aligned_values(rows, interior)
    low = [tukey_low_mask(values) & interior for values in aligned]
    stacked = np.stack(low, axis=0)
    persistent = np.all(stacked, axis=0)
    occasional = np.any(stacked, axis=0) & ~persistent
    n_persistent = int(persistent.sum())
    n_occasional = int(occasional.sum())
    clustered = persistent & (
        _component_sizes(_grid_cells(xs, ys, persistent, spacing), persistent.size) >= MIN_CLUSTER_PIXELS
    )
    n_cluster = int(clustered.sum())
    regions = [
        {"name": name, "share": share}
        for name, share in region_shares(region_labels(xs, ys), clustered)[:2]
    ]
    dates = "、".join(cn_md(row["date"]) for row in rows)
    where = "、".join(item["name"] for item in regions)
    if n_cluster:
        # 只点出方位。巡查套话不写进这句，避免结论把同一句话再抄一遍。
        place = f"{where}有一小片一直比整块地弱" if where else "有一小片一直比整块地弱"
    elif n_persistent:
        place = "有些地方连续几期都偏弱，但没有连成一片，先不用专门去看"
    else:
        place = "没有哪一块连续这几期都比整块地弱"
    occasional = "另外有些地方只是某一期偏低，不算持续偏弱。" if n_occasional else ""
    line = f"持续偏弱（{_metric_name(metric)}）：{dates}这几期，{place}。{occasional}"
    return {
        "metric": metric,
        "status": "ok",
        "dates": [row["date"] for row in rows],
        "n_supported": n_supported,
        "n_interior": n_interior,
        "n_boundary": n_boundary,
        "n_persistent": n_persistent,
        "n_occasional": n_occasional,
        "n_persistent_clustered": n_cluster,
        "regions": regions,
        "line": line,
    }


def _spread(entry: dict[str, Any]) -> float | None:
    if entry.get("status") != "ok":
        return None
    p10, p90 = entry.get("p10"), entry.get("p90")
    if p10 is None or p90 is None:
        return None
    return float(p90) - float(p10)


def _uniformity_for_metric(series: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    name = _metric_name(metric)
    usable = []
    for row in series:
        entry = (row.get("metrics") or {}).get(metric) or {}
        spread = _spread(entry)
        if spread is None:
            continue
        usable.append(
            {
                "date": row["date"],
                "spread": spread,
                "n_valid": int(entry.get("n_valid") or 0),
                "p10": float(entry["p10"]),
                "p90": float(entry["p90"]),
            }
        )
    if len(usable) < 2:
        return {
            "metric": metric,
            "status": "insufficient",
            "line": f"均匀度（{name}）：没有两期可以比较，暂不评价趋同或分化。",
        }
    first, last = usable[0], usable[-1]
    if first["n_valid"] < MIN_UNIFORM_PIXELS or last["n_valid"] < MIN_UNIFORM_PIXELS:
        return {
            "metric": metric,
            "status": "low_sample",
            "dates": [first["date"], last["date"]],
            "n_valid": [first["n_valid"], last["n_valid"]],
            "spread": [first["spread"], last["spread"]],
            "line": (
                f"均匀度（{name}）：能用的观测太少，样本少，不判断地里是变齐了还是更散了。"
            ),
        }
    delta = last["spread"] - first["spread"]
    floor = max(UNIFORM_ABS_FLOOR, UNIFORM_REL_FLOOR * abs(first["spread"]))
    if abs(delta) < floor:
        direction = "similar"
        change = "地里各处差不多，看不出明显一块好、一块差"
    elif delta < 0:
        direction = "narrower"
        change = "地里更齐了，各处更趋同"
    else:
        direction = "wider"
        change = "地里更散了，各处更分化"
    line = (
        f"均匀度（{name}）：{cn_md(first['date'])}到{cn_md(last['date'])}，{change}。"
        "这只说明地里齐不齐。"
    )
    return {
        "metric": metric,
        "status": "ok",
        "direction": direction,
        "dates": [first["date"], last["date"]],
        "spread": [first["spread"], last["spread"]],
        "n_valid": [first["n_valid"], last["n_valid"]],
        "line": line,
    }


def _crop_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "小麦" in text or "wheat" in text:
        return "wheat"
    if "玉米" in text or "maize" in text or "corn" in text:
        return "maize"
    return text


def _doy_gap(left: date, right: date) -> int:
    def shifted(day: date) -> date:
        try:
            return date(2001, day.month, day.day)
        except ValueError:
            return date(2001, day.month, 28)

    gap = abs((shifted(left) - shifted(right)).days)
    return min(gap, 365 - gap)


def _current_crop(facts: dict[str, Any]) -> str:
    return _crop_key(facts.get("crop_type") or facts.get("crop_name"))


def _metric_point(obs: dict[str, Any], metric: str) -> dict[str, Any] | None:
    metrics = obs.get("metrics") or {}
    entry = metrics.get(metric)
    if not isinstance(entry, dict):
        return None
    statistic = entry.get("statistic") or ("median" if entry.get("median") is not None else None)
    if statistic == "median":
        value = entry.get("median")
    elif statistic == "mean":
        value = entry.get("field_mean")
        if value is None:
            value = entry.get("mean")
    else:
        return None
    if value is None:
        return None
    try:
        number = float(value)
        n_valid = entry.get("n_valid")
        n_valid = None if n_valid is None else int(n_valid)
    except (TypeError, ValueError):
        return None
    return {"value": number, "statistic": statistic, "n_valid": n_valid, "date": _day(obs.get("date"))}


def _history_line(facts: dict[str, Any]) -> dict[str, Any]:
    """Same-field prior-year archive only. Never promotes the previous period."""
    archive = facts.get(ARCHIVE_KEY)
    absent = {"status": "absent", "line": HISTORY_ABSENT, "comparisons": []}
    if not isinstance(archive, list) or not archive:
        return absent
    land_id = str(facts.get("land_id") or "")
    if not land_id:
        return absent
    period_start = _parse_day((facts.get("period") or {}).get("start"))
    latest_day = _parse_day((facts.get("comparison") or {}).get("latest_date"))
    if period_start is None or latest_day is None:
        return absent
    current_crop = _current_crop(facts)
    current_sowing = _parse_day(facts.get("sowing_date"))
    plan = facts.get("report_metrics") or {}
    metrics = [m for m in (plan.get("primary") or []) if m in METRIC_SPECS]
    if not metrics:
        metrics = ["ndvi"]
    current_stats = (facts.get("comparison") or {}).get("metrics") or {}
    comparisons: list[dict[str, Any]] = []
    rejected_reason = ""
    for entry in archive:
        if not isinstance(entry, dict) or str(entry.get("land_id") or "") != land_id:
            rejected_reason = "land"
            continue
        archive_crop = _crop_key(entry.get("crop_type") or entry.get("crop_name"))
        archive_sowing = _parse_day(entry.get("sowing_date"))
        for obs in entry.get("observations") or []:
            if not isinstance(obs, dict):
                continue
            obs_day = _parse_day(obs.get("date"))
            if obs_day is None:
                continue
            if (period_start - obs_day).days < MIN_HISTORY_GAP_DAYS:
                rejected_reason = rejected_reason or "recent"
                continue
            if _doy_gap(obs_day, latest_day) > MAX_DOY_GAP_DAYS:
                rejected_reason = rejected_reason or "season"
                continue
            for metric in metrics:
                point = _metric_point(obs, metric)
                current = current_stats.get(metric) or {}
                if point is None or current.get("latest") is None:
                    continue
                comparisons.append(
                    {
                        "metric": metric,
                        "year": obs_day.year,
                        "date": point["date"],
                        "value": point["value"],
                        "statistic": point["statistic"],
                        "n_valid": point["n_valid"],
                        "current": float(current["latest"]),
                        "current_statistic": current.get("statistic"),
                        "crop": archive_crop,
                        "sowing": archive_sowing,
                    }
                )
    if not comparisons:
        if rejected_reason == "recent":
            line = "所附日期距本期不足一年，不能把上一期当作往年。历史可比数据不足，暂不评价。"
        else:
            line = HISTORY_ABSENT
        return {"status": "absent", "line": line, "comparisons": []}

    # 同一指标只留年份最近、月-日也最接近的一条，避免用更远的日期充数。
    best: dict[str, dict[str, Any]] = {}
    for item in comparisons:
        item["doy_gap"] = _doy_gap(_parse_day(item["date"]) or latest_day, latest_day)
        held = best.get(item["metric"])
        if held is None or item["year"] > held["year"] or (
            item["year"] == held["year"] and item["doy_gap"] < held["doy_gap"]
        ):
            best[item["metric"]] = item
    chosen = [best[metric] for metric in metrics if metric in best]
    if not current_crop or any(not item["crop"] for item in chosen):
        return {
            "status": "limited",
            "reason": "crop_unknown",
            "line": "历史归档或本期缺少作物信息，轮作无法核对，暂不评价。",
            "comparisons": chosen,
        }
    if any(item["crop"] != current_crop for item in chosen):
        return {
            "status": "limited",
            "reason": "crop_mismatch",
            "line": "历史作物与本期不一致，不能当作同一比较，暂不评价。",
            "comparisons": chosen,
        }
    usable = []
    blocked: list[str] = []
    for item in chosen:
        if item["current_statistic"] != item["statistic"]:
            blocked.append("statistic")
            continue
        if item["n_valid"] is None or item["n_valid"] < MIN_UNIFORM_PIXELS:
            blocked.append("sample")
            continue
        usable.append(item)
    if not usable:
        if "sample" in blocked:
            line = "历史同期归档的有效像元不足，样本少，暂不评价。"
        else:
            line = "历史同期与本期的统计量不一致，暂不评价。"
        return {"status": "limited", "reason": blocked[0] if blocked else "unusable", "line": line, "comparisons": chosen}
    if current_sowing is None or any(item["sowing"] is None for item in usable):
        listed = "；".join(
            f"{item['year']}年{cn_md(item['date'])}{_abbr(item['metric'])}{fmt_index(item['value'])}"
            for item in usable
        )
        return {
            "status": "limited",
            "reason": "sowing_unknown",
            "line": f"归档有{listed}。播期未知，不能排除播期不同造成的生育阶段差异，暂不评价。",
            "comparisons": usable,
        }
    # 播期比的是年内月日，不是两个年份的绝对间隔。10月6日和上一年10月10日只差4天。
    sowing_gaps = [_doy_gap(current_sowing, item["sowing"]) for item in usable]
    if any(gap > MAX_SOWING_GAP_DAYS for gap in sowing_gaps):
        return {
            "status": "limited",
            "reason": "sowing_gap",
            "line": f"历史播期与本期相差超过{MAX_SOWING_GAP_DAYS}天，暂不评价。",
            "comparisons": usable,
        }
    parts = []
    for item in usable:
        delta = round(round(item["current"], 2) - round(item["value"], 2), 2)
        stat = "中位数" if item["statistic"] == "median" else "均值"
        parts.append(
            f"与{item['year']}年{cn_md(item['date'])}同田{_abbr(item['metric'])}{stat}"
            f"{fmt_index(item['value'])}相比，本期{fmt_index(item['current'])}，相差{fmt_signed(delta)}"
        )
    line = "；".join(parts) + "。同田归档对照，不是产量。"
    return {"status": "compared", "line": line, "comparisons": usable}


def assess_field_patterns(facts: dict[str, Any], products: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Persistent-weak, uniformity and historical lines for the report page that already exists."""
    plan = facts.get("report_metrics") or {}
    primary = [metric for metric in (plan.get("primary") or []) if metric in METRIC_SPECS]
    if not primary:
        primary = ["ndvi"]
    series = [row for row in (facts.get("metric_series") or []) if isinstance(row, dict)]
    persistent = [_persistent_for_metric(list(products or []), metric) for metric in primary]
    uniformity = [_uniformity_for_metric(series, metric) for metric in primary]
    history = _history_line(facts)
    return {
        "persistent": persistent,
        "uniformity": uniformity,
        "history": history,
        "persistent_lines": [item["line"] for item in persistent],
        "uniformity_lines": [item["line"] for item in uniformity],
        "history_line": history["line"],
    }
