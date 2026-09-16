"""GeoJSON helpers shared across ingest tasks."""

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
