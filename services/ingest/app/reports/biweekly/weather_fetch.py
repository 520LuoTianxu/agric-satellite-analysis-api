"""Period weather for official biweekly PDFs, using existing ingest sources.

The download-machine generate path calls this before rendering so the official
PDF can include rainfall and temperature. Open-Meteo archive is primary;
NASA POWER is the existing fallback. This module does not write Postgres,
does not apply the daily-sync land blacklist, and does not invent values.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

ANTECEDENT_DAYS = 30


def _as_day(value: Any) -> str:
    return str(value or "")[:10]


def _num(series: list[Any] | None, index: int) -> float | None:
    if not series or index >= len(series) or series[index] is None:
        return None
    try:
        return float(series[index])
    except (TypeError, ValueError):
        return None


def rows_from_daily_payload(daily: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Map Open-Meteo / NASA-POWER daily blocks to official_pdf weather rows."""
    if not daily:
        return []
    times = list(daily.get("time") or [])
    precip = daily.get("precipitation_sum") or []
    tmin = daily.get("temperature_2m_min") or []
    tmax = daily.get("temperature_2m_max") or []
    et0 = daily.get("et0_fao_evapotranspiration") or daily.get("et0_fao_mm") or []
    rows: list[dict[str, Any]] = []
    for i, day in enumerate(times):
        stamp = _as_day(day)
        if len(stamp) != 10:
            continue
        rows.append(
            {
                "date": stamp,
                "precipitation_mm": _num(precip, i),
                "min_c": _num(tmin, i),
                "max_c": _num(tmax, i),
                "et0_mm": _num(et0, i),
            }
        )
    return rows


def summarize_weather_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "days": 0,
            "precipitation_mm": None,
            "et0_mm": None,
            "min_c": None,
            "max_c": None,
            "mean_c": None,
            "precipitation_days": 0,
            "et0_days": 0,
            "temperature_days": 0,
        }
    rains = [
        row["precipitation_mm"]
        for row in rows
        if row.get("precipitation_mm") is not None
    ]
    et0s = [row["et0_mm"] for row in rows if row.get("et0_mm") is not None]
    mins = [row["min_c"] for row in rows if row.get("min_c") is not None]
    maxs = [row["max_c"] for row in rows if row.get("max_c") is not None]
    means = [
        (row["min_c"] + row["max_c"]) / 2
        for row in rows
        if row.get("min_c") is not None and row.get("max_c") is not None
    ]
    return {
        "days": len(rows),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "precipitation_mm": sum(rains) if rains else None,
        "et0_mm": sum(et0s) if et0s else None,
        "min_c": min(mins) if mins else None,
        "max_c": max(maxs) if maxs else None,
        "mean_c": (sum(means) / len(means)) if means else None,
        "missing_precipitation": len(rains) != len(rows),
        "missing_et0": len(et0s) != len(rows),
        # 有记录的天数随合计一并给出：缺测日不计入合计，报告须写明合计覆盖了几天。
        "precipitation_days": len(rains),
        "et0_days": len(et0s),
        "temperature_days": min(len(mins), len(maxs)),
    }


_CLIMATE_KEYS = ("precipitation_mm", "min_c", "max_c")


def _row_has_climate(row: dict[str, Any]) -> bool:
    return any(row.get(key) is not None for key in _CLIMATE_KEYS)


def _rows_have_climate(rows: list[dict[str, Any]]) -> bool:
    return any(_row_has_climate(row) for row in rows)


def _summary_has_climate(summary: Any) -> bool:
    if not isinstance(summary, dict):
        return False
    return any(
        summary.get(key) is not None
        for key in ("precipitation_mm", "min_c", "max_c", "mean_c")
    )


