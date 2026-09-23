"""10×10 km 虚拟项目区的数据库编排与任务构造。

规划器只处理几何，本模块负责把规划结果落到 ``virtual_project_areas``、
``virtual_project_area_lands``，并把一个项目区拆成日期/传感器共享下载 Job。
Smart 后续同步的地块先做“完整包含”匹配，只有无法复用已有项目区时才重新规划。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from numbers import Integral
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from shapely.geometry import shape
from shapely.strtree import STRtree

from app.core.config import settings
from app.models.tables import Job, LandParcel
from app.services.virtual_area_planner import (
    DEFAULT_WINDOW_SIDE_M,
    ExistingArea,
    PlannerParcel,
    VPA10_ALGORITHM_VERSION,
    VirtualAreaPlan,
    plan_virtual_areas,
)

VPA10_ASSIGNMENT_TYPE = "vpa10_greedy"
VPA10_SOURCE = "vpa10:greedy"
VPA10_HISTORY_YEARS = 5


def default_history_window(
    *, as_of: date | None = None, years: int = VPA10_HISTORY_YEARS
) -> tuple[date, date]:
    """返回默认历史窗口；按自然日回退，兼容 2 月 29 日。"""
    if years < 1:
        raise ValueError("years must be positive")
    end = as_of or date.today()
    try:
        start = end.replace(year=end.year - years)
    except ValueError:
        start = end.replace(year=end.year - years, day=28)
    return start, end


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plan_identity(plan: VirtualAreaPlan) -> tuple[str, str]:
    """用边界、成员和算法版本生成幂等项目区 ID，重试不会产生重复项目区。"""
    payload = {
        "algorithm_version": VPA10_ALGORITHM_VERSION,
        "window_side_m": plan.window_side_m,
        "anchor_land_id": plan.anchor_land_id,
        "land_ids": list(plan.land_ids),
        "boundary": plan.boundary_geojson,
    }
    plan_hash = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    return f"vpa10_{plan_hash[:32]}", plan_hash


async def load_land_snapshots(
    db: AsyncSession,
    land_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """读取规划所需的最小地块快照，避免把业务无关字段带入规划器。"""
    stmt = select(
        LandParcel.land_id,
        LandParcel.boundary_geojson,
        LandParcel.boundary_srid,
    ).where(LandParcel.deleted_at.is_(None))
    normalized = list(
        dict.fromkeys(
            str(value).strip() for value in (land_ids or ()) if str(value).strip()
        )
    )
    if normalized:
        stmt = stmt.where(LandParcel.land_id.in_(normalized))
    result = await db.execute(stmt)
    return [dict(row) for row in result.mappings().all()]


async def load_vpa10_areas(db: AsyncSession) -> list[dict[str, Any]]:
    """读取可复用的 v2 项目区及其当前状态。"""
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT tile_id, anchor_land_id, boundary_geojson, min_lon, min_lat,
                       max_lon, max_lat, status, source_properties, data_ready_ratio
                FROM agric_satellite.virtual_project_areas
                WHERE algorithm_version = :algorithm_version
                  AND coalesce(status, 'planned') <> 'archived'
                ORDER BY tile_id
                """
                ),
                {"algorithm_version": VPA10_ALGORITHM_VERSION},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


async def load_vpa10_assignments(db: AsyncSession) -> dict[str, str]:
    """返回 active v2 地块到项目区的唯一归属。"""
    rows = await load_vpa10_assignment_records(db)
    return {str(row["land_id"]): str(row["tile_id"]) for row in rows}


