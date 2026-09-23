"""按动态 10×10 km 窗口规划并构造一次性遥感下载任务。"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from app.core.config import settings
from app.core.geo import geojson_to_shape
from app.models.tables import Job, LandParcel
from app.schemas.satellite_batch import SatelliteBatchGroup
from app.services.virtual_area_planner import (
    DEFAULT_WINDOW_SIDE_M,
    VPA10_ALGORITHM_VERSION,
    plan_virtual_areas,
)


def satellite_land_geometry(land: LandParcel):
    """校验一次性遥感任务使用的地块边界，坏数据不进入几何规划。"""
    geom = geojson_to_shape(land.boundary_geojson)
    if (
        land.boundary_srid != 4326
        or geom is None
        or geom.is_empty
        or not geom.is_valid
        or geom.geom_type not in {"Polygon", "MultiPolygon"}
        or not all(math.isfinite(value) for value in geom.bounds)
        or geom.bounds[0] < -180
        or geom.bounds[2] > 180
        or geom.bounds[1] <= -90
        or geom.bounds[3] >= 90
        or geom.bounds[2] - geom.bounds[0] >= 180
    ):
        raise ValueError(f"地块{land.land_id}缺少有效的WGS84多边形边界")
    return geom


def group_satellite_lands(lands: Sequence[LandParcel]) -> list[SatelliteBatchGroup]:
    """每次按本次输入地块重新规划 10km 窗口，不读写项目区主数据。"""
    valid_lands = list(lands)
    for land in valid_lands:
        satellite_land_geometry(land)

    plans = plan_virtual_areas(valid_lands, window_side_m=DEFAULT_WINDOW_SIDE_M)
    return [
        SatelliteBatchGroup(
            anchor_land_id=plan.anchor_land_id,
            land_ids=list(plan.land_ids),
            aggregation_bbox=plan.aggregation_bbox,
            # 窗口边界来自规划器选出的动态中心，不再用地块中心重新居中。
            download_bbox=plan.aggregation_bbox,
            processing_boundary_geojson=plan.boundary_geojson,
            oversized=plan.oversized,
        )
        for plan in plans
    ]


def build_satellite_batch_jobs(
    lands: Sequence[LandParcel],
    *,
    date_from: date,
    date_to: date,
    sensors: Sequence[str],
    force: bool = False,
    parent_job_id: uuid.UUID | None = None,
    id_namespace: uuid.UUID | None = None,
    chunk_days: int | None = None,
    land_date_windows: Mapping[str, tuple[date, date]] | None = None,
    job_land_id: str | None = None,
    extra_params: Mapping[str, Any] | None = None,
) -> tuple[list[SatelliteBatchGroup], list[Job]]:
    """构造临时空间组任务；相同组/日期/传感器只读一次共享 COG 窗口。"""
    if date_from > date_to:
        raise ValueError("date_from must be no later than date_to")

    groups = group_satellite_lands(lands)
    jobs: list[Job] = []
    unique_sensors = list(dict.fromkeys(str(sensor) for sensor in sensors))
    chunk = max(
        int(settings.index_backfill_chunk_days if chunk_days is None else chunk_days), 1
    )
    date_windows = {str(key): value for key, value in (land_date_windows or {}).items()}

    for group in groups:
        group_windows = [date_windows[land_id] for land_id in group.land_ids if land_id in date_windows]
        group_date_from = min((window[0] for window in group_windows), default=date_from)
        group_date_to = max((window[1] for window in group_windows), default=date_to)
        if group_date_from > group_date_to:
            raise ValueError(f"分组{group.anchor_land_id}的日期范围无效")

        cursor = group_date_from
        member_key = ",".join(sorted(group.land_ids))
        while cursor <= group_date_to:
            end = min(cursor + timedelta(days=chunk - 1), group_date_to)
            for sensor in unique_sensors:
                if id_namespace is None:
                    job_id = uuid.uuid4()
                else:
                    job_id = uuid.uuid5(
                        id_namespace,
                        f"satellite-batch-10km:{group.anchor_land_id}:{member_key}:"
                        f"{sensor}:{cursor.isoformat()}:{end.isoformat()}",
                    )
                params = {
                    **dict(extra_params or {}),
                    "land_ids": list(group.land_ids),
                    "anchor_land_id": group.anchor_land_id,
                    "processing_window_km": DEFAULT_WINDOW_SIDE_M / 1000,
                    "processing_window_side_m": DEFAULT_WINDOW_SIDE_M,
                    "processing_boundary_geojson": group.processing_boundary_geojson,
                    "aggregation_bbox": list(group.aggregation_bbox),
                    "download_bbox": list(group.download_bbox),
                    "oversized": group.oversized,
                    "sensor": sensor,
                    "date_from": cursor.isoformat(),
                    "date_to": end.isoformat(),
                    "force": force,
                    "grouping_algorithm": VPA10_ALGORITHM_VERSION,
                }
                job = Job(
                    id=job_id,
                    land_id=job_land_id or group.anchor_land_id,
                    type="satellite_batch",
                    status="pending",
                    parent_job_id=parent_job_id,
                    params_json=params,
                )
                group.job_ids.append(str(job.id))
                jobs.append(job)
            cursor = end + timedelta(days=1)
    return groups, jobs


async def create_satellite_batch_jobs(
    db,
    lands: Sequence[LandParcel],
    **kwargs: Any,
) -> tuple[list[SatelliteBatchGroup], list[Job], dict[str, Any]]:
    """统一的 API 编排入口；只把下载 Job 写入队列，不持久化空间分组。"""
    groups, jobs = build_satellite_batch_jobs(lands, **kwargs)
    db.add_all(jobs)
    return groups, jobs, {
        "algorithm_version": VPA10_ALGORITHM_VERSION,
        "window_side_m": DEFAULT_WINDOW_SIDE_M,
    }


__all__ = [
    "build_satellite_batch_jobs",
    "create_satellite_batch_jobs",
    "group_satellite_lands",
    "satellite_land_geometry",
]