def period_has_usable_weather(
    facts: dict[str, Any], *, start: str, end_inclusive: str
) -> bool:
    """本期至少有一天气温或降水。只有日期、数值全空，不算已经有天气。"""
    rows = [
        row
        for row in (facts.get("weather_daily") or [])
        if isinstance(row, dict) and start <= _as_day(row.get("date")) <= end_inclusive
    ]
    if _rows_have_climate(rows):
        return True
    # 有些调用只带合计、不带逐日。合计里有气温或降水就不要再整段重拉。
    if not rows and _summary_has_climate(facts.get("weather_period")):
        return True
    return False


def merge_weather_daily(
    existing: list[Any] | None, incoming: list[Any] | None
) -> list[dict[str, Any]]:
    """按日期合并。已有的非空气温、降水、蒸散保留，空位用新拉到的值补上。"""
    by_date: dict[str, dict[str, Any]] = {}
    for batch in (existing or [], incoming or []):
        for row in batch:
            if not isinstance(row, dict):
                continue
            day = _as_day(row.get("date"))
            if len(day) != 10:
                continue
            current = by_date.get(day)
            if current is None:
                by_date[day] = {
                    "date": day,
                    "precipitation_mm": row.get("precipitation_mm"),
                    "min_c": row.get("min_c"),
                    "max_c": row.get("max_c"),
                    "et0_mm": row.get("et0_mm"),
                }
                continue
            for key in ("precipitation_mm", "min_c", "max_c", "et0_mm"):
                if current.get(key) is None and row.get(key) is not None:
                    current[key] = row[key]
    return [by_date[day] for day in sorted(by_date)]


def apply_fetched_weather(
    existing: dict[str, Any],
    fetched: dict[str, Any],
    *,
    start: str,
    end_inclusive: str,
) -> dict[str, Any]:
    """把拉到的天气补进事实。拉取失败时保留原来已经有的气温和降水。"""
    merged = merge_weather_daily(
        existing.get("weather_daily"), fetched.get("weather_daily")
    )
    period_rows = [row for row in merged if start <= row["date"] <= end_inclusive]
    antecedent_rows = [row for row in merged if row["date"] < start]
    period = summarize_weather_rows(period_rows) if period_rows else None
    antecedent = summarize_weather_rows(antecedent_rows) if antecedent_rows else None
    if not _summary_has_climate(period) and _summary_has_climate(
        existing.get("weather_period")
    ):
        period = existing.get("weather_period")
    if period is None:
        period = summarize_weather_rows([])
    if not _summary_has_climate(antecedent) and _summary_has_climate(
        existing.get("weather_antecedent")
    ):
        antecedent = existing.get("weather_antecedent")
    if antecedent is None:
        antecedent = summarize_weather_rows([])
    prior = fetched.get("weather_same_period_prior_year")
    if not _summary_has_climate(prior):
        prior = existing.get("weather_same_period_prior_year")
    usable = _summary_has_climate(period)
    fetched_rows = [
        row for row in (fetched.get("weather_daily") or []) if isinstance(row, dict)
    ]
    if usable and _rows_have_climate(fetched_rows):
        source = fetched.get("weather_source") or existing.get("weather_source")
    else:
        source = existing.get("weather_source") or fetched.get("weather_source")
    error = None
    if not usable:
        error = fetched.get("weather_error") or existing.get("weather_error")
    return {
        "weather_daily": merged,
        "weather_period": period,
        "weather_antecedent": antecedent,
        "weather_same_period_prior_year": prior,
        "weather_source": source,
        "weather_error": error,
    }


