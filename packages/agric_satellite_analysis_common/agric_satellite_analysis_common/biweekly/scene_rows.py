"""Turn stored scene and weather rows into biweekly report inputs.

Pure mapping: no database, no network. The API applies it to rows read from
``agric_satellite.parcel_scene_products`` and ``agric_satellite.weather_daily``;
offline tooling applies it to exported rows, so both mark the same scenes as
usable and give the same exclusion reason.

光学观测采用规则：哨兵二号、地块云量（缺失时用整景云量）不超过 30%、至少有一个指数值。
缺测保持 None，不写成 0。
"""

from __future__ import annotations

from typing import Any

OPTICAL_CLOUD_LIMIT_PCT = 30.0
OPTICAL_INDEX_COLUMNS = ("ndvi_avg", "evi_avg", "ndre_avg", "ndmi_avg")


def as_day(value: Any) -> str:
    raw = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return raw[:10]


def as_sensor(value: Any) -> str:
    return str(value or "").strip().upper()


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def eligible_optical(row: dict[str, Any]) -> bool:
    if as_sensor(row.get("sensor")) != "S2":
        return False
    cloud = row.get("parcel_cloud_cover_pct")
    if cloud is None:
        cloud = row.get("cloud_cover")
    try:
        if cloud is not None and float(cloud) > OPTICAL_CLOUD_LIMIT_PCT:
            return False
    except (TypeError, ValueError):
        return False
    return any(row.get(key) is not None for key in OPTICAL_INDEX_COLUMNS)


def exclusion_reason(row: dict[str, Any]) -> str | None:
    """为什么一景光学影像未被采用；雷达景不属于光学剔除，返回None。"""
    if as_sensor(row.get("sensor")) != "S2":
        return None
    cloud = row.get("parcel_cloud_cover_pct")
    if cloud is None:
        cloud = row.get("cloud_cover")
    try:
        if cloud is not None and float(cloud) > OPTICAL_CLOUD_LIMIT_PCT:
            return "cloud_over_limit"
    except (TypeError, ValueError):
        return "cloud_unreadable"
    return "no_index_values"


def to_observation(
    row: dict[str, Any], *, source: str = "parcel_scene_products"
) -> dict[str, Any]:
    date_s = as_day(row["date"])
    sensor = as_sensor(row.get("sensor"))
    eligible = eligible_optical({**row, "sensor": sensor})
    ndvi = as_float(row.get("ndvi_avg"))
    evi = as_float(row.get("evi_avg"))
    ndre = as_float(row.get("ndre_avg"))
    ndmi = as_float(row.get("ndmi_avg"))
    return {
        "date": date_s,
        "sensor": sensor,
        "scene_id": row.get("scene_id"),
        "quality_status": "eligible_descriptive" if eligible else "excluded",
        "exclusion_reason": None if eligible else exclusion_reason({**row, "sensor": sensor}),
        # 云量随观测一并下发，报告据此说明数据质量；缺测保持None，不写成0。
        "cloud_cover": as_float(row.get("cloud_cover")),
        "parcel_cloud_cover_pct": as_float(row.get("parcel_cloud_cover_pct")),
        "ndvi_avg": ndvi,
        "evi_avg": evi,
        "ndre_avg": ndre,
        "ndmi_avg": ndmi,
        "metrics": {
            "ndvi": ndvi,
            "evi": evi,
            "ndre": ndre,
            "ndmi": ndmi,
            "vv": as_float(row.get("vv_avg")),
            "vh": as_float(row.get("vh_avg")),
        },
        "source": source,
    }


def eligible_observations(rows: list[dict] | None) -> list[dict]:
    if not rows:
        return []
    return [row for row in rows if row.get("quality_status") == "eligible_descriptive"]


def radar_count(rows: list[dict] | None) -> int:
    if not rows:
        return 0
    return sum(1 for row in rows if as_sensor(row.get("sensor")) == "S1")


def weather_row(row: dict[str, Any]) -> dict[str, Any]:
    """One ``weather_daily`` row in the shape the report reads."""

    def num(key: str):
        return as_float(row.get(key))

    return {
        "date": as_day(row["date"]),
        "min_c": num("temperature_2m_min"),
        "max_c": num("temperature_2m_max"),
        "precipitation_mm": num("precipitation_sum"),
        "et0_mm": num("et0_fao_mm"),
    }
