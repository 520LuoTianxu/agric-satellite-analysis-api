"""Fill official biweekly facts with weather and context maps before render.

Called from the official generate entry used by the Celery generate_biweekly_report
task. Pulls period weather from existing Open-Meteo / NASA POWER helpers, and
builds in-field / 1–2 km imagery-farmland figures from parcel products or OSS.
Does not write a database, invent science values, or substitute another land.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from agric_satellite_analysis_common.biweekly.metric_specs import REPORT_METRIC_ORDER
from agric_satellite_analysis_common.biweekly.phase_policy import (
    FORBIDDEN_REPORT_METRICS,
    PHASE_METRICS,
    phase_metric_profile,
)
from app.reports.biweekly.context_maps import (
    CLOUD_LIMIT_PCT,
    _as_day,
    _columns,
    _in_period_products,
    bounds_from_field_pixels,
    build_surrounding_comparison,
    classify_imagery_farmland,
    fetch_processing_window_truecolor,
    is_eligible_optical,
    last_prior_period_products,
    resolve_stac_item_id,
)
from app.reports.biweekly.infield_maps import build_infield_metric
from app.reports.biweekly.metric_stats import field_stats
from app.reports.biweekly.weather_fetch import (
    apply_fetched_weather,
    fetch_period_weather,
    period_has_usable_weather,
    resolve_report_lat_lon,
)

_S2 = "S2"


def _period(facts: dict[str, Any]) -> dict[str, Any]:
    return dict(facts.get("period") or {})


def _end_inclusive(period: dict[str, Any]) -> str:
    explicit = _as_day(period.get("end_inclusive") or period.get("end"))
    if explicit:
        return explicit
    exclusive = _as_day(period.get("end_exclusive"))
    if exclusive:
        from datetime import date, timedelta

        return (date.fromisoformat(exclusive) - timedelta(days=1)).isoformat()
    return ""


def normalize_product(product: dict[str, Any]) -> dict[str, Any]:
    """Return the product with pixels as ``lonlat_columns_v1`` columns; values are not changed.

    出图和统计只读列式像元。历史产品可能仍是对象数组（lonlat_v1），从数据库导出的行里
    pixel_data 还可能是 JSON 字符串；这里统一转成列式，像元个数、顺序和数值都不变。
    不修改传入的对象。
    """
    from agric_satellite_analysis_common.lonlat_pixels import (
        migrate_product_to_columnar,
        needs_columnar_migration,
    )

    out = product
    pixel_data = out.get("pixel_data")
    if isinstance(pixel_data, str):
        try:
            parsed = json.loads(pixel_data)
        except json.JSONDecodeError:
            parsed = None
        out = {**out, "pixel_data": parsed if isinstance(parsed, dict) else None}
    elif pixel_data is not None and not isinstance(pixel_data, dict):
        out = {**out, "pixel_data": None}
    if needs_columnar_migration(out):
        out = dict(out)
        if isinstance(out.get("pixel_data"), dict):
            out["pixel_data"] = dict(out["pixel_data"])
        migrate_product_to_columnar(out)
    return out


def load_products_from_dir(products_dir: str | Path) -> list[dict[str, Any]]:
    directory = Path(products_dir)
    if not directory.is_dir():
        return []
    products: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("_") or path.name.endswith("_index.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and (
            payload.get("pixel_data") or payload.get("sensor") or payload.get("date")
        ):
            products.append(payload)
    return products


def load_products_from_oss(
    land_id: str,
    dates: Iterable[str],
    *,
    storage: Any | None = None,
) -> list[dict[str, Any]]:
    """Read already-produced parcel JSON from object storage. No DB writes."""
    try:
        from agric_satellite_analysis_common.storage import get_storage

        client = storage or get_storage()
    except Exception:  # noqa: BLE001 — missing storage must not abort official render
        return []
    products: list[dict[str, Any]] = []
    for day in dates:
        for sensor in ("S2", "S1"):
            key = f"s1s2_parcel/json/{land_id}/{day}_{sensor}.json"
            payload = None
            try:
                raw = client.get_bytes(key)
                payload = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
            except Exception:  # noqa: BLE001
                try:
                    tmp = Path("/tmp") / f"official_product_{land_id}_{day}_{sensor}.json"
                    client.download_file(key, str(tmp))
                    payload = json.loads(tmp.read_text(encoding="utf-8"))
                    tmp.unlink(missing_ok=True)
                except Exception:
                    payload = None
            if isinstance(payload, dict):
                products.append(payload)
    return products


def _land_id(facts: dict[str, Any]) -> str:
    return str(facts.get("land_id") or (facts.get("land") or {}).get("id") or "").strip()


def _collect_dates(facts: dict[str, Any], products: list[dict[str, Any]]) -> list[str]:
    days = set()
    for obs in facts.get("observations") or []:
        day = _as_day(obs.get("date") if isinstance(obs, dict) else None)
        if day:
            days.add(day)
    period = _period(facts)
    start, end_ex = _as_day(period.get("start")), _as_day(period.get("end_exclusive"))
    for product in products:
        day = _as_day(product.get("date"))
        if day:
            days.add(day)
    # Always include a little lookback so the in-field change panel can pair pixels.
    from datetime import date, timedelta

    if start:
        cursor = date.fromisoformat(start) - timedelta(days=28)
        end = date.fromisoformat(end_ex) if end_ex else date.fromisoformat(start)
        while cursor < end:
            days.add(cursor.isoformat())
            cursor += timedelta(days=1)
    return sorted(days)


def resolve_products(
    facts: dict[str, Any],
    *,
    products: list[dict[str, Any]] | None = None,
    products_dir: str | Path | None = None,
    storage: Any | None = None,
    offline: bool = False,
) -> list[dict[str, Any]]:
    if products is not None:
        return [normalize_product(item) for item in products]
    loaded: list[dict[str, Any]] = []
    if products_dir:
        loaded = load_products_from_dir(products_dir)
    if loaded or offline:
        # 离线生成只用调用方给出的产品，找不到也不回退到对象存储。
        return [normalize_product(item) for item in loaded]
    land_id = _land_id(facts)
    if not land_id:
        return []
    return [
        normalize_product(item)
        for item in load_products_from_oss(land_id, _collect_dates(facts, []), storage=storage)
    ]


def _field_points(
    products: list[dict[str, Any]],
    period: dict[str, Any],
    *,
    optical_days: list[str] | None = None,
) -> tuple[Any, Any] | None:
    import numpy as np

    current = _in_period_products(
        products,
        _as_day(period.get("start")),
        _as_day(period.get("end_exclusive")),
        optical_days=optical_days,
    )
    if not current:
        return None
    columns = _columns(current[-1])
    lons = [float(v) for v in (columns.get("lon") or []) if v is not None]
    lats = [float(v) for v in (columns.get("lat") or []) if v is not None]
    if len(lons) < 10 or len(lats) < 10:
        return None
    n = min(len(lons), len(lats))
    return np.asarray(lons[:n]), np.asarray(lats[:n])


def _processing_window_bounds(product: dict[str, Any]) -> tuple[float, float, float, float] | None:
    window = product.get("processing_window") or {}
    bounds = window.get("bounds") or window.get("bbox")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        return float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])
    return None


def _latest_in_period(
    products: list[dict[str, Any]],
    period: dict[str, Any],
    *,
    optical_days: list[str] | None = None,
) -> dict[str, Any] | None:
    current = _in_period_products(
        products,
        _as_day(period.get("start")),
        _as_day(period.get("end_exclusive")),
        optical_days=optical_days,
    )
    return current[-1] if current else None



def _observation_from_product(product: dict[str, Any]) -> dict[str, Any]:
    """Field-mean row for one product, in the same shape the API sends."""
    row = {
        "date": _as_day(product.get("date")),
        "sensor": str(product.get("sensor") or "S2"),
        "cloud_cover": product.get("cloud_cover"),
        "parcel_cloud_cover_pct": product.get("parcel_cloud_cover_pct"),
    }
    for metric in REPORT_METRIC_ORDER:
        value = product.get(f"{metric}_avg")
        if value is None:
            columns = _columns(product)
            key = next((k for k in (metric.upper(), metric) if k in columns), None)
            if key:
                vals = [float(v) for v in (columns.get(key) or []) if v is not None]
                value = (sum(vals) / len(vals)) if vals else None
        if value is not None:
            row[f"{metric}_avg"] = float(value)
    return row


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cloud_of(row: dict[str, Any]) -> tuple[float | None, str | None]:
    """Cloud percentage and which one it is: over the parcel, or the whole scene."""
    parcel = _float_or_none(row.get("parcel_cloud_cover_pct"))
    if parcel is not None:
        return parcel, "parcel"
    scene = _float_or_none(row.get("cloud_cover"))
    if scene is not None:
        return scene, "scene"
    return None, None


def report_metric_plan(phase: dict[str, Any] | None, latest_date: str | None = None) -> dict[str, Any]:
    """Which metrics this report leads with, and why.

    - 本期只落在一个阶段：用该阶段的主要/辅助指标；
    - 本期跨两个阶段：按最近一次有效观测所在阶段取指标，并在报告中说明；
    - 没有阶段代号但上游给了指标清单：照清单展示；
    - 没有种植方案或阶段不适用：四项指标都列出，田内图用 NDVI，不作阶段判断。
    """
    phase = phase or {}
    label = phase.get("display_phase") if phase.get("display_phase") in PHASE_METRICS else None
    basis = "phase" if label else None
    if label is None:
        stages = [
            row
            for row in (phase.get("observation_stages") or [])
            if row.get("phase_label") in PHASE_METRICS
        ]
        if latest_date:
            exact = [row for row in stages if _as_day(row.get("date")) == latest_date]
            stages = exact or stages
        if stages:
            label = stages[-1]["phase_label"]
            basis = "latest_observation_phase"
    if label is None:
        # 上游只给了指标清单、没给阶段代号（旧版事实或人工指定）：照清单展示，不补阶段解释。
        listed = [m for m in (phase.get("primary_metrics") or []) if m in REPORT_METRIC_ORDER]
        if listed:
            extra = [m for m in (phase.get("supporting_metrics") or []) if m in REPORT_METRIC_ORDER]
            supporting = [m for m in extra if m not in listed] or [
                m for m in REPORT_METRIC_ORDER if m not in listed
            ]
            return {
                "phase_label": None,
                "basis": "listed",
                "formal_name": None,
                "stage_span": None,
                "rationale": None,
                "primary": listed,
                "supporting": supporting,
            }
        return {
            "phase_label": None,
            "basis": "default",
            "formal_name": None,
            "stage_span": None,
            "rationale": None,
            "primary": ["ndvi"],
            "supporting": ["evi", "ndre", "ndmi"],
        }
    profile = phase_metric_profile(label)
    return {
        **profile,
        "basis": basis,
        "primary": [m for m in profile["primary"] if m not in FORBIDDEN_REPORT_METRICS],
        "supporting": [m for m in profile["supporting"] if m not in FORBIDDEN_REPORT_METRICS],
    }


def _stage_by_date(phase: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in ("prior_observation_stages", "observation_stages"):
        for row in phase.get(key) or []:
            day = _as_day(row.get("date"))
            if day:
                out[day] = row
    return out


def build_metric_series(
    facts: dict[str, Any],
    products: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Per-date values for every report metric, prior dates first.

    有像元产品的日期用晴空像元的稳健统计（中位数、P10–P90、有效像元占比）；
    没有像元产品的日期只有接口下发的田块均值。缺测保持 None，不写成 0。
    返回 (逐日序列, 日期 → 像元产品)。
    """
    period = _period(facts)
    start = _as_day(period.get("start"))
    end_exclusive = _as_day(period.get("end_exclusive"))
    optical_days = facts.get("optical_days")
    phase = facts.get("phase") or {}
    stages = _stage_by_date(phase)

    current = _in_period_products(products, start, end_exclusive, optical_days=optical_days)
    prior = last_prior_period_products(products, start, optical_days=optical_days) if start else []
    by_date: dict[str, dict[str, Any]] = {}
    for product in prior + current:
        if _columns(product):
            by_date[_as_day(product.get("date"))] = product

    fact_rows: dict[str, dict[str, Any]] = {}
    for key in ("prior_observations", "observations"):
        for row in facts.get(key) or []:
            if isinstance(row, dict) and _as_day(row.get("date")):
                fact_rows[_as_day(row.get("date"))] = row
    for product in prior + current:
        fact_rows.setdefault(_as_day(product.get("date")), _observation_from_product(product))

    series: list[dict[str, Any]] = []
    for day in sorted(fact_rows):
        row = fact_rows[day]
        product = by_date.get(day)
        cloud, cloud_basis = _cloud_of(product or row)
        if cloud is None:
            cloud, cloud_basis = _cloud_of(row)
        metrics: dict[str, dict[str, Any]] = {}
        for metric in REPORT_METRIC_ORDER:
            mean = _float_or_none(row.get(f"{metric}_avg"))
            if mean is None and product is not None:
                mean = _float_or_none(product.get(f"{metric}_avg"))
            entry: dict[str, Any] = {"status": "missing", "field_mean": mean}
            if product is not None:
                stats = field_stats(product, metric)
                entry.update({k: v for k, v in stats.items() if k != "status"})
                entry["status"] = stats["status"]
            elif mean is not None:
                entry["status"] = "mean_only"
            metrics[metric] = entry
        stage = stages.get(day) or {}
        series.append(
            {
                "date": day,
                "in_period": bool(start and end_exclusive and start <= day < end_exclusive),
                "source": "pixels" if product is not None else "field_mean",
                "cloud_pct": cloud,
                "cloud_basis": cloud_basis,
                "stage_code": stage.get("stage_code"),
                "phase_label": stage.get("phase_label"),
                "metrics": metrics,
            }
        )
    return series, by_date


