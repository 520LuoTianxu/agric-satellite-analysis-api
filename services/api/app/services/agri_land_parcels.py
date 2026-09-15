"""Upsert agric_satellite.land_parcels from field geometry + agri tags.

When a field carries ``agri:<land_id>``, the API provisions a complete
``agric_satellite.land_parcels`` row so download-machine / claim / UI can resolve the
parcel without a manual SQL fix. Does not enqueue RS backfill.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agri_tags import (
    iter_tag_strings,
    parse_agri_land_id,
    parse_cdfinance_group_id,
)
from app.core.geo import wkb_to_geojson
from app.core.logging import logger

SOURCE_FILE = "openfarm_field_upsert"
SOURCE_FEATURE_INDEX = 0

UPSERT_LAND_PARCEL_SQL = """
INSERT INTO agric_satellite.land_parcels (
    land_id,
    tile_id,
    land_name,
    group_id,
    land_area_mu,
    original_area_mu,
    boundary_geojson,
    boundary_srid,
    min_lon,
    min_lat,
    max_lon,
    max_lat,
    source_properties,
    source_file,
    source_feature_index,
    province_name,
    city_name,
    county_name,
    town_name,
    village_name,
    created_at,
    updated_at
) VALUES (
    :land_id,
    :tile_id,
    :land_name,
    :group_id,
    :land_area_mu,
    :original_area_mu,
    CAST(:boundary_geojson AS jsonb),
    4326,
    :min_lon,
    :min_lat,
    :max_lon,
    :max_lat,
    CAST(:source_properties AS jsonb),
    :source_file,
    :source_feature_index,
    :province_name,
    :city_name,
    :county_name,
    :town_name,
    :village_name,
    now(),
    now()
)
ON CONFLICT (land_id) DO UPDATE SET
    land_name = EXCLUDED.land_name,
    group_id = COALESCE(EXCLUDED.group_id, agric_satellite.land_parcels.group_id),
    land_area_mu = COALESCE(EXCLUDED.land_area_mu, agric_satellite.land_parcels.land_area_mu),
    original_area_mu = COALESCE(
        EXCLUDED.original_area_mu, agric_satellite.land_parcels.original_area_mu
    ),
    boundary_geojson = EXCLUDED.boundary_geojson,
    min_lon = EXCLUDED.min_lon,
    min_lat = EXCLUDED.min_lat,
    max_lon = EXCLUDED.max_lon,
    max_lat = EXCLUDED.max_lat,
    source_properties = EXCLUDED.source_properties,
    source_file = EXCLUDED.source_file,
    source_feature_index = EXCLUDED.source_feature_index,
    province_name = COALESCE(
        EXCLUDED.province_name, agric_satellite.land_parcels.province_name
    ),
    city_name = COALESCE(EXCLUDED.city_name, agric_satellite.land_parcels.city_name),
    county_name = COALESCE(EXCLUDED.county_name, agric_satellite.land_parcels.county_name),
    town_name = COALESCE(EXCLUDED.town_name, agric_satellite.land_parcels.town_name),
    village_name = COALESCE(
        EXCLUDED.village_name, agric_satellite.land_parcels.village_name
    ),
    tile_id = CASE
        WHEN agric_satellite.land_parcels.source_file = :source_file
        THEN EXCLUDED.tile_id
        ELSE agric_satellite.land_parcels.tile_id
    END,
    updated_at = now()
