"""Read ingested parcel_scene_products for biweekly generate/list."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.core.logging import logger

SCENE_SQL = """
SELECT land_id, date, sensor, scene_id,
       ndvi_avg, evi_avg, ndre_avg, ndmi_avg,
       cloud_cover, parcel_cloud_cover_pct,
       vv_avg, vh_avg
FROM agric_satellite.parcel_scene_products
WHERE land_id = :land_id
  AND date >= :start
  AND date < :end_exclusive
ORDER BY date, sensor, scene_id
"""


def _as_day(value: Any) -> str:
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return raw[:10]


def _as_sensor(value: Any) -> str:
    return str(value or "").strip().upper()


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _eligible_optical(row: dict[str, Any]) -> bool:
    if _as_sensor(row.get("sensor")) != "S2":
        return False
    cloud = row.get("parcel_cloud_cover_pct")
    if cloud is None:
        cloud = row.get("cloud_cover")
    try:
        if cloud is not None and float(cloud) > 30:
            return False
    except (TypeError, ValueError):
        return False
    return any(
        row.get(key) is not None for key in ("ndvi_avg", "evi_avg", "ndre_avg", "ndmi_avg")
    )


def _exclusion_reason(row: dict[str, Any]) -> str | None:
    """为什么一景光学影像未被采用；雷达景不属于光学剔除，返回None。"""
    if _as_sensor(row.get("sensor")) != "S2":
        return None
    cloud = row.get("parcel_cloud_cover_pct")
    if cloud is None:
        cloud = row.get("cloud_cover")
    try:
        if cloud is not None and float(cloud) > 30:
            return "cloud_over_limit"
    except (TypeError, ValueError):
        return "cloud_unreadable"
    return "no_index_values"


def _to_observation(row: dict[str, Any]) -> dict[str, Any]:
    date_s = _as_day(row["date"])
    sensor = _as_sensor(row.get("sensor"))
    eligible = _eligible_optical({**row, "sensor": sensor})
    ndvi = _as_float(row.get("ndvi_avg"))
    evi = _as_float(row.get("evi_avg"))
    ndre = _as_float(row.get("ndre_avg"))
    ndmi = _as_float(row.get("ndmi_avg"))
    return {
        "date": date_s,
        "sensor": sensor,
        "scene_id": row.get("scene_id"),
        "quality_status": "eligible_descriptive" if eligible else "excluded",
        "exclusion_reason": None if eligible else _exclusion_reason({**row, "sensor": sensor}),
        # 云量随观测一并下发，报告据此说明数据质量；缺测保持None，不写成0。
        "cloud_cover": _as_float(row.get("cloud_cover")),
        "parcel_cloud_cover_pct": _as_float(row.get("parcel_cloud_cover_pct")),
        "ndvi_avg": ndvi,
        "evi_avg": evi,
        "ndre_avg": ndre,
        "ndmi_avg": ndmi,
        "metrics": {
            "ndvi": ndvi,
            "evi": evi,
            "ndre": ndre,
            "ndmi": ndmi,
            "vv": _as_float(row.get("vv_avg")),
            "vh": _as_float(row.get("vh_avg")),
        },
        "source": "parcel_scene_products",
    }


async def load_scene_observations(
    land_id: str, start: str, end_exclusive: str
) -> list[dict[str, Any]] | None:
    """Return observations, or None if the product table cannot be read."""
    if not (settings.database_url or "").strip():
        return None
    try:
        from app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(
                text(SCENE_SQL),
                {
                    "land_id": str(land_id),
                    "start": start,
                    "end_exclusive": end_exclusive,
                },
            )
            rows = [dict(row) for row in result.mappings().all()]
        return [_to_observation(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        # Do not log the URL or DSN; a failed read must not be treated as "no optics"
        # by callers without checking for None.
        logger.warning(
            "biweekly_scene_query_failed land_id=%s err_type=%s",
            land_id,
            type(exc).__name__,
        )
        return None


async def count_period_scenes(
    land_id: str, start: str, end_exclusive: str
) -> dict[str, int] | None:
    rows = await load_scene_observations(land_id, start, end_exclusive)
    if rows is None:
        return None
    optical = [row for row in rows if row["quality_status"] == "eligible_descriptive"]
    radar = [row for row in rows if str(row.get("sensor") or "").upper() == "S1"]
    return {
        "optical_descriptive_days": len({row["date"] for row in optical}),
        "s1_products": len(radar),
        "has_eligible_optics": bool(optical),
    }


def is_maize_crop(crop_type: str | None) -> bool:
    text_value = (crop_type or "").strip().lower()
    return any(token in text_value for token in ("maize", "corn", "玉米"))


def is_wheat_crop(crop_type: str | None) -> bool:
    text_value = (crop_type or "").strip().lower()
    if not text_value:
        return False
    return any(token in text_value for token in ("wheat", "小麦", "冬小麦"))



WEATHER_SQL = """
SELECT date, temperature_2m_min, temperature_2m_max, precipitation_sum, et0_fao_mm
FROM agric_satellite.weather_daily
WHERE land_id = :land_id
  AND date >= :start
  AND date <= :end_inclusive
