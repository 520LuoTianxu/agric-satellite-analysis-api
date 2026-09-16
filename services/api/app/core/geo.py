"""GeoJSON helpers shared across routers.

The database stores parcel and observation geometries as JSONB.  Shapely is
used only in application memory for validation and calculations; no database
spatial extension or binary geometry conversion is involved.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shapely.geometry import shape as shapely_shape
from shapely.geometry.base import BaseGeometry


def geojson_to_shape(
    geojson: Mapping[str, Any] | None,
) -> BaseGeometry | None:
    """Convert a JSONB GeoJSON object to an in-memory Shapely geometry."""
    if not isinstance(geojson, Mapping):
        return None
    try:
        return shapely_shape(geojson)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def geojson_centroid(
    geojson: Mapping[str, Any] | None,
) -> tuple[float, float] | None:
    """Return ``(latitude, longitude)`` for a JSONB GeoJSON geometry."""
    geometry = geojson_to_shape(geojson)
    if geometry is None or geometry.is_empty:
        return None
    centroid = geometry.centroid
    return float(centroid.y), float(centroid.x)