def series_statistic(series: list[dict[str, Any]], metric: str) -> str | None:
    """One statistic per metric across all dates, so points on a line are comparable.

    所有有数据的日期都有像元统计时用中位数；只要有一个日期只有田块均值，就全部用均值，
    不把中位数和均值画在同一条线上。
    """
    kinds = {row["metrics"][metric]["status"] for row in series}
    kinds -= {"missing", "missing_metric", "insufficient_valid_pixels"}
    if not kinds:
        return None
    return "median" if kinds == {"ok"} else "mean"


def series_value(row: dict[str, Any], metric: str, statistic: str | None) -> float | None:
    entry = row["metrics"][metric]
    if statistic == "median":
        return entry.get("median") if entry.get("status") == "ok" else None
    if statistic == "mean":
        return entry.get("field_mean")
    return None


def build_comparison(series: list[dict[str, Any]], metrics: list[str]) -> dict[str, Any] | None:
    """Latest in-period observation against the observation before it.

    全报告只用这一对日期：结论、证据表和田内变化图都相同。
    变化量为两次数值各保留两位小数后的差，差值为 0.00 记为持平。
    """
    statistics = {metric: series_statistic(series, metric) for metric in metrics}

    def has_value(row: dict[str, Any]) -> bool:
        return any(series_value(row, m, statistics[m]) is not None for m in metrics)

    usable = [row for row in series if has_value(row)]
    current = [row for row in usable if row["in_period"]]
    if not current:
        return None
    latest = current[-1]
    earlier = [row for row in usable if row["date"] < latest["date"]]
    baseline = earlier[-1] if earlier else None
    out: dict[str, Any] = {
        "latest_date": latest["date"],
        "baseline_date": baseline["date"] if baseline else None,
        "baseline_in_period": bool(baseline and baseline["in_period"]),
        "gap_days": None,
        "metrics": {},
    }
    if baseline:
        from datetime import date

        out["gap_days"] = (date.fromisoformat(latest["date"]) - date.fromisoformat(baseline["date"])).days
    for metric in metrics:
        stat = statistics[metric]
        now = series_value(latest, metric, stat)
        before = series_value(baseline, metric, stat) if baseline else None
        item: dict[str, Any] = {"statistic": stat, "latest": now, "baseline": before, "delta": None, "direction": None}
        if now is not None and before is not None:
            # 变化量按报告中印出的两位小数相减，保证表内「最近值 − 基准值 = 变化量」读者可自行核对；
            # 若用未取整的数相减再取整，会出现 0.25 − 0.24 却写 +0.02 的情形。
            delta = round(round(now, 2) - round(before, 2), 2)
            item["delta"] = delta
            item["direction"] = "up" if delta > 0 else ("down" if delta < 0 else "flat")
        out["metrics"][metric] = item
    return out