async def load_vpa10_assignment_records(db: AsyncSession) -> list[dict[str, Any]]:
    """读取 active 归属及几何指纹，便于 Smart 同步时发现边界已变化。"""
    rows = (
        (
            await db.execute(
                text(
                    """
                SELECT l.land_id, l.tile_id, l.geometry_hash,
                       l.containment_verified, l.last_verified_at
                FROM agric_satellite.virtual_project_area_lands l
                JOIN agric_satellite.virtual_project_areas a ON a.tile_id = l.tile_id
                WHERE l.algorithm_version = :algorithm_version
                  AND l.assignment_status = 'active'
                  AND a.algorithm_version = :algorithm_version
                  AND coalesce(a.status, 'planned') <> 'archived'
                """
                ),
                {"algorithm_version": VPA10_ALGORITHM_VERSION},
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _geometry_hash(boundary_geojson: Any, boundary_srid: Any = 4326) -> str:
    """对实际边界而非 land_id 做指纹，避免地块变更后沿用错误的项目区。"""
    boundary = boundary_geojson
    if isinstance(boundary, str):
        try:
            boundary = json.loads(boundary)
        except json.JSONDecodeError:
            boundary = {"raw": boundary}
    payload = {"srid": int(boundary_srid or 4326), "geometry": boundary}
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _planner_parcels(snapshots: Sequence[dict[str, Any]]) -> list[PlannerParcel]:
    parcels: list[PlannerParcel] = []
    for row in snapshots:
        land_id = str(row["land_id"])
        geometry = shape(row["boundary_geojson"])
        if (
            geometry.is_empty
            or not geometry.is_valid
            or geometry.geom_type not in {"Polygon", "MultiPolygon"}
        ):
            raise ValueError(f"地块 {land_id} 的 boundary_geojson 无效")
        if row.get("boundary_srid") not in (None, 4326):
            raise ValueError(f"地块 {land_id} 的 boundary_srid 必须是 EPSG:4326")
        parcels.append(PlannerParcel(land_id=land_id, geometry=geometry))
    return parcels


def _existing_area_objects(rows: Sequence[dict[str, Any]]) -> list[ExistingArea]:
    return [
        ExistingArea(tile_id=str(row["tile_id"]), geometry=row["boundary_geojson"])
        for row in rows
    ]


def _row_area_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "tile_id": str(row["tile_id"]),
        "anchor_land_id": str(row.get("anchor_land_id") or ""),
        "boundary_geojson": row["boundary_geojson"],
        "min_lon": float(row["min_lon"]),
        "min_lat": float(row["min_lat"]),
        "max_lon": float(row["max_lon"]),
        "max_lat": float(row["max_lat"]),
        "status": row.get("status") or "planned",
        "data_ready_ratio": float(row.get("data_ready_ratio") or 0),
        "source_properties": dict(row.get("source_properties") or {}),
    }


async def persist_vpa10_plans(
    db: AsyncSession,
    plans: Sequence[VirtualAreaPlan],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    assigned_by: str = "vpa10-planner",
    geometry_hash_by_land_id: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """幂等写入项目区主表与地块关系，并返回可下发任务的项目区快照。"""
    if not plans:
        return []

    # 同一 API 实例内的初始化/增量同步可能并发运行；锁只保护 v2 规划落库，
    # 防止两个事务同时给同一个新地块建立 active 归属。
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('vpa10-greedy-planner'))")
    )
    now = datetime.now(timezone.utc)
    output: list[dict[str, Any]] = []
    area_rows: list[dict[str, Any]] = []
    relation_rows: list[dict[str, Any]] = []
    for plan in plans:
        tile_id, plan_hash = _plan_identity(plan)
        props = {
            "algorithm_version": VPA10_ALGORITHM_VERSION,
            "protected_score": plan.protected_score,
            "compactness": plan.compactness,
            "fill_ratio": plan.fill_ratio,
            "overlap_ratio": plan.overlap_ratio,
            "oversized": plan.oversized,
            "land_ids": list(plan.land_ids),
        }
        area_rows.append(
            {
                "tile_id": tile_id,
                "project_key": tile_id,
                "anchor_land_id": plan.anchor_land_id,
                "assignment_type": VPA10_ASSIGNMENT_TYPE,
                "parcel_count": len(plan.land_ids),
                "tile_width_m": plan.window_side_m,
                "tile_height_m": plan.window_side_m,
                "boundary_geojson": _stable_json(plan.boundary_geojson),
                "boundary_srid": plan.boundary_srid,
                "min_lon": plan.aggregation_bbox[0],
                "min_lat": plan.aggregation_bbox[1],
                "max_lon": plan.aggregation_bbox[2],
                "max_lat": plan.aggregation_bbox[3],
                "source_properties": _stable_json(props),
                "source_file": VPA10_SOURCE,
                "source_feature_index": 0,
                "algorithm_version": VPA10_ALGORITHM_VERSION,
                "window_side_m": plan.window_side_m,
                "window_shape": "square",
                "planning_crs": plan.planning_crs,
                "grid_crs": "EPSG:4326",
                "center_x": plan.center_x,
                "center_y": plan.center_y,
                "status": "planned",
                "plan_hash": plan_hash,
                "data_from": date_from,
                "data_to": date_to,
                "data_ready_ratio": 0.0,
                "last_planned_at": now,
                "updated_at": now,
            }
        )
        relation_rows.extend(
            {
                "tile_id": tile_id,
                "land_id": land_id,
                "assignment_type": VPA10_ASSIGNMENT_TYPE,
                "is_anchor": land_id == plan.anchor_land_id,
                "coverage_ratio": 1.0,
                "source": assigned_by,
                "algorithm_version": VPA10_ALGORITHM_VERSION,
                "assignment_status": "active",
                "geometry_hash": (geometry_hash_by_land_id or {}).get(land_id)
                or hashlib.sha256(land_id.encode("utf-8")).hexdigest(),
                "containment_verified": True,
                "assigned_by": assigned_by,
                "assigned_at": now,
                "last_verified_at": now,
            }
            for land_id in plan.land_ids
        )

    await db.execute(
        text(
            """
            INSERT INTO agric_satellite.virtual_project_areas (
                tile_id, project_key, anchor_land_id, assignment_type, parcel_count,
                tile_width_m, tile_height_m, boundary_geojson, boundary_srid,
                min_lon, min_lat, max_lon, max_lat, source_properties, source_file,
                source_feature_index, algorithm_version, window_side_m, window_shape,
                planning_crs, grid_crs, center_x, center_y, status, plan_hash,
                data_from, data_to, data_ready_ratio, last_planned_at, updated_at
            ) VALUES (
                :tile_id, :project_key, :anchor_land_id, :assignment_type, :parcel_count,
                :tile_width_m, :tile_height_m,
                CAST(:boundary_geojson AS jsonb), :boundary_srid,
                :min_lon, :min_lat, :max_lon, :max_lat,
                CAST(:source_properties AS jsonb), :source_file, :source_feature_index,
                :algorithm_version, :window_side_m, :window_shape, :planning_crs,
                :grid_crs, :center_x, :center_y, :status, :plan_hash,
                :data_from, :data_to, :data_ready_ratio, :last_planned_at, :updated_at
            )
            ON CONFLICT (tile_id) DO UPDATE SET
                project_key = EXCLUDED.project_key,
                anchor_land_id = EXCLUDED.anchor_land_id,
                assignment_type = EXCLUDED.assignment_type,
                parcel_count = EXCLUDED.parcel_count,
                tile_width_m = EXCLUDED.tile_width_m,
                tile_height_m = EXCLUDED.tile_height_m,
                boundary_geojson = EXCLUDED.boundary_geojson,
                boundary_srid = EXCLUDED.boundary_srid,
                min_lon = EXCLUDED.min_lon, min_lat = EXCLUDED.min_lat,
                max_lon = EXCLUDED.max_lon, max_lat = EXCLUDED.max_lat,
                source_properties = EXCLUDED.source_properties,
                algorithm_version = EXCLUDED.algorithm_version,
                window_side_m = EXCLUDED.window_side_m,
                window_shape = EXCLUDED.window_shape,
                planning_crs = EXCLUDED.planning_crs,
                grid_crs = EXCLUDED.grid_crs,
                center_x = EXCLUDED.center_x, center_y = EXCLUDED.center_y,
                status = CASE
                    WHEN agric_satellite.virtual_project_areas.status IN ('active', 'ready')
                        THEN agric_satellite.virtual_project_areas.status
                    ELSE EXCLUDED.status
                END,
                plan_hash = EXCLUDED.plan_hash,
                data_from = coalesce(EXCLUDED.data_from, agric_satellite.virtual_project_areas.data_from),
                data_to = coalesce(EXCLUDED.data_to, agric_satellite.virtual_project_areas.data_to),
                updated_at = EXCLUDED.updated_at,
                last_planned_at = EXCLUDED.last_planned_at
            """
        ),
        area_rows,
    )
    await db.execute(
        text(
            """
            INSERT INTO agric_satellite.virtual_project_area_lands (
                tile_id, land_id, assignment_type, is_anchor, coverage_ratio, source,
                algorithm_version, assignment_status, geometry_hash, containment_verified,
                assigned_by, assigned_at, last_verified_at, updated_at
            ) VALUES (
                :tile_id, :land_id, :assignment_type, :is_anchor, :coverage_ratio, :source,
                :algorithm_version, :assignment_status, :geometry_hash, :containment_verified,
                :assigned_by, :assigned_at, :last_verified_at, :updated_at
            )
            ON CONFLICT (tile_id, land_id) DO UPDATE SET
                assignment_type = EXCLUDED.assignment_type,
                is_anchor = EXCLUDED.is_anchor,
                coverage_ratio = EXCLUDED.coverage_ratio,
                source = EXCLUDED.source,
                algorithm_version = EXCLUDED.algorithm_version,
                assignment_status = EXCLUDED.assignment_status,
                geometry_hash = EXCLUDED.geometry_hash,
                containment_verified = EXCLUDED.containment_verified,
                assigned_by = EXCLUDED.assigned_by,
                assigned_at = EXCLUDED.assigned_at,
                last_verified_at = EXCLUDED.last_verified_at,
                updated_at = EXCLUDED.updated_at
            """
        ),
        [{**row, "updated_at": now} for row in relation_rows],
    )
    for row in area_rows:
        output.append(
            {
                "tile_id": row["tile_id"],
                "anchor_land_id": row["anchor_land_id"],
                "boundary_geojson": json.loads(row["boundary_geojson"]),
                "min_lon": row["min_lon"],
                "min_lat": row["min_lat"],
                "max_lon": row["max_lon"],
                "max_lat": row["max_lat"],
                "status": "planned",
                "data_ready_ratio": 0.0,
                "source_properties": json.loads(row["source_properties"]),
                "land_ids": [
                    item["land_id"]
                    for item in relation_rows
                    if item["tile_id"] == row["tile_id"]
                ],
            }
        )
    return output


async def prepare_vpa10_areas(
    db: AsyncSession,
    snapshots: Sequence[dict[str, Any]],
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    assigned_by: str = "vpa10-planner",
    include_all_existing: bool = False,
) -> dict[str, Any]:
    """匹配已有项目区并规划未匹配地块；调用方决定是否创建下载 Job。"""
    # 把“读取未归属地块 → 规划 → 写关系”包在同一事务锁中，避免两个
    # Smart 增量请求同时看到同一个未归属地块并触发 active 唯一索引冲突。
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('vpa10-greedy-planner'))")
    )
    existing_rows = await load_vpa10_areas(db)
    assignment_rows = await load_vpa10_assignment_records(db)
    assignment_by_land = {
        str(row["land_id"]): str(row["tile_id"]) for row in assignment_rows
    }
    assignment_row_by_land = {str(row["land_id"]): row for row in assignment_rows}
    existing_objects = _existing_area_objects(existing_rows)
    existing_geometries = [shape(row["boundary_geojson"]) for row in existing_rows]
    existing_tree = STRtree(existing_geometries) if existing_geometries else None
    existing_identity = {
        id(geometry): index for index, geometry in enumerate(existing_geometries)
    }
    existing_by_tile = {
        str(row["tile_id"]): _row_area_record(row) for row in existing_rows
    }
    unassigned: list[dict[str, Any]] = []
    direct_assignments: dict[str, str] = {}
    already_assigned_area_ids: set[str] = set()
    stale_assignment_land_ids: list[str] = []
    stale_area_ids: set[str] = set()
    geometry_hash_by_land_id = {
        str(row["land_id"]): _geometry_hash(
            row["boundary_geojson"], row.get("boundary_srid")
        )
        for row in snapshots
    }
    # 完整包含命中已有项目区时直接复用，不再产生第二个重叠窗口。
    for snapshot in snapshots:
        land_id = str(snapshot["land_id"])
        if land_id in assignment_by_land:
            tile_id = assignment_by_land[land_id]
            area = existing_by_tile.get(tile_id)
            parcel_geometry = shape(snapshot["boundary_geojson"])
            stored = assignment_row_by_land.get(land_id) or {}
            boundary_still_contains = bool(
                area and shape(area["boundary_geojson"]).covers(parcel_geometry)
            )
            fingerprint_matches = (
                stored.get("geometry_hash") == geometry_hash_by_land_id[land_id]
            )
            if boundary_still_contains and fingerprint_matches:
                already_assigned_area_ids.add(tile_id)
                continue
            # Smart 同步可能更新了地块边界；先解除旧 active 关系，再让规划器
            # 为当前几何重新选择项目区，避免旧唯一索引阻止新关系写入。
            stale_assignment_land_ids.append(land_id)
            stale_area_ids.add(tile_id)
            assignment_by_land.pop(land_id, None)
            unassigned.append(snapshot)
            continue
        geometry = snapshot["boundary_geojson"]
        parcel_geometry = shape(geometry)
        candidate_positions: list[int]
        if existing_tree is None:
            candidate_positions = []
        else:
            candidate_positions = []
            for value in existing_tree.query(parcel_geometry):
                if isinstance(value, Integral):
                    candidate_positions.append(int(value))
                else:
                    position = existing_identity.get(id(value))
                    if position is not None:
                        candidate_positions.append(position)
        containing = [
            existing_objects[index]
            for index in candidate_positions
            if shape(existing_objects[index].geometry).covers(parcel_geometry)
        ]
        if containing:
            chosen = min(containing, key=lambda area: area.tile_id)
            direct_assignments[land_id] = chosen.tile_id
        else:
            unassigned.append(snapshot)

    if stale_assignment_land_ids:
        await db.execute(
            text(
                """
                UPDATE agric_satellite.virtual_project_area_lands
                SET assignment_status = 'stale', containment_verified = false,
                    last_verified_at = now(), updated_at = now()
                WHERE algorithm_version = :algorithm_version
                  AND assignment_status = 'active'
                  AND land_id IN :land_ids
                """
            ).bindparams(bindparam("land_ids", expanding=True)),
            {
                "algorithm_version": VPA10_ALGORITHM_VERSION,
                "land_ids": stale_assignment_land_ids,
            },
        )
        await db.execute(
            text(
                """
                UPDATE agric_satellite.virtual_project_areas a
                SET parcel_count = (
                        SELECT count(*)
                        FROM agric_satellite.virtual_project_area_lands l
                        WHERE l.tile_id = a.tile_id
                          AND l.algorithm_version = :algorithm_version
                          AND l.assignment_status = 'active'
                    ),
                    updated_at = now()
                WHERE a.tile_id IN :tile_ids
                """
            ).bindparams(bindparam("tile_ids", expanding=True)),
            {
                "algorithm_version": VPA10_ALGORITHM_VERSION,
                "tile_ids": sorted(stale_area_ids),
            },
        )

    existing_land_ids: dict[str, list[str]] = {}
    for land_id, tile_id in assignment_by_land.items():
        existing_land_ids.setdefault(tile_id, []).append(land_id)
    for tile_id, members in existing_land_ids.items():
        if tile_id in existing_by_tile:
            existing_by_tile[tile_id]["land_ids"] = sorted(members)

    plans = await asyncio.to_thread(
        plan_virtual_areas,
        _planner_parcels(unassigned),
        existing_areas=existing_objects,
        window_side_m=DEFAULT_WINDOW_SIDE_M,
    )
    persisted = await persist_vpa10_plans(
        db,
        plans,
        date_from=date_from,
        date_to=date_to,
        assigned_by=assigned_by,
        geometry_hash_by_land_id=geometry_hash_by_land_id,
    )

    # 直接匹配的地块也必须写关系，后续查询才能以唯一 active 归属命中。
    if direct_assignments:
        now = datetime.now(timezone.utc)
        rows = [
            {
                "tile_id": tile_id,
                "land_id": land_id,
                "assignment_type": VPA10_ASSIGNMENT_TYPE,
                "is_anchor": False,
                "coverage_ratio": 1.0,
                "source": assigned_by,
                "algorithm_version": VPA10_ALGORITHM_VERSION,
                "assignment_status": "active",
                "geometry_hash": geometry_hash_by_land_id[land_id],
                "containment_verified": True,
                "assigned_by": assigned_by,
                "assigned_at": now,
                "last_verified_at": now,
                "updated_at": now,
            }
            for land_id, tile_id in direct_assignments.items()
        ]
        await db.execute(
            text(
                """
                INSERT INTO agric_satellite.virtual_project_area_lands (
                    tile_id, land_id, assignment_type, is_anchor, coverage_ratio, source,
                    algorithm_version, assignment_status, geometry_hash, containment_verified,
                    assigned_by, assigned_at, last_verified_at, updated_at
                ) VALUES (
                    :tile_id, :land_id, :assignment_type, :is_anchor, :coverage_ratio, :source,
                    :algorithm_version, :assignment_status, :geometry_hash, :containment_verified,
                    :assigned_by, :assigned_at, :last_verified_at, :updated_at
                )
                ON CONFLICT (tile_id, land_id) DO UPDATE SET
                    assignment_status = EXCLUDED.assignment_status,
                    geometry_hash = EXCLUDED.geometry_hash,
                    containment_verified = EXCLUDED.containment_verified,
                    assigned_by = EXCLUDED.assigned_by,
                    assigned_at = EXCLUDED.assigned_at,
                    last_verified_at = EXCLUDED.last_verified_at,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            rows,
        )
        await db.execute(
            text(
                """
                UPDATE agric_satellite.virtual_project_areas a
                SET parcel_count = (
                        SELECT count(*) FROM agric_satellite.virtual_project_area_lands l
                        WHERE l.tile_id = a.tile_id
                          AND l.algorithm_version = :algorithm_version
                          AND l.assignment_status = 'active'
                    ),
                    updated_at = now()
                WHERE a.tile_id IN :tile_ids
                """
            ).bindparams(bindparam("tile_ids", expanding=True)),
            {
                "tile_ids": list(set(direct_assignments.values())),
                "algorithm_version": VPA10_ALGORITHM_VERSION,
            },
        )

    area_ids = (
        already_assigned_area_ids
        | set(direct_assignments.values())
        | {str(row["tile_id"]) for row in persisted}
    )
    if include_all_existing:
        area_ids.update(existing_by_tile)
    for tile_id in area_ids:
        existing_by_tile.setdefault(tile_id, {"tile_id": tile_id, "land_ids": []})
        existing_by_tile[tile_id].setdefault("land_ids", [])
    for land_id, tile_id in direct_assignments.items():
        if land_id not in existing_by_tile[tile_id]["land_ids"]:
            existing_by_tile[tile_id]["land_ids"].append(land_id)
    for row in persisted:
        existing_by_tile[row["tile_id"]] = row

    return {
        "plans": plans,
        "new_areas": persisted,
        "matched_land_ids": sorted(direct_assignments),
        "area_ids": sorted(area_ids),
        "areas": [existing_by_tile[tile_id] for tile_id in sorted(area_ids)],
        "existing_area_count": len(existing_rows),
    }


async def create_vpa10_download_jobs(
    db: AsyncSession,
    areas: Sequence[dict[str, Any]],
    *,
    date_from: date,
    date_to: date,
    sensors: Sequence[str] = ("S1", "S2"),
    force: bool = False,
    parent_job_id: uuid.UUID,
    chunk_days: int | None = None,
) -> list[Job]:
    """一个项目区对应共享窗口下载 Job；Job 内携带精确动态边界。"""
    if date_from > date_to:
        raise ValueError("date_from must be no later than date_to")
    chunk = max(int(chunk_days or settings.index_backfill_chunk_days), 1)
    unique_sensors = list(dict.fromkeys(str(sensor) for sensor in sensors))
    jobs: list[Job] = []
    for area in areas:
        tile_id = str(area["tile_id"])
        land_ids = list(dict.fromkeys(str(value) for value in area.get("land_ids", [])))
        if not land_ids:
            continue
        anchor = str(area.get("anchor_land_id") or land_ids[0])
        props = dict(area.get("source_properties") or {})
        cursor = date_from
        while cursor <= date_to:
            end = min(cursor + timedelta(days=chunk - 1), date_to)
            for sensor in unique_sensors:
                job_id = uuid.uuid5(
                    parent_job_id,
                    f"vpa10:{tile_id}:{sensor}:{cursor.isoformat()}:{end.isoformat()}",
                )
                jobs.append(
                    Job(
                        id=job_id,
                        land_id=anchor,
                        type="satellite_batch",
                        status="pending",
                        parent_job_id=parent_job_id,
                        params_json={
                            "land_ids": land_ids,
                            "anchor_land_id": anchor,
                            "virtual_area": True,
                            "virtual_area_tile_id": tile_id,
                            "processing_window_km": 10.0,
                            "processing_window_side_m": DEFAULT_WINDOW_SIDE_M,
                            "processing_boundary_geojson": area["boundary_geojson"],
                            "aggregation_bbox": [
                                area["min_lon"],
                                area["min_lat"],
                                area["max_lon"],
                                area["max_lat"],
                            ],
                            "oversized": bool(props.get("oversized", False)),
                            "sensor": sensor,
                            "date_from": cursor.isoformat(),
                            "date_to": end.isoformat(),
                            "force": force,
                        },
                    )
                )
            cursor = end + timedelta(days=1)
    db.add_all(jobs)
    return jobs


async def initialize_virtual_areas(
    *,
    land_ids: Sequence[str] | None = None,
    parent_job_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, Any]:
    """初始化项目区主数据；默认只建区，不立即拉取历史影像。"""
    from app.core.database import async_session

    execution_id = parent_job_id or uuid.uuid4()
    async with async_session() as db:
        snapshots = await load_land_snapshots(db, land_ids)
        if land_ids and len(snapshots) != len(set(str(value) for value in land_ids)):
            missing = sorted(
                set(str(value) for value in land_ids)
                - {str(row["land_id"]) for row in snapshots}
            )
            raise ValueError(f"land parcels not found: {', '.join(missing[:20])}")
        prepared = await prepare_vpa10_areas(
            db,
            snapshots,
            date_from=date_from,
            date_to=date_to,
            assigned_by="vpa10-initialize",
        )
        parent = Job(
            id=execution_id,
            type="virtual_area_initialize",
            status="completed",
            progress_json={
                "stage": "planned",
                "land_count": len(snapshots),
                "new_area_count": len(prepared["new_areas"]),
                "matched_land_count": len(prepared["matched_land_ids"]),
                "area_count": len(prepared["area_ids"]),
            },
            params_json={"land_ids": [row["land_id"] for row in snapshots]},
        )
        db.add(parent)
        await db.commit()
    return {
        "status": "completed",
        "parent_job_id": str(execution_id),
        "land_count": len(snapshots),
        "new_area_count": len(prepared["new_areas"]),
        "matched_land_count": len(prepared["matched_land_ids"]),
        "area_count": len(prepared["area_ids"]),
        "area_ids": prepared["area_ids"],
    }


async def backfill_virtual_area_history(
    *,
    land_ids: Sequence[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    years: int = VPA10_HISTORY_YEARS,
    sensors: Sequence[str] = ("S1", "S2"),
    force: bool = False,
    parent_job_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """为项目区创建五年历史共享下载任务；同项目区只下载一次窗口。"""
    from app.core.database import async_session
    from app.mq_publish import publish_api_task

    if date_from is None or date_to is None:
        # 允许调度器传入 as_of；起始日期必须相对同一个业务日计算，
        # 否则补历史快照时会把窗口错误地锚定到机器当前日期。
        default_from, default_to = default_history_window(as_of=date_to, years=years)
        date_from = date_from or default_from
        date_to = date_to or default_to
    execution_id = parent_job_id or uuid.uuid4()
    async with async_session() as db:
        snapshots = await load_land_snapshots(db, land_ids)
        prepared = await prepare_vpa10_areas(
            db,
            snapshots,
            date_from=date_from,
            date_to=date_to,
            assigned_by="vpa10-history",
            include_all_existing=not land_ids,
        )
        jobs = await create_vpa10_download_jobs(
            db,
            prepared["areas"],
            date_from=date_from,
            date_to=date_to,
            sensors=sensors,
            force=force,
            parent_job_id=execution_id,
        )
        parent = Job(
            id=execution_id,
            type="virtual_area_history_backfill",
            status="pending",
            progress_json={
                "stage": "queued",
                "land_count": len(snapshots),
                "area_count": len(prepared["areas"]),
                "job_count": len(jobs),
            },
            params_json={
                "land_ids": [row["land_id"] for row in snapshots],
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "sensors": list(sensors),
                "force": force,
                "virtual_area_ids": prepared["area_ids"],
            },
        )
        db.add(parent)
        await db.commit()

        failed_job_ids: list[str] = []
        for job in jobs:
            try:
                await asyncio.to_thread(
                    publish_api_task,
                    type="satellite_batch",
                    land_id=job.land_id,
                    task_id=str(job.id),
                    extras={"job_id": str(job.id)},
                )
            except Exception as exc:
                job.status = "failed"
                job.error = f"虚拟项目区历史任务派发失败：{str(exc)[:3900]}"
                failed_job_ids.append(str(job.id))
        parent.status = "partial" if failed_job_ids else "running"
        parent.progress_json = {
            **(parent.progress_json or {}),
            "stage": "dispatched",
            "queued_count": len(jobs) - len(failed_job_ids),
            "failed_count": len(failed_job_ids),
        }
        await db.commit()

    return {
        "status": "partial" if failed_job_ids else "queued",
        "parent_job_id": str(execution_id),
        "land_count": len(snapshots),
        "area_count": len(prepared["areas"]),
        "job_count": len(jobs),
        "queued_job_ids": [
            str(job.id) for job in jobs if str(job.id) not in failed_job_ids
        ],
        "failed_job_ids": failed_job_ids,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "area_ids": prepared["area_ids"],
    }


__all__ = [
    "VPA10_ASSIGNMENT_TYPE",
    "VPA10_HISTORY_YEARS",
    "backfill_virtual_area_history",
    "create_vpa10_download_jobs",
    "default_history_window",
    "initialize_virtual_areas",
    "load_land_snapshots",
    "load_vpa10_areas",
    "load_vpa10_assignments",
    "load_vpa10_assignment_records",
    "persist_vpa10_plans",
    "prepare_vpa10_areas",
]
