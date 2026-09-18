"""Per-land remote-sensing processing window helpers."""

from __future__ import annotations

import math
from typing import Any

from pyproj import Transformer
from shapely.geometry import Polygon, box, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from app.core.config import settings


def resolve_processing_window_km(value: float | int | str | None = None) -> float:
    """Resolve and validate the side length of the per-land square AOI."""
    raw = settings.processing_window_km if value is None else value
    try:
        side_km = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("processing_window_km must be numeric") from exc
    if not math.isfinite(side_km) or side_km <= 0:
        raise ValueError("processing_window_km must be greater than zero")
    return side_km


def build_processing_window(
    land_geom: BaseGeometry,
    side_km: float | int | str | None = None,
) -> Polygon:
    """Build a metric square centered on the parcel centroid.

    A local azimuthal-equidistant projection keeps the requested 5 km side
    length accurate without relying on a single China-wide projected CRS.
    """
    side_km = resolve_processing_window_km(side_km)
    if land_geom.is_empty:
        raise ValueError("land geometry is empty")

    centroid = land_geom.centroid
    to_local = Transformer.from_crs(
        "EPSG:4326",
        f"+proj=aeqd +lat_0={centroid.y} +lon_0={centroid.x} "
        "+datum=WGS84 +units=m +no_defs",
        always_xy=True,
    ).transform
    from_local = Transformer.from_crs(
        f"+proj=aeqd +lat_0={centroid.y} +lon_0={centroid.x} "
        "+datum=WGS84 +units=m +no_defs",
        "EPSG:4326",
        always_xy=True,
    ).transform
    center_x, center_y = to_local(centroid.x, centroid.y)
    half_side_m = side_km * 1000.0 / 2.0
    local_square = Polygon(
        [
            (center_x - half_side_m, center_y - half_side_m),
            (center_x + half_side_m, center_y - half_side_m),
            (center_x + half_side_m, center_y + half_side_m),
            (center_x - half_side_m, center_y + half_side_m),
        ]
    )
    return shapely_transform(from_local, local_square)


def processing_window_geojson(
    land_geom: BaseGeometry,
    side_km: float | int | str | None = None,
) -> dict[str, Any]:
    """Return the per-land processing square as JSON-serializable GeoJSON."""
    return dict(mapping(build_processing_window(land_geom, side_km)))


def build_complete_processing_window(
    land_geom: BaseGeometry,
    side_km: float | int | str | None = None,
) -> tuple[BaseGeometry, bool]:
    """Return a square, or a full parcel bbox when the parcel is oversized."""
    square = build_processing_window(land_geom, side_km)
    if square.covers(land_geom):
        return square, False
    # 不能用5 km网格裁掉超大地块，超大地块单独使用完整外接矩形。
    return box(*land_geom.bounds), True