"""


def build_openfarm_tile_id(land_id: str, group_id: str | None = None) -> str:
    """tile_id convention for agric-satellite-analysis-provisioned parcels."""
    lid = str(land_id).strip()
    if group_id is not None and str(group_id).strip():
        return f"p{str(group_id).strip()}_t00001_a{lid}"
    return f"p_manual_t00001_a{lid}"


def _parse_tag_value(tags: Any, prefix: str) -> str | None:
    for tag in iter_tag_strings(tags):
        if tag.startswith(prefix):
            val = tag[len(prefix) :].strip()
            if val:
                return val
    return None


def _area_mu_from_ha(area_ha: Any) -> float | None:
    if area_ha is None:
        return None
    try:
        return round(float(area_ha) * 15.0, 4)
    except (TypeError, ValueError):
        return None


def build_land_parcel_upsert_params(
    *,
    land_id: str,
    boundary_geojson: dict[str, Any],
    land_name: str | None = None,
    group_id: str | None = None,
    area_ha: float | None = None,
    tags: Any = None,
    field_id: str | None = None,
    farm_id: str | None = None,
) -> dict[str, Any]:
    """Build bind params for UPSERT_LAND_PARCEL_SQL (pure; no DB)."""
    from shapely.geometry import shape

    geom = shape(boundary_geojson)
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ValueError(f"boundary must be Polygon/MultiPolygon, got {geom.geom_type}")
    if geom.is_empty:
        raise ValueError("boundary geometry is empty")

    min_lon, min_lat, max_lon, max_lat = geom.bounds
    area_mu = _area_mu_from_ha(area_ha)
    gid = (
        str(group_id).strip()
        if group_id is not None and str(group_id).strip()
        else parse_cdfinance_group_id(tags)
    )
    tile_id = build_openfarm_tile_id(land_id, gid)

    source_properties: dict[str, Any] = {"source": SOURCE_FILE}
    if field_id:
        source_properties["field_id"] = str(field_id)
    if farm_id:
        source_properties["farm_id"] = str(farm_id)

    return {
        "land_id": str(land_id).strip(),
        "tile_id": tile_id,
        "land_name": land_name,
        "group_id": gid,
        "land_area_mu": area_mu,
        "original_area_mu": area_mu,
        "boundary_geojson": json.dumps(boundary_geojson, ensure_ascii=False),
        "min_lon": float(min_lon),
        "min_lat": float(min_lat),
        "max_lon": float(max_lon),
        "max_lat": float(max_lat),
        "source_properties": json.dumps(source_properties, ensure_ascii=False),
        "source_file": SOURCE_FILE,
        "source_feature_index": SOURCE_FEATURE_INDEX,
        "province_name": _parse_tag_value(tags, "province:"),
        "city_name": _parse_tag_value(tags, "city:"),
        "county_name": _parse_tag_value(tags, "county:"),
        "town_name": _parse_tag_value(tags, "town:"),
        "village_name": _parse_tag_value(tags, "village:"),
    }


async def ensure_agri_land_parcel_for_field(
    db: AsyncSession,
    field: Any,
    land_id: str | None = None,
    group_id: str | None = None,
) -> bool:
    """Upsert ``agric_satellite.land_parcels`` from field geom + tags.

    Returns True when an upsert was executed, False when skipped (no land_id
    or no usable geometry). Raises on SQL / geometry errors.
    """
    tags = getattr(field, "tags_json", None)
    lid = (str(land_id).strip() if land_id else None) or parse_agri_land_id(tags)
    if not lid:
        return False

    boundary = wkb_to_geojson(getattr(field, "geom", None))
    if not boundary or not isinstance(boundary, dict):
        logger.warning(
            "agri_land_parcel_skip_no_geom",
            field_id=str(getattr(field, "id", "")),
            land_id=lid,
        )
        return False

    farm_id = getattr(field, "farm_id", None)
    params = build_land_parcel_upsert_params(
        land_id=lid,
        boundary_geojson=boundary,
        land_name=getattr(field, "name", None),
        group_id=group_id,
        area_ha=(
            float(field.area_ha)
            if getattr(field, "area_ha", None) is not None
            else None
        ),
        tags=tags,
        field_id=str(field.id) if getattr(field, "id", None) else None,
        farm_id=str(farm_id) if farm_id else None,
    )

    await db.execute(text(UPSERT_LAND_PARCEL_SQL), params)
    logger.info(
        "agri_land_parcel_upserted",
        field_id=str(getattr(field, "id", "") or ""),
        land_id=lid,
        tile_id=params["tile_id"],
        group_id=params.get("group_id"),
    )
    return True
