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

from app.reports.biweekly.context_maps import (
    _as_day,
    _columns,
    _in_period_products,
    last_prior_period_products,
    _prior_products,
    bounds_from_field_pixels,
    build_infield_variation,
    build_surrounding_comparison,
    classify_imagery_farmland,
    fetch_processing_window_truecolor,
    resolve_stac_item_id,
)
from app.reports.biweekly.weather_fetch import fetch_period_weather, resolve_report_lat_lon

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
) -> list[dict[str, Any]]:
    if products is not None:
        return list(products)
    loaded: list[dict[str, Any]] = []
    if products_dir:
        loaded = load_products_from_dir(products_dir)
    if loaded:
        return loaded
    land_id = _land_id(facts)
    if not land_id:
        return []
    return load_products_from_oss(land_id, _collect_dates(facts, []), storage=storage)


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
    row = {
        "date": _as_day(product.get("date")),
        "sensor": str(product.get("sensor") or "S2"),
    }
    for metric in ("ndvi", "evi", "ndre", "ndmi"):
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
) -> dict[str, Any]:
    """Mutate and return official facts with weather and context maps attached."""
    filled = dict(facts)
    period = _period(filled)
    figures = Path(figures_dir)
    figures.mkdir(parents=True, exist_ok=True)
    loaded = resolve_products(
        filled, products=products, products_dir=products_dir, storage=storage
    )

    end_inclusive = _end_inclusive(period)
    start = _as_day(period.get("start"))
    if loaded and not filled.get("prior_observations"):
        earlier = last_prior_period_products(loaded, start, optical_days=filled.get("optical_days"))
        if earlier:
            filled["prior_observations"] = [_observation_from_product(item) for item in earlier]
    if start and end_inclusive and (not filled.get("weather_daily") or not _has_antecedent_daily(filled, start)):
        point = resolve_report_lat_lon(
            land_id=_land_id(filled) or None,
            latitude=latitude,
            longitude=longitude,
            products=loaded,
        )
        if point is None:
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
            filled.update({k: v for k, v in weather.items() if v is not None or k == "weather_error"})

    infield_path = figures / "spatial_zones.png"
    infield = build_infield_variation(
        products=loaded,
        period=period,
        phase=filled.get("phase") or {},
        dest=infield_path,
        optical_days=filled.get("optical_days"),
    )
    if infield and infield.get("image"):
        filled["spatial_variation"] = infield.get("bullets") or infield["text"]
        filled["spatial_variation_image"] = infield["image"]
        filled["spatial_variation_meta"] = {k: v for k, v in infield.items() if k != "text"}
        if infield.get("next_week"):
            filled.setdefault("spatial_variation_meta", {})["next_week"] = infield["next_week"]
    elif not filled.get("spatial_variation"):
        filled["spatial_variation"] = (
            "本期没有足够的本田像元分布，田内分区图标为缺失，不编造灾情面积。"
        )

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
    if not filled.get("surrounding_comparison"):
        reason = {
            "no_in_period_field_pixels": "本期没有足够的本田像元，无法在真彩色底图上落下本田位置。",
            "no_processing_window": "未能根据本田像元重建处理窗口，不能把已有预览图误当作 10 公里范围来用。",
            "stac_item_unavailable": "未能在官方影像目录中找到覆盖本田的同期哨兵-2 景，1–2 公里对照不能改用项目登记地块。",
            "truecolor_unavailable": "未能重新下载覆盖 1–2 公里的同期真彩色窗口。",
        }.get(surrounding_error or "", "未能完成 1–2 公里影像农田对照。")
        filled["surrounding_comparison"] = (
            reason + "因此本期对照标为缺失。没有改用项目登记地块，也没有借用其他地块的图。"
        )
        filled["surrounding_error"] = surrounding_error
    return filled
