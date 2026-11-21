"""Read ingested parcel_scene_products for biweekly generate/list."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.core.config import settings

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


def _eligible_optical(row: dict[str, Any]) -> bool:
    if str(row.get("sensor") or "").upper() != "S2":
        return False
    cloud = row.get("parcel_cloud_cover_pct")
    if cloud is None:
        cloud = row.get("cloud_cover")
    try:
        if cloud is not None and float(cloud) > 30:
            return False
    except (TypeError, ValueError):
        return False
    return any(row.get(key) is not None for key in ("ndvi_avg", "evi_avg", "ndre_avg", "ndmi_avg"))


def _to_observation(row: dict[str, Any]) -> dict[str, Any]:
    day = row["date"]
    date_s = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    eligible = _eligible_optical(row)
    return {
        "date": date_s,
        "sensor": str(row.get("sensor") or ""),
        "scene_id": row.get("scene_id"),
        "quality_status": "eligible_descriptive" if eligible else "excluded",
        "metrics": {
            "ndvi": row.get("ndvi_avg"),
            "evi": row.get("evi_avg"),
            "ndre": row.get("ndre_avg"),
            "ndmi": row.get("ndmi_avg"),
            "vv": row.get("vv_avg"),
            "vh": row.get("vh_avg"),
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
    except Exception:  # noqa: BLE001
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
