"""Load compact weather and autumn-emergence evidence for LLM prompts.

Callers pass a SQLAlchemy session. Missing tables or empty results return
None so each source can be omitted independently.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text

from agric_satellite_analysis_common.agri_classify import CLOUD_MAX_PCT
from agric_satellite_analysis_common.autumn_emergence import (
    daily_fields_from_rows,
    default_season_key,
    official_daily_fields_sql,
    season_bounds,
    summary_for_model,
    autumn_season_key,
)
from agric_satellite_analysis_common.weather_gdd import (
    gdd_for_model,
    iso_day,
    weather_period_for_model,
)

_LAND_SQL = text(
    """
    SELECT land_id, crop_type,
           autumn_emerged, autumn_emerged_season, autumn_emerged_date,
           autumn_emerged_at, autumn_sowing_date, autumn_sowing_season
    FROM agric_satellite.land_parcels
    WHERE land_id = :land_id AND deleted_at IS NULL
    """
)

_WEATHER_SQL = text(
    """
    SELECT date, temperature_2m_min, temperature_2m_max, temperature_2m_mean,
           precipitation_sum, et0_fao_mm, gdd_daily, gdd_cumulative,
           drought_index, water_balance_30d_mm
    FROM agric_satellite.weather_daily
    WHERE land_id = :land_id
      AND date >= :date_from AND date <= :date_to
    ORDER BY date
    """
)

_DAILY_SQL = text(official_daily_fields_sql("p"))


def _mappings(session: Any, statement, params: dict[str, Any]) -> list[dict[str, Any]]:
    result = session.execute(statement, params)
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings().all()]
    return [dict(row) for row in result]


def _one(session: Any, statement, params: dict[str, Any]) -> dict[str, Any] | None:
    rows = _mappings(session, statement, params)
    return rows[0] if rows else None


def load_weather_daily(
    session: Any,
    land_id: str,
    date_from: str | date,
    date_to: str | date,
) -> list[dict[str, Any]]:
    start = iso_day(date_from)
    end = iso_day(date_to)
    if not start or not end:
        return []
    try:
        return _mappings(
            session,
            _WEATHER_SQL,
            {"land_id": str(land_id), "date_from": start, "date_to": end},
        )
    except Exception:
        return []


def load_land_flags(session: Any, land_id: str) -> dict[str, Any] | None:
    try:
        return _one(session, _LAND_SQL, {"land_id": str(land_id)})
    except Exception:
        return None


def load_autumn_emergence_for_model(
    session: Any,
    land_id: str,
    *,
    as_of: str | date | None = None,
    season_key: str | None = None,
) -> dict[str, Any] | None:
    """Latest algorithm summary for the season. None when no result and no land flag."""
    day = date.fromisoformat(iso_day(as_of) or date.today().isoformat())
    season = season_key or autumn_season_key(day) or default_season_key(day)
    start, end = season_bounds(season)
    land = load_land_flags(session, land_id)
    if land and land.get("autumn_sowing_date"):
        sown = land["autumn_sowing_date"]
        sown_day = sown if isinstance(sown, date) else date.fromisoformat(str(sown)[:10])
        if sown_day > start:
            start = sown_day
    try:
        rows = _mappings(
            session,
            _DAILY_SQL,
            {
                "land_id": str(land_id),
                "date_from": start,
                "date_to": min(end, day),
                "cloud_max": CLOUD_MAX_PCT,
            },
        )
    except Exception:
        rows = []
    fields = daily_fields_from_rows(rows)
    return summary_for_model(daily_fields=fields, land=land)


def weather_and_gdd_for_model(
    rows: list[dict[str, Any]] | None,
    *,
    start: str | date | None,
    end: str | date | None,
    crop_type: str | None = None,
    sowing_date: str | date | None = None,
    stage_names: list[str] | None = None,
) -> dict[str, Any]:
    """Build independent weather and 积温 blocks. Omit a key when that source is empty."""
    start_day = iso_day(start)
    end_day = iso_day(end)
    body: dict[str, Any] = {}
    weather = weather_period_for_model(rows, start=start_day, end=end_day)
    if weather:
        body["weather"] = weather
    gdd = gdd_for_model(
        rows,
        start=start_day,
        end=end_day,
        crop_type=crop_type,
        stage_names=stage_names,
        sowing_date=iso_day(sowing_date),
        latest=end_day,
    )
    if gdd:
        body["gdd"] = gdd
    return body