def _supplement_period_climate(
    latitude: float,
    longitude: float,
    period_start: str,
    period_end: str,
    rows: list[dict[str, Any]],
    source: str | None,
    error: str | None,
    *,
    forecast_fetcher: Any | None = None,
    nasa_fetcher: Any | None = None,
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """本期还没有气温和降水时，再用预报接口和 NASA POWER 补这一段。

    Open-Meteo 归档经常比最近几天慢。归档窗口里已有更早日期时，整段会被当成
    成功，本期仍是空的。这里只补空位，不覆盖已有数值，也不编造缺测日。
    """
    period_rows = [row for row in rows if period_start <= row["date"] <= period_end]
    if _rows_have_climate(period_rows):
        return rows, source, error
    try:
        start_day = date.fromisoformat(period_start)
        end_day = date.fromisoformat(period_end)
    except ValueError:
        return rows, source, error
    try:
        from agric_satellite_analysis_common.weather_window import (
            resolve_historical_weather_window,
        )

        window = resolve_historical_weather_window(
            date_from=start_day, date_to=end_day
        )
    except ValueError:
        return rows, source, error or "no_data"

    merged = rows
    try:
        if forecast_fetcher is None:
            from app.tasks.weather import _fetch_open_meteo as forecast_fetcher

        payload = forecast_fetcher(
            latitude, longitude, window.start, window.end, use_archive=False
        )
        forecast_rows = rows_from_daily_payload((payload or {}).get("daily"))
        merged = merge_weather_daily(merged, forecast_rows)
        period_rows = [
            row for row in merged if period_start <= row["date"] <= period_end
        ]
        if _rows_have_climate(period_rows):
            return merged, source or "open-meteo", None
    except Exception:  # noqa: BLE001 — 预报接口失败再试 NASA，不在这里中断出报
        pass

    if source == "nasa-power":
        return merged, source, error or "no_data"
    try:
        if nasa_fetcher is None:
            from app.tasks.weather import _fetch_nasa_power as nasa_fetcher

        payload = nasa_fetcher(latitude, longitude, window.start, window.end)
        nasa_rows = rows_from_daily_payload((payload or {}).get("daily"))
        merged = merge_weather_daily(merged, nasa_rows)
        period_rows = [
            row for row in merged if period_start <= row["date"] <= period_end
        ]
        if _rows_have_climate(period_rows):
            return merged, "nasa-power", None
    except Exception:  # noqa: BLE001 — 两个来源都没有时保持空，不编数
        pass
    return merged, source, error or "no_data"


def centroid_from_products(
    products: list[dict[str, Any]],
) -> tuple[float, float] | None:
    """Field-pixel centroid (lat, lon) from lonlat_columns_v1 products."""
    lats: list[float] = []
    lons: list[float] = []
    for product in products or []:
        columns = (product.get("pixel_data") or {}).get("columns") or {}
        lat_col = columns.get("lat") or []
        lon_col = columns.get("lon") or []
        for lat, lon in zip(lat_col, lon_col):
            if lat is None or lon is None:
                continue
            lats.append(float(lat))
            lons.append(float(lon))
    if not lats or not lons:
        return None
    return sum(lats) / len(lats), sum(lons) / len(lons)


def resolve_report_lat_lon(
    *,
    land_id: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    products: list[dict[str, Any]] | None = None,
) -> tuple[float, float] | None:
    """Resolve a point for weather without reading API Postgres."""
    if latitude is not None and longitude is not None:
        return float(latitude), float(longitude)
    from_pixels = centroid_from_products(products or [])
    if from_pixels is not None:
        return from_pixels
    if not land_id:
        return None
    try:
        from agric_satellite_analysis_common.internal_api import (
            internal_api_enabled,
            land_geom,
        )

        if not internal_api_enabled():
            return None
        geom = land_geom(land_id)
        lat, lon = geom.get("centroid_lat"), geom.get("centroid_lon")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except Exception:  # noqa: BLE001 — weather is optional; renderer will mark missing
        return None


def _prior_year(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def _load_daily(
    latitude: float, longitude: float, start: date, end: date
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Open-Meteo archive, then NASA POWER. Returns daily, source, error."""
    from app.tasks.weather import (
        _daily_has_usable_rows,
        _fetch_nasa_power,
        _fetch_open_meteo,
    )

    error: str | None = None
    try:
        payload = _fetch_open_meteo(
            float(latitude), float(longitude), start, end, use_archive=True
        )
        if _daily_has_usable_rows((payload or {}).get("daily")):
            return (payload or {}).get("daily"), "open-meteo", None
        error = "open_meteo_no_data"
    except Exception as exc:  # noqa: BLE001
        error = f"open_meteo_{type(exc).__name__}"
    try:
        payload = _fetch_nasa_power(float(latitude), float(longitude), start, end)
        return (payload or {}).get("daily"), "nasa-power", None
    except Exception as exc:  # noqa: BLE001
        return None, None, error or f"nasa_power_{type(exc).__name__}"


def _summary_for_range(
    latitude: float,
    longitude: float,
    start: date,
    end: date,
    loader: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    daily, _source, error = (loader or _load_daily)(latitude, longitude, start, end)
    if daily is None:
        return None, error or "no_data"
    rows = [
        row
        for row in rows_from_daily_payload(daily)
        if start.isoformat() <= row["date"] <= end.isoformat()
    ]
    summary = summarize_weather_rows(rows)
    if summary.get("temperature_days") or summary.get("precipitation_mm") is not None:
        return summary, None
    return None, error or "no_data"


def fetch_period_weather(
    *,
    latitude: float,
    longitude: float,
    period_start: str,
    period_end_inclusive: str,
    antecedent_days: int = ANTECEDENT_DAYS,
    daily_loader: Any | None = None,
) -> dict[str, Any]:
    """Fetch period + antecedent daily weather. Never invents or writes a database."""
    from agric_satellite_analysis_common.weather_window import (
        resolve_historical_weather_window,
    )

    start = date.fromisoformat(_as_day(period_start)) - timedelta(
        days=int(antecedent_days)
    )
    end = date.fromisoformat(_as_day(period_end_inclusive))
    try:
        window = resolve_historical_weather_window(date_from=start, date_to=end)
    except ValueError as exc:
        return {
            "weather_daily": [],
            "weather_period": summarize_weather_rows([]),
            "weather_antecedent": summarize_weather_rows([]),
            "weather_same_period_prior_year": None,
            "weather_source": None,
            "weather_error": str(exc),
        }

    loader = daily_loader or _load_daily
    daily, source, error = loader(
        float(latitude), float(longitude), window.start, window.end
    )
    rows = rows_from_daily_payload(daily) if daily else []
    # 调用方传入的 loader 负责全部取数，测试不再访问外部接口。
    # 正式出报在归档没有本期气温和降水时，改走预报接口和 NASA POWER。
    if daily_loader is None:
        rows, source, error = _supplement_period_climate(
            float(latitude),
            float(longitude),
            _as_day(period_start),
            _as_day(period_end_inclusive),
            rows,
            source,
            error,
        )
    if not rows:
        return {
            "weather_daily": [],
            "weather_period": summarize_weather_rows([]),
            "weather_antecedent": summarize_weather_rows([]),
            "weather_same_period_prior_year": None,
            "weather_source": None,
            "weather_error": error,
        }

    period_rows = [
        row
        for row in rows
        if _as_day(period_start) <= row["date"] <= _as_day(period_end_inclusive)
    ]
    antecedent_end = _as_day(period_start)
    antecedent_rows = [
        row for row in rows if _as_day(start) <= row["date"] < antecedent_end
    ]
    period_start_day = date.fromisoformat(_as_day(period_start))
    period_end_day = date.fromisoformat(_as_day(period_end_inclusive))
    prior_summary, _prior_error = _summary_for_range(
        float(latitude),
        float(longitude),
        _prior_year(period_start_day),
        _prior_year(period_end_day),
        loader,
    )
    return {
        # official_pdf charts "本期及可获得的对照日"; totals stay split.
        "weather_daily": antecedent_rows + period_rows,
        "weather_period": summarize_weather_rows(period_rows),
        "weather_antecedent": summarize_weather_rows(antecedent_rows),
        "weather_same_period_prior_year": prior_summary,
        "weather_source": source,
        "weather_error": None,
    }