def build_data_quality(
    facts: dict[str, Any],
    products: list[dict[str, Any]],
    series: list[dict[str, Any]],
    primary_metric: str,
) -> dict[str, Any]:
    """What was observed, what was dropped and how much of the field was usable."""
    period = _period(facts)
    start = _as_day(period.get("start"))
    end_exclusive = _as_day(period.get("end_exclusive"))
    used = [row for row in series if row["in_period"]]
    used_days = {row["date"] for row in used}

    rejected: dict[str, dict[str, Any]] = {}
    for row in facts.get("rejected_optical") or []:
        day = _as_day(row.get("date"))
        if day and day not in used_days:
            cloud, basis = _cloud_of(row)
            rejected[day] = {"date": day, "reason": row.get("reason") or "excluded", "cloud_pct": cloud, "cloud_basis": basis}
    for product in products or []:
        if str(product.get("sensor") or "S2").upper() != _S2:
            continue
        day = _as_day(product.get("date"))
        if not (start and end_exclusive and start <= day < end_exclusive) or day in used_days:
            continue
        if is_eligible_optical(product, optical_days=facts.get("optical_days")):
            continue
        cloud, basis = _cloud_of(product)
        reason = "cloud_over_limit" if cloud is not None and cloud > CLOUD_LIMIT_PCT else "no_index_values"
        rejected.setdefault(day, {"date": day, "reason": reason, "cloud_pct": cloud, "cloud_basis": basis})

    fractions = [
        row["metrics"][primary_metric].get("valid_fraction")
        for row in used
        if row["metrics"][primary_metric].get("valid_fraction") is not None
    ]
    return {
        "used_dates": sorted(used_days),
        "rejected": [rejected[day] for day in sorted(rejected)],
        "cloud_limit_pct": CLOUD_LIMIT_PCT,
        "pixel_dates": [row["date"] for row in used if row["source"] == "pixels"],
        "mean_only_dates": [row["date"] for row in used if row["source"] == "field_mean"],
        "valid_fraction_min": min(fractions) if fractions else None,
        "valid_fraction_max": max(fractions) if fractions else None,
        "radar_count": int((period.get("s1_products") or facts.get("s1_count") or 0) or 0),
    }