ORDER BY date
"""

LAND_SQL = """
SELECT land_id, land_name, crop_type
FROM agric_satellite.land_parcels
WHERE land_id = :land_id AND deleted_at IS NULL
"""


def _weather_row(row: dict[str, Any]) -> dict[str, Any]:
    day = row["date"]
    date_s = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]

    def num(key: str):
        value = row.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return {
        "date": date_s,
        "min_c": num("temperature_2m_min"),
        "max_c": num("temperature_2m_max"),
        "precipitation_mm": num("precipitation_sum"),
        "et0_mm": num("et0_fao_mm"),
    }


def _summarize_weather(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"days": 0, "precipitation_mm": None, "et0_mm": None, "min_c": None, "max_c": None}
    rains = [row["precipitation_mm"] for row in rows if row.get("precipitation_mm") is not None]
    et0s = [row["et0_mm"] for row in rows if row.get("et0_mm") is not None]
    mins = [row["min_c"] for row in rows if row.get("min_c") is not None]
    maxs = [row["max_c"] for row in rows if row.get("max_c") is not None]
    return {
        "days": len(rows),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "precipitation_mm": sum(rains) if rains else None,
        "et0_mm": sum(et0s) if et0s else None,
        "min_c": min(mins) if mins else None,
        "max_c": max(maxs) if maxs else None,
        "missing_precipitation": len(rains) != len(rows),
        "missing_et0": len(et0s) != len(rows),
    }


async def load_weather_series(
    land_id: str, start: str, end_inclusive: str
) -> list[dict[str, Any]] | None:
    """Return daily weather rows, or None if the table cannot be read."""
    if not (settings.database_url or "").strip():
        return None
    try:
        from app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(
                text(WEATHER_SQL),
                {
                    "land_id": str(land_id),
                    "start": start,
                    "end_inclusive": end_inclusive,
                },
            )
            rows = [dict(row) for row in result.mappings().all()]
        return [_weather_row(row) for row in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "biweekly_weather_query_failed land_id=%s err_type=%s",
            land_id,
            type(exc).__name__,
        )
        return None


async def load_land_meta(land_id: str) -> dict[str, Any] | None:
    if not (settings.database_url or "").strip():
        return None
    try:
        from app.core.database import async_session

        async with async_session() as db:
            result = await db.execute(text(LAND_SQL), {"land_id": str(land_id)})
            row = result.mappings().first()
        if not row:
            return {"land_id": str(land_id), "land_name": None, "crop_type": None}
        return {
            "land_id": str(row["land_id"]),
            "land_name": row.get("land_name"),
            "crop_type": row.get("crop_type"),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "biweekly_land_meta_query_failed land_id=%s err_type=%s",
            land_id,
            type(exc).__name__,
        )
        return None


def summarize_weather(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if rows is None:
        return None
    return _summarize_weather(rows)
