"""10×10 km 动态窗口规划算法；纯几何计算，不持久化任何空间分组。

每次请求基于当前地块集合进行全局 anchor 候选、稀缺地块保护与紧凑度
评分，输出本次下载任务使用的窗口边界和地块成员。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from numbers import Integral
from typing import Any

from pyproj import CRS, Transformer
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from app.core.geo import geojson_to_shape

VPA10_ALGORITHM_VERSION = "dynamic-window-greedy-10km-v1"
DEFAULT_WINDOW_SIDE_M = 10_000.0
# 全国初始化时每轮只保留一批全局候选，避免地块数增长后候选窗口呈平方级膨胀；
# 稀缺度排序仍保证困难地块优先进入候选，调用方可在小规模专项规划中提高上限。
DEFAULT_ANCHOR_LIMIT = 8
DEFAULT_CENTER_LIMIT = 16


@dataclass(frozen=True)
class PlannerParcel:
    """规划所需的最小地块快照。"""

    land_id: str
    geometry: BaseGeometry


@dataclass(frozen=True)
class ExistingArea:
    """已有项目区的边界，用于新项目区重叠惩罚。"""

    tile_id: str
    geometry: BaseGeometry


@dataclass(frozen=True)
class VirtualAreaPlan:
    """一次贪心选择得到的虚拟项目区。"""

    anchor_land_id: str
    land_ids: tuple[str, ...]
    boundary_geojson: dict[str, Any]
    boundary_srid: int
    aggregation_bbox: tuple[float, float, float, float]
    window_side_m: float
    planning_crs: str
    center_x: float
    center_y: float
    protected_score: float
    compactness: float
    fill_ratio: float
    overlap_ratio: float
    oversized: bool = False


@dataclass(frozen=True)
class _LocalParcel:
    land_id: str
    geometry: BaseGeometry


@dataclass(frozen=True)
class _Candidate:
    anchor_land_id: str
    center_x: float
    center_y: float
    window: BaseGeometry
    members: tuple[str, ...]
    protected_score: float
    compactness: float
    fill_ratio: float
    overlap_ratio: float
    planning_crs: str
    to_wgs84: Transformer


def _to_geometry(value: Any) -> BaseGeometry:
    """统一解析 GeoJSON 或 Shapely 几何，并拒绝无效边界。"""
    if isinstance(value, BaseGeometry):
        geometry = value
    elif isinstance(value, dict):
        geometry = shape(value)
    else:
        geometry = geojson_to_shape(value)
    if (
        geometry is None
        or geometry.is_empty
        or not geometry.is_valid
        or geometry.geom_type not in {"Polygon", "MultiPolygon"}
    ):
        raise ValueError("地块边界必须是有效的 Polygon 或 MultiPolygon")
    return geometry


def normalize_parcels(lands: Iterable[Any]) -> list[PlannerParcel]:
    """从 LandParcel、字典或测试对象提取稳定的地块规划快照。"""
    output: list[PlannerParcel] = []
    seen: set[str] = set()
    for land in lands:
        if isinstance(land, PlannerParcel):
            land_id = land.land_id
            geometry = land.geometry
        elif isinstance(land, dict):
            land_id = str(land.get("land_id") or land.get("id") or "").strip()
            raw_geometry = land.get("geometry") or land.get("boundary_geojson")
            geometry = _to_geometry(raw_geometry)
        else:
            land_id = str(getattr(land, "land_id", "")).strip()
            raw_geometry = getattr(land, "boundary_geojson", None)
            geometry = _to_geometry(raw_geometry)
        if not land_id:
            raise ValueError("地块缺少 land_id")
        if land_id in seen:
            continue
        seen.add(land_id)
        output.append(PlannerParcel(land_id=land_id, geometry=geometry))
    return output


def _local_projection(geometry: BaseGeometry) -> tuple[CRS, Transformer, Transformer]:
    """以当前候选 anchor 建立局部等距投影，避免经纬度直接计算米数。"""
    center = geometry.representative_point()
    crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m +no_defs"
    )
    to_local = Transformer.from_crs(4326, crs, always_xy=True)
    to_wgs84 = Transformer.from_crs(crs, 4326, always_xy=True)
    return crs, to_local, to_wgs84


def _feasible_center_rect(
    geometry: BaseGeometry, half_side_m: float
) -> tuple[float, float, float, float] | None:
    """计算能够完整覆盖地块的窗口中心矩形。"""
    return _feasible_center_rect_from_bounds(geometry.bounds, half_side_m)


def _feasible_center_rect_from_bounds(
    bounds: tuple[float, float, float, float], half_side_m: float
) -> tuple[float, float, float, float] | None:
    """从米制 bounds 计算完整覆盖窗口的中心可行域。"""
    min_x, min_y, max_x, max_y = bounds
    left = max_x - half_side_m
    right = min_x + half_side_m
    bottom = max_y - half_side_m
    top = min_y + half_side_m
    if left > right or bottom > top:
        return None
    return left, bottom, right, top


def _rect_intersects(
    first: tuple[float, float, float, float] | None,
    second: tuple[float, float, float, float] | None,
) -> bool:
    if first is None or second is None:
        return False
    return not (
        first[2] < second[0]
        or first[0] > second[2]
        or first[3] < second[1]
        or first[1] > second[3]
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _rough_query_box(geometry: BaseGeometry, side_m: float) -> BaseGeometry:
    """按米制窗口换算一个保守的 WGS84 粗筛框。"""
    center = geometry.centroid
    lat_delta = side_m / 110_800.0 * 1.5
    # 经度每度的实际长度随纬度缩短；设置下限避免极区出现无穷大的框。
    lon_scale = max(abs(math.cos(math.radians(center.y))), 0.1)
    lon_delta = lat_delta / lon_scale
    return box(
        center.x - lon_delta,
        center.y - lat_delta,
        center.x + lon_delta,
        center.y + lat_delta,
    )


def _tree_positions(
    tree: STRtree,
    query_geometry: BaseGeometry,
    *,
    identity_index: dict[int, int],
) -> list[int]:
    """兼容 Shapely 1/2 的 STRtree 查询结果，统一返回几何位置。"""
    positions: list[int] = []
    for value in tree.query(query_geometry):
        if isinstance(value, Integral):
            positions.append(int(value))
        else:
            position = identity_index.get(id(value))
            if position is not None:
                positions.append(position)
    return positions


def _approximate_metric_bounds(
    geometry: BaseGeometry, *, origin_x: float, origin_y: float
) -> tuple[float, float, float, float]:
    """用局部等距近似换算 bounds；仅用于稀缺度排序，不用于最终边界。"""
    latitude = math.radians(origin_y)
    meter_per_lon = 111_320.0 * max(abs(math.cos(latitude)), 0.1)
    meter_per_lat = 110_540.0
    min_x, min_y, max_x, max_y = geometry.bounds
    return (
        (min_x - origin_x) * meter_per_lon,
        (min_y - origin_y) * meter_per_lat,
        (max_x - origin_x) * meter_per_lon,
        (max_y - origin_y) * meter_per_lat,
    )


def _candidate_centers(
    anchor: _LocalParcel,
    nearby: Sequence[_LocalParcel],
    existing_windows: Sequence[BaseGeometry],
    *,
    half_side_m: float,
    center_limit: int,
) -> list[tuple[float, float]]:
    """生成有限且确定性的候选中心，不把 anchor 质心当成唯一中心。"""
    anchor_rect = _feasible_center_rect(anchor.geometry, half_side_m)
    if anchor_rect is None:
        return [(anchor.geometry.centroid.x, anchor.geometry.centroid.y)]

    min_x, min_y, max_x, max_y = anchor_rect
    x_values = {min_x, max_x, (min_x + max_x) / 2}
    y_values = {min_y, max_y, (min_y + max_y) / 2}

    # 候选地块的可行中心边界是“覆盖数量”发生变化的临界位置。
    for parcel in nearby:
        rect = _feasible_center_rect(parcel.geometry, half_side_m)
        if not _rect_intersects(anchor_rect, rect):
            continue
        assert rect is not None
        x_values.update(
            (
                _clamp(rect[0], min_x, max_x),
                _clamp(rect[2], min_x, max_x),
            )
        )
        y_values.update(
            (
                _clamp(rect[1], min_y, max_y),
                _clamp(rect[3], min_y, max_y),
            )
        )

    # 将新窗口边界对齐到已有窗口边界，给“最小重叠”留下可行候选。
    for existing in existing_windows:
        e_min_x, e_min_y, e_max_x, e_max_y = existing.bounds
        x_values.update(
            _clamp(value, min_x, max_x)
            for value in (
                e_min_x - half_side_m,
                e_min_x + half_side_m,
                e_max_x - half_side_m,
                e_max_x + half_side_m,
            )
        )
        y_values.update(
            _clamp(value, min_y, max_y)
            for value in (
                e_min_y - half_side_m,
                e_min_y + half_side_m,
                e_max_y - half_side_m,
                e_max_y + half_side_m,
            )
        )

    # 小网格用于覆盖重叠目标在内部的最优点；数量受限，避免候选爆炸。
    for ratio in (0.25, 0.5, 0.75):
        x_values.add(min_x + (max_x - min_x) * ratio)
        y_values.add(min_y + (max_y - min_y) * ratio)

    centers = [(x, y) for x in sorted(x_values) for y in sorted(y_values)]
    anchor_center = (anchor.geometry.centroid.x, anchor.geometry.centroid.y)
    centers.sort(
        key=lambda item: (
            abs(item[0] - anchor_center[0]) + abs(item[1] - anchor_center[1]),
            item,
        )
    )
    return centers[:center_limit]


def _scarcity_scores(
    parcels: Sequence[PlannerParcel],
    *,
    side_m: float,
    target_land_ids: set[str] | None = None,
) -> dict[str, float]:
    """按可共同覆盖地块数量估算稀缺度。

    稀缺度只在同一窗口附近有意义。使用 STRtree 先做空间粗筛，再在 anchor
    的局部等距投影中计算可行中心矩形，避免全国地块初始化退化成 O(N²) 全量投影。
    ``target_land_ids`` 用于每轮只刷新受上轮移除地块影响的局部难度。
    """
    if not parcels:
        return {}
    geometries = [parcel.geometry for parcel in parcels]
    tree = STRtree(geometries)
    identity_index = {id(geometry): index for index, geometry in enumerate(geometries)}
    parcel_by_id = {parcel.land_id: parcel for parcel in parcels}
    targets = set(parcel_by_id) if target_land_ids is None else target_land_ids
    scores: dict[str, float] = {}
    for parcel in parcels:
        if parcel.land_id not in targets:
            continue
        origin = parcel.geometry.centroid
        local_bounds = _approximate_metric_bounds(
            parcel.geometry, origin_x=origin.x, origin_y=origin.y
        )
        rect = _feasible_center_rect_from_bounds(local_bounds, side_m / 2)
        if rect is None:
            scores[parcel.land_id] = 2.0
            continue
        eligible = 0
        # 只有可能被同一个 10×10km 窗口完整容纳的地块才需要投影。
        nearby_positions = _tree_positions(
            tree,
            _rough_query_box(parcel.geometry, side_m),
            identity_index=identity_index,
        )
        for other_index in nearby_positions:
            other = parcels[other_index]
            if other.land_id == parcel.land_id:
                continue
            other_bounds = _approximate_metric_bounds(
                other.geometry, origin_x=origin.x, origin_y=origin.y
            )
            other_rect = _feasible_center_rect_from_bounds(other_bounds, side_m / 2)
            if _rect_intersects(rect, other_rect):
                eligible += 1
        size_ratio = min(
            max(local_bounds[2] - local_bounds[0], local_bounds[3] - local_bounds[1])
            / side_m,
            1.0,
        )
        scores[parcel.land_id] = 1.0 / (eligible + 1.0) + 0.25 * size_ratio
    return scores


def _candidate_for_anchor(
    anchor: PlannerParcel,
    remaining: Sequence[PlannerParcel],
    difficulty: dict[str, float],
    existing_areas: Sequence[ExistingArea],
    *,
    side_m: float,
    anchor_tree: STRtree,
    geometry_index: dict[int, str],
    geometry_identity_index: dict[int, int],
    existing_tree: STRtree | None,
    existing_identity_index: dict[int, int] | None,
    existing_geometries: Sequence[BaseGeometry],
    center_limit: int,
) -> list[_Candidate]:
    """为一个 anchor 生成候选项目区并完成精确 covers 判断。"""
    _, to_local, to_wgs84 = _local_projection(anchor.geometry)
    center = anchor.geometry.representative_point()
    planning_crs = f"AEQD:{center.y:.6f},{center.x:.6f}"
    half_side_m = side_m / 2
    # 先用 WGS84 bbox 粗筛，再转投影；这一步避免每轮把远处全国地块投影。
    query_box = _rough_query_box(anchor.geometry, side_m)
    remaining_ids = {parcel.land_id for parcel in remaining}
    nearby_ids = {
        geometry_index[index]
        for index in _tree_positions(
            anchor_tree, query_box, identity_index=geometry_identity_index
        )
        if geometry_index.get(index) in remaining_ids
    }
    parcels_by_id = {parcel.land_id: parcel for parcel in remaining}
    local_by_id = {
        land_id: _LocalParcel(
            land_id, transform(to_local.transform, parcels_by_id[land_id].geometry)
        )
        for land_id in nearby_ids
    }
    local_anchor = local_by_id[anchor.land_id]
    nearby = [local_by_id[land_id] for land_id in sorted(nearby_ids)]
    if existing_tree is not None and existing_identity_index is not None:
        existing_positions = _tree_positions(
            existing_tree, query_box, identity_index=existing_identity_index
        )
        existing_local = [
            transform(to_local.transform, existing_geometries[index])
            for index in existing_positions
        ]
    else:
        existing_local = [
            transform(to_local.transform, _to_geometry(area.geometry))
            for area in existing_areas
        ]

    candidates: list[_Candidate] = []
    for center_x, center_y in _candidate_centers(
        local_anchor,
        nearby,
        existing_local,
        half_side_m=half_side_m,
        center_limit=center_limit,
    ):
        window = box(
            center_x - half_side_m,
            center_y - half_side_m,
            center_x + half_side_m,
            center_y + half_side_m,
        )
        members = tuple(
            parcel.land_id for parcel in nearby if window.covers(parcel.geometry)
        )
        if anchor.land_id not in members:
            continue
        union_members = unary_union(
            [local_by_id[land_id].geometry for land_id in members]
        )
        union_existing = unary_union(existing_local) if existing_local else None
        overlap_ratio = (
            float(union_existing.intersection(window).area / window.area)
            if union_existing is not None and not union_existing.is_empty
            else 0.0
        )
        centroid_distances = [
            local_by_id[land_id].geometry.centroid.distance(window.centroid)
            for land_id in members
        ]
        compactness = max(
            0.0,
            1.0
            - (sum(centroid_distances) / max(len(centroid_distances), 1))
            / (half_side_m * math.sqrt(2)),
        )
        fill_ratio = min(max(float(union_members.area / window.area), 0.0), 1.0)
        protected_score = sum(difficulty.get(land_id, 0.0) for land_id in members)
        candidates.append(
            _Candidate(
                anchor_land_id=anchor.land_id,
                center_x=center_x,
                center_y=center_y,
                window=window,
                members=tuple(sorted(members)),
                protected_score=protected_score,
                compactness=compactness,
                fill_ratio=fill_ratio,
                overlap_ratio=overlap_ratio,
                planning_crs=planning_crs,
                to_wgs84=to_wgs84,
            )
        )
    return candidates


def _candidate_sort_key(candidate: _Candidate) -> tuple[Any, ...]:
    """实现“数量优先、稀缺保护、紧凑、重叠最小”的稳定排序。"""
    return (
        len(candidate.members),
        candidate.protected_score,
        -candidate.overlap_ratio,
        candidate.compactness,
        candidate.fill_ratio,
        tuple(candidate.members),
    )


def _to_plan(candidate: _Candidate, *, side_m: float) -> VirtualAreaPlan:
    boundary = transform(candidate.to_wgs84.transform, candidate.window)
    return VirtualAreaPlan(
        anchor_land_id=candidate.anchor_land_id,
        land_ids=candidate.members,
        boundary_geojson=mapping(boundary),
        boundary_srid=4326,
        aggregation_bbox=tuple(float(value) for value in boundary.bounds),
        window_side_m=side_m,
        planning_crs=candidate.planning_crs,
        center_x=candidate.center_x,
        center_y=candidate.center_y,
        protected_score=candidate.protected_score,
        compactness=candidate.compactness,
        fill_ratio=candidate.fill_ratio,
        overlap_ratio=candidate.overlap_ratio,
    )


def plan_virtual_areas(
    lands: Sequence[Any],
    *,
    existing_areas: Sequence[ExistingArea] = (),
    window_side_m: float = DEFAULT_WINDOW_SIDE_M,
    anchor_limit: int = DEFAULT_ANCHOR_LIMIT,
    center_limit: int = DEFAULT_CENTER_LIMIT,
) -> list[VirtualAreaPlan]:
    """全局 anchor + 稀缺地块保护的贪心动态窗口规划。"""
    if window_side_m <= 0:
        raise ValueError("window_side_m必须大于0")
    parcels = normalize_parcels(lands)
    if not parcels:
        return []

    remaining = {parcel.land_id: parcel for parcel in parcels}
    parcels_by_id = {parcel.land_id: parcel for parcel in parcels}
    all_geometries = [parcel.geometry for parcel in parcels]
    # Shapely 2 返回几何索引；保留对象身份映射，同时兼容 Shapely 1。
    index_by_position = {index: parcel.land_id for index, parcel in enumerate(parcels)}
    geometry_identity_index = {
        id(geometry): index for index, geometry in enumerate(all_geometries)
    }
    tree = STRtree(all_geometries)
    existing_geometries = [_to_geometry(area.geometry) for area in existing_areas]
    existing_tree = STRtree(existing_geometries) if existing_geometries else None
    existing_identity_index = (
        {id(geometry): index for index, geometry in enumerate(existing_geometries)}
        if existing_geometries
        else None
    )
    difficulty = _scarcity_scores(parcels, side_m=window_side_m)
    plans: list[VirtualAreaPlan] = []

    while remaining:
        current = list(remaining.values())
        current_ids = {parcel.land_id for parcel in current}
        # 每轮只在剩余地块中挑选困难地块作为全局候选 anchor。
        anchor_ids = sorted(
            current_ids,
            key=lambda land_id: (-difficulty.get(land_id, 0.0), land_id),
        )[: max(1, min(anchor_limit, len(current)))]
        candidates: list[_Candidate] = []
        for anchor_id in anchor_ids:
            candidates.extend(
                _candidate_for_anchor(
                    remaining[anchor_id],
                    current,
                    difficulty,
                    existing_areas,
                    side_m=window_side_m,
                    anchor_tree=tree,
                    geometry_index=index_by_position,
                    geometry_identity_index=geometry_identity_index,
                    existing_tree=existing_tree,
                    existing_identity_index=existing_identity_index,
                    existing_geometries=existing_geometries,
                    center_limit=center_limit,
                )
            )
        if not candidates:
            # 理论上只有无效/超大几何会走到这里；保留单地块计划，避免死循环。
            anchor = min(current, key=lambda parcel: parcel.land_id)
            boundary = anchor.geometry.envelope
            plans.append(
                VirtualAreaPlan(
                    anchor_land_id=anchor.land_id,
                    land_ids=(anchor.land_id,),
                    boundary_geojson=mapping(boundary),
                    boundary_srid=4326,
                    aggregation_bbox=tuple(float(value) for value in boundary.bounds),
                    window_side_m=window_side_m,
                    planning_crs="EPSG:4326",
                    center_x=anchor.geometry.centroid.x,
                    center_y=anchor.geometry.centroid.y,
                    protected_score=difficulty.get(anchor.land_id, 0.0),
                    compactness=0.0,
                    fill_ratio=0.0,
                    overlap_ratio=0.0,
                    oversized=True,
                )
            )
            remaining.pop(anchor.land_id)
            continue

        best = max(candidates, key=_candidate_sort_key)
        plan = _to_plan(best, side_m=window_side_m)
        plans.append(plan)
        for land_id in plan.land_ids:
            remaining.pop(land_id, None)

        # 当前实现使用静态 STRtree，remaining 通过字典过滤。只刷新本轮移除
        # 地块周边的稀缺度，保持“局部重新计算”同时避免每轮扫描全国地块。
        if remaining:
            removed_geometries = [
                # best.members 已从 remaining 移除；从标准化快照取几何。
                parcels_by_id[land_id].geometry
                for land_id in plan.land_ids
            ]
            affected_ids: set[str] = set()
            current_after = list(remaining.values())
            current_after_geometries = [parcel.geometry for parcel in current_after]
            current_after_tree = STRtree(current_after_geometries)
            current_after_identity = {
                id(geometry): index
                for index, geometry in enumerate(current_after_geometries)
            }
            for removed_geometry in removed_geometries:
                for index in _tree_positions(
                    current_after_tree,
                    _rough_query_box(removed_geometry, window_side_m),
                    identity_index=current_after_identity,
                ):
                    affected_ids.add(current_after[index].land_id)
            difficulty.update(
                _scarcity_scores(
                    current_after,
                    side_m=window_side_m,
                    target_land_ids=affected_ids,
                )
            )
            difficulty = {
                land_id: difficulty.get(land_id, 0.0) for land_id in remaining
            }

    return plans


def find_containing_area(
    parcel: Any,
    areas: Sequence[ExistingArea],
) -> list[ExistingArea]:
    """返回完全包含地块的项目区，调用方可按资产完整度继续排序。"""
    geometry = normalize_parcels([parcel])[0].geometry
    return [area for area in areas if _to_geometry(area.geometry).covers(geometry)]


__all__ = [
    "DEFAULT_ANCHOR_LIMIT",
    "DEFAULT_CENTER_LIMIT",
    "DEFAULT_WINDOW_SIDE_M",
    "ExistingArea",
    "PlannerParcel",
    "VPA10_ALGORITHM_VERSION",
    "VirtualAreaPlan",
    "find_containing_area",
    "normalize_parcels",
    "plan_virtual_areas",
]