def attach_report_inputs(
    facts: dict[str, Any], products: list[dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    """Fill the series, metric plan, comparison and quality blocks the PDF reads.

    Mutates ``facts``; returns date → pixel product for the map step.
    """
    series, product_by_date = build_metric_series(facts, list(products or []))
    in_period_days = [row["date"] for row in series if row["in_period"]]
    plan = report_metric_plan(facts.get("phase"), in_period_days[-1] if in_period_days else None)
    facts["report_metrics"] = plan
    facts["metric_series"] = series
    facts["metric_statistics"] = {m: series_statistic(series, m) for m in REPORT_METRIC_ORDER}
    facts["comparison"] = build_comparison(series, plan["primary"] + plan["supporting"])
    facts["data_quality"] = build_data_quality(facts, list(products or []), series, plan["primary"][0])
    return product_by_date


def ensure_report_inputs(facts: dict[str, Any]) -> dict[str, Any]:
    """Facts ready for rendering; computes the blocks from observations if absent.

    未经 enrich 的 facts（如证据受限简报）没有像元产品，此时只用接口下发的田块均值。
    """
    if facts.get("metric_series") is not None and facts.get("report_metrics"):
        return facts
    filled = dict(facts)
    attach_report_inputs(filled, [])
    filled.setdefault("infield", [])
    return filled


def _has_antecedent_daily(facts: dict[str, Any], start: str) -> bool:
    return any(_as_day(row.get("date")) < start for row in (facts.get("weather_daily") or []) if isinstance(row, dict))


def enrich_official_report_facts(
    facts: dict[str, Any],
    *,
    figures_dir: str | Path,
    products: list[dict[str, Any]] | None = None,
    products_dir: str | Path | None = None,
    storage: Any | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    fetch_weather: Any | None = None,
    fetch_truecolor: Any | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    """Mutate and return official facts with weather and context maps attached.

    ``offline=True`` 时只使用传入的 facts 和像元产品，不访问任何服务：不从对象存储补读产品，
    不经内部接口取地块中心点，不下载气象数据，不查询影像目录、不下载真彩色影像。
    缺少的气象和周边对照整节按未提供处理，并在报告「本期未提供的内容」中写明。
    """
    filled = dict(facts)
    period = _period(filled)
    figures = Path(figures_dir)
    figures.mkdir(parents=True, exist_ok=True)
    loaded = resolve_products(
        filled, products=products, products_dir=products_dir, storage=storage, offline=offline
    )

    end_inclusive = _end_inclusive(period)
    start = _as_day(period.get("start"))
    if loaded and not filled.get("prior_observations"):
        earlier = last_prior_period_products(loaded, start, optical_days=filled.get("optical_days"))
        if earlier:
            filled["prior_observations"] = [_observation_from_product(item) for item in earlier]

    product_by_date = attach_report_inputs(filled, loaded)
    plan = filled["report_metrics"]
    comparison = filled["comparison"]
    # 库里有日期但气温、降水全空，或只有本期之前的日子，都要再拉。
    # 已有的非空值会留下来，拉失败也不把它们清掉。
    needs_period = not period_has_usable_weather(
        filled, start=start, end_inclusive=end_inclusive
    )
    needs_antecedent = not _has_antecedent_daily(filled, start)
    if not offline and start and end_inclusive and (needs_period or needs_antecedent):
        point = resolve_report_lat_lon(
            land_id=_land_id(filled) or None,
            latitude=latitude,
            longitude=longitude,
            products=loaded,
        )
        if point is None:
            if needs_period:
                filled["weather_error"] = "no_centroid"
        else:
            lat, lon = point
            getter = fetch_weather or fetch_period_weather
            try:
                weather = getter(
                    latitude=lat,
                    longitude=lon,
                    period_start=start,
                    period_end_inclusive=end_inclusive,
                )
            except Exception as exc:  # noqa: BLE001
                weather = {"weather_error": f"{type(exc).__name__}"}
            compensated = apply_fetched_weather(
                filled, weather, start=start, end_inclusive=end_inclusive
            )
            filled.update(
                {
                    key: value
                    for key, value in compensated.items()
                    if value is not None or key == "weather_error"
                }
            )

    # 田内图：每个主要指标一幅，日期与结论中的对比口径（comparison）一致。
    infield: list[dict[str, Any]] = []
    latest_product = product_by_date.get(comparison["latest_date"]) if comparison else None
    baseline_product = (
        product_by_date.get(comparison["baseline_date"]) if comparison and comparison.get("baseline_date") else None
    )
    if latest_product is not None:
        # 两个及以上主要指标共用一页，图按该页高度绘制，避免 PDF 再把大图缩糊。
        compact_maps = len(plan["primary"]) > 1
        for metric in plan["primary"]:
            try:
                item = build_infield_metric(
                    metric=metric,
                    latest=latest_product,
                    baseline=baseline_product,
                    dest=figures / f"infield_{metric}.png",
                    compact=compact_maps,
                )
            except Exception:  # noqa: BLE001 — 单个指标出图失败不影响其余内容
                item = None
            if item is not None:
                infield.append(item)
    filled["infield"] = infield
    if infield:
        first = infield[0]
        filled["spatial_variation_image"] = first.get("image")
        filled["spatial_variation_meta"] = {
            "metric": first["metric"],
            "date": first["date"],
            "prior_date": first.get("baseline_date"),
            "pixel_count": first["stats"]["n_valid"],
        }

    if offline:
        surrounding_error = "offline"
    else:
        try:
            surrounding_error = _attach_surrounding(filled, loaded, period, figures, fetch_truecolor)
        except Exception as exc:  # noqa: BLE001 — 周边对照是可选内容，失败时标为未提供，不影响正文
            surrounding_error = f"surrounding_{type(exc).__name__}"
    meta = filled.get("surrounding_comparison_meta") or {}
    if not filled.get("surrounding_comparison_image") or not meta.get("comparisons"):
        # 没有成图或没有可比较的数值时，整节按未提供处理，不留半截内容。
        if surrounding_error is None:
            surrounding_error = "classification_failed"
        for key in (
            "surrounding_comparison",
            "surrounding_comparison_image",
            "surrounding_contrast_image",
            "surrounding_judgment",
            "surrounding_comparison_meta",
        ):
            filled.pop(key, None)
        filled["surrounding_error"] = surrounding_error or "unavailable"
        filled["surrounding_missing_reason"] = SURROUNDING_MISSING_REASONS.get(
            surrounding_error or "", "未能完成周边1—2公里农田对照"
        )
    # 水分和田内格局只用已经附在事实里的序列、气象和像元，不另外取数。
    from app.reports.biweekly.field_patterns import assess_field_patterns
    from app.reports.biweekly.water_risk import assess_water_risk

    filled["field_patterns"] = assess_field_patterns(filled, list(product_by_date.values()))
    filled["water_risk"] = assess_water_risk(filled, list(product_by_date.values()))
    return filled


def _attach_surrounding(
    filled: dict[str, Any],
    loaded: list[dict[str, Any]],
    period: dict[str, Any],
    figures: Path,
    fetch_truecolor: Any | None,
) -> str | None:
    """Add the 1–2 km imagery-farmland contrast. Returns an error code, or None."""
    surrounding_path = figures / "surrounding_farmland.png"
    latest = _latest_in_period(loaded, period, optical_days=filled.get("optical_days"))
    points = _field_points(loaded, period, optical_days=filled.get("optical_days"))
    bounds = _processing_window_bounds(latest) if latest else None
    if bounds is None and points is not None:
        bounds = bounds_from_field_pixels(points[0], points[1])
    item_id = resolve_stac_item_id(latest, bounds) if latest and bounds else None
    surrounding_error = None
    if latest is None or points is None:
        surrounding_error = "no_in_period_field_pixels"
        rgb = None
    elif not bounds:
        surrounding_error = "no_processing_window"
        rgb = None
    elif not item_id:
        surrounding_error = "stac_item_unavailable"
        rgb = None
    else:
        getter = fetch_truecolor or fetch_processing_window_truecolor
        try:
            rgb = getter(stac_item_id=item_id, bounds=bounds)
        except Exception as exc:  # noqa: BLE001
            rgb = None
            surrounding_error = f"truecolor_{type(exc).__name__}"
        if rgb is None and surrounding_error is None:
            surrounding_error = "truecolor_unavailable"
    if rgb is not None and points is not None and bounds is not None:
        by_date: dict = {}
        current = _in_period_products(
            loaded,
            _as_day(period.get("start")),
            _as_day(period.get("end_exclusive")),
            optical_days=filled.get("optical_days"),
        )
        for product in current:
            day = _as_day(product.get("date"))
            item = resolve_stac_item_id(product, bounds)
            if not item:
                continue
            try:
                scene = getter(stac_item_id=item, bounds=bounds) if day != _as_day(latest.get("date")) else rgb
            except Exception:
                scene = None
            if scene is None:
                continue
            classified = classify_imagery_farmland(
                scene, bounds=bounds, field_lons=points[0], field_lats=points[1]
            )
            if classified:
                by_date[day] = {
                    "stats": {
                        "field": classified["stats_field"],
                        "surrounding_1km": classified["stats_1km"],
                        "surrounding_2km": classified["stats_2km"],
                    }
                }
        built = build_surrounding_comparison(
            rgb=rgb,
            bounds=bounds,
            field_lons=points[0],
            field_lats=points[1],
            dest=surrounding_path,
            date=_as_day(latest.get("date")),
            by_date=by_date or None,
            contrast_dest=figures / "greenness_contrast.png",
            basemap_note=filled.get("surrounding_basemap_note"),
        )
        if built:
            filled["surrounding_comparison"] = built["text"]
            if built.get("image"):
                filled["surrounding_comparison_image"] = built["image"]
            if built.get("contrast_image"):
                filled["surrounding_contrast_image"] = built["contrast_image"]
            if built.get("judgment"):
                filled["surrounding_judgment"] = built["judgment"]
            filled["surrounding_comparison_meta"] = {
                k: v for k, v in built.items() if k not in {"text", "image", "contrast_image"}
            }
        else:
            surrounding_error = surrounding_error or "classification_failed"
    return surrounding_error


SURROUNDING_MISSING_REASONS = {
    "offline": "本次为离线生成，未获取周边1—2公里的同期真彩色影像",
    "no_in_period_field_pixels": "本期没有足够的本田像元，无法在影像上确定本田位置",
    "no_processing_window": "未能确定覆盖本田周边的影像范围",
    "stac_item_unavailable": "未能在影像目录中找到覆盖本田的同期哨兵二号影像",
    "truecolor_unavailable": "未能取得覆盖周边1—2公里的同期真彩色影像",
    "classification_failed": "同期真彩色影像中未能识别出周边农田",
}
