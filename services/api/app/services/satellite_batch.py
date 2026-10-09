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
    max_jobs: int | None = None,
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
    extra = dict(extra_params or {})
    target_dates_by_land: dict[str, list[date]] = {}
    targeted_dates = "target_dates_by_land" in extra
    if targeted_dates:
        raw_targets = extra.get("target_dates_by_land")
        if not isinstance(raw_targets, Mapping):
            raise ValueError("target_dates_by_land must be a mapping of land IDs to dates")
        for land_id, values in raw_targets.items():
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                raise ValueError(f"地块{land_id}的目标日期必须为日期列表")
            parsed = {
                date.fromisoformat(str(value)[:10])
                for value in values
            }
            if parsed:
                target_dates_by_land[str(land_id)] = sorted(parsed)

    # 先按每个空间组的实际日期窗口估算任务数，超限时在构造 Job 前快速拒绝。
    planned_groups: list[
        tuple[SatelliteBatchGroup, date, date, str, dict[str, list[date]]]
    ] = []
    estimated_job_count = 0
    for group in groups:
        group_targets = {
            land_id: [
                target_date
                for target_date in target_dates_by_land.get(land_id, [])
                if date_from <= target_date <= date_to
            ]
            for land_id in group.land_ids
            if land_id in target_dates_by_land
        }
        group_targets = {land_id: dates for land_id, dates in group_targets.items() if dates}
        group_target_dates = [target_date for dates in group_targets.values() for target_date in dates]
        if targeted_dates and not group_target_dates:
            continue

        group_windows = [
            date_windows[land_id]
            for land_id in group.land_ids
            if land_id in date_windows
        ]
        if targeted_dates:
            group_date_from = min(group_target_dates)
            group_date_to = max(group_target_dates)
        else:
            group_date_from = min((window[0] for window in group_windows), default=date_from)
            group_date_to = max((window[1] for window in group_windows), default=date_to)
        if group_date_from > group_date_to:
            raise ValueError(f"分组{group.anchor_land_id}的日期范围无效")
        member_key = ",".join(sorted(group.land_ids))
        planned_groups.append(
            (group, group_date_from, group_date_to, member_key, group_targets)
        )
        if targeted_dates:
            chunk_offsets = {
                (target_date - group_date_from).days // chunk
                for target_date in group_target_dates
            }
            chunk_count = len(chunk_offsets)
        else:
            day_count = (group_date_to - group_date_from).days + 1
            chunk_count = (day_count + chunk - 1) // chunk
        estimated_job_count += chunk_count * len(unique_sensors)

    job_limit = max(int(settings.satellite_batch_max_jobs), 1)
    if max_jobs is not None:
        if max_jobs < 1:
            raise ValueError("max_jobs must be a positive integer")
        job_limit = min(job_limit, int(max_jobs))
    if estimated_job_count > job_limit:
        raise ValueError(
            f"本批次预计创建{estimated_job_count}个遥感任务，超过单批上限{job_limit}；"
            "请缩短日期范围、减少地块/传感器，或拆成多个批次提交"
        )

    for group, group_date_from, group_date_to, member_key, group_targets in planned_groups:
        cursor = group_date_from
        while cursor <= group_date_to:
            end = min(cursor + timedelta(days=chunk - 1), group_date_to)
            chunk_targets = {
                land_id: [
                    target_date.isoformat()
                    for target_date in target_dates
                    if cursor <= target_date <= end
                ]
                for land_id, target_dates in group_targets.items()
                if any(cursor <= target_date <= end for target_date in target_dates)
            }
            if targeted_dates and not chunk_targets:
                cursor = end + timedelta(days=1)
                continue
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
                    **extra,
                    **(
                        {"target_dates_by_land": chunk_targets}
                        if targeted_dates
                        else {}
                    ),
                    "land_ids": list(chunk_targets) if targeted_dates else list(group.land_ids),
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
    *,
    dispatch_priority: int | None = None,
    dispatch_extras: Mapping[str, Any] | None = None,
    durable_dispatch: bool = True,
    **kwargs: Any,
) -> tuple[list[SatelliteBatchGroup], list[Job], dict[str, Any]]:
    """统一创建空间批任务，并与持久队列/Outbox原子保存派发意图。"""
    groups, jobs = build_satellite_batch_jobs(lands, **kwargs)
    db.add_all(jobs)
    await stage_satellite_batch_dispatches(
        db,
        jobs,
        priority=dispatch_priority,
        extras=dispatch_extras,
        enabled=durable_dispatch,
    )
    return groups, jobs, {
        "algorithm_version": VPA10_ALGORITHM_VERSION,
        "window_side_m": DEFAULT_WINDOW_SIDE_M,
    }


async def stage_satellite_batch_dispatches(
    db,
    jobs: Sequence[Job],
    *,
    priority: int | None = None,
    extras: Mapping[str, Any] | None = None,
    enabled: bool = True,
) -> None:
    """在Job同一事务内创建claim队列项或MQ Outbox，避免提交后遗失派发意图。"""
    if not jobs or not enabled:
        return

    from agric_satellite_analysis_common.task_priority import (
        BACKGROUND_TASK_PRIORITY,
        normalize_task_priority,
    )
    from agric_satellite_analysis_common.trace import stamp_trace_on_payload
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.tables import SatelliteBatchDispatchOutbox, WorkItem
    from app.services.work_items import (
        should_enqueue_work_items,
        should_publish_mq,
    )

    resolved_priority = normalize_task_priority(
        BACKGROUND_TASK_PRIORITY if priority is None else priority
    )
    base_extras = dict(extras or {})
    item_rows: list[dict[str, Any]] = []
    outbox_rows: list[SatelliteBatchDispatchOutbox] = []

    for job in jobs:
        task_id = str(job.id)
        # 任务标识由服务端Job决定，调用方附加信息不能覆盖幂等键。
        task_extras = {**base_extras, "job_id": task_id, "priority": resolved_priority}
        job.progress_json = {
            **(job.progress_json or {}),
            "dispatch_status": "queued",
            "dispatch_attempts": 0,
        }
        payload = stamp_trace_on_payload(
            {
                "land_id": str(job.land_id),
                "extras": task_extras,
                "task_id": task_id,
            }
        )
        if should_enqueue_work_items():
            item_rows.append(
                {
                    "type": "satellite_batch",
                    # 与既有publish_api_task路径相同，WorkItem按子Job ID检索任务树。
                    "parent_job_id": job.id,
                    "payload_json": payload,
                    "status": "pending",
                    "priority": resolved_priority,
                    "attempts": 0,
                    "idempotency_key": f"satellite_batch:{task_id}",
                }
            )
        if should_publish_mq():
            outbox_rows.append(
                SatelliteBatchDispatchOutbox(
                    task_id=job.id,
                    land_id=str(job.land_id),
                    extras_json=task_extras,
                    priority=resolved_priority,
                )
            )

    if item_rows:
        # 大批量回填最多可生成数千子任务，分块避免单条INSERT超过PostgreSQL参数上限。
        for offset in range(0, len(item_rows), 500):
            stmt = (
                pg_insert(WorkItem)
                .values(item_rows[offset : offset + 500])
                .on_conflict_do_nothing(index_elements=[WorkItem.idempotency_key])
            )
            await db.execute(stmt)
    if outbox_rows:
        db.add_all(outbox_rows)


__all__ = [
    "build_satellite_batch_jobs",
    "create_satellite_batch_jobs",
    "group_satellite_lands",
    "satellite_land_geometry",
    "stage_satellite_batch_dispatches",
]
