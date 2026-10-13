"""GeoJSON helpers shared across ingest tasks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry import shape as shapely_shape
from shapely.ops import unary_union
from shapely.validation import make_valid
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


def _dedupe_ring(coords) -> list[tuple[float, ...]]:
    """去掉连续重复点（含首尾闭合之外的重复），保持环的顺序。"""
    output: list[tuple[float, ...]] = []
    for point in coords:
        point = tuple(point)
        if not output or point != output[-1]:
            output.append(point)
    return output


def _dedupe_polygon(polygon: Polygon) -> Polygon:
    exterior = _dedupe_ring(polygon.exterior.coords)
    interiors = [_dedupe_ring(ring.coords) for ring in polygon.interiors]
    interiors = [ring for ring in interiors if len(ring) >= 4]
    if len(exterior) < 4:
        return Polygon()
    return Polygon(exterior, interiors)


def _polygonal_parts(geometry: BaseGeometry | None) -> list[Polygon]:
    """只保留面要素，GeometryCollection 里的线、点在修复后丢弃。"""
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry] if geometry.area > 0 else []
    if isinstance(geometry, MultiPolygon):
        return [part for part in geometry.geoms if part.area > 0]
    if isinstance(geometry, GeometryCollection):
        parts: list[Polygon] = []
        for item in geometry.geoms:
            parts.extend(_polygonal_parts(item))
        return parts
    return []


def repair_polygon_geometry(geometry: BaseGeometry | None) -> BaseGeometry | None:
    """修复自相交/重复点等无效地块边界，返回有效的 Polygon/MultiPolygon。

    修复顺序：去连续重复点 → make_valid（失败时 buffer(0)）→ 仅保留面部分。
    已有效的边界原样返回；修复后为空或面积为 0 时返回 None，由调用方拒绝。
    """
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type not in {"Polygon", "MultiPolygon", "GeometryCollection"}:
        return None
    if geometry.is_valid:
        parts = _polygonal_parts(geometry)
        if geometry.geom_type in {"Polygon", "MultiPolygon"} and parts:
            return geometry
    else:
        parts = []
    try:
        if isinstance(geometry, Polygon):
            candidate: BaseGeometry = _dedupe_polygon(geometry)
        elif isinstance(geometry, MultiPolygon):
            candidate = MultiPolygon(
                [p for p in (_dedupe_polygon(g) for g in geometry.geoms) if not p.is_empty]
            )
        else:
            candidate = geometry
    except (ValueError, TypeError):
        candidate = geometry
    repaired: BaseGeometry | None = None
    if not candidate.is_empty and candidate.is_valid:
        repaired = candidate
    else:
        try:
            repaired = make_valid(candidate)
        except Exception:  # noqa: BLE001 - GEOS 异常时退回 buffer(0)
            repaired = None
        if repaired is None or not _polygonal_parts(repaired):
            try:
                repaired = candidate.buffer(0)
            except Exception:  # noqa: BLE001
                repaired = None
    parts = _polygonal_parts(repaired)
    if not parts:
        return None
    result: BaseGeometry = parts[0] if len(parts) == 1 else MultiPolygon(parts)
    if not result.is_valid:
        # 多个面拼成 MultiPolygon 可能共边相接，再合并一次。
        result = unary_union(parts)
        if not _polygonal_parts(result):
            return None
        if isinstance(result, GeometryCollection):
            result = MultiPolygon(_polygonal_parts(result))
    if result.is_empty or result.area <= 0 or not result.is_valid:
        return None
    return result
