"""历史遥感回填编排；地块集合每次都临时规划 10km 下载窗口。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.core.config import settings
from app.models.tables import Job, LandParcel
from app.services.satellite_batch import (  # noqa: F401
    create_satellite_batch_jobs,
    find_invalid_satellite_lands,
    satellite_land_geometry,
)

DEFAULT_HISTORY_YEARS = 5
# 当前光学产品算法版本；须与 ingest/app/tasks/agri_lonlat.py 的
# AGRI_OPTICAL_ALGORITHM_VERSION 保持一致（有测试校验）。
CURRENT_S2_ALGORITHM_VERSION = "stac-optical-lonlat-v5"


def default_history_window(
    *, as_of: date | None = None, years: int = DEFAULT_HISTORY_YEARS
) -> tuple[date, date]:
    """按自然年生成历史范围；闰日回溯到目标年的 2 月 28 日。"""
    if years < 1:
        raise ValueError("years must be positive")
    end = as_of or date.today()
    try:
        start = end.replace(year=end.year - years)
    except ValueError:
        start = end.replace(year=end.year - years, day=28)
    return start, end


async def backfill_satellite_history(
    *,
    land_ids: Sequence[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    years: int = DEFAULT_HISTORY_YEARS,
    sensors: Sequence[str] = ("S1", "S2"),
    force: bool = False,
    parent_job_id: uuid.UUID | None = None,
    redownload_stale_versions: bool = False,
) -> dict[str, Any]:
    """下发历史 S1/S2 回填；共享范围只在 Job 参数中暂存，不写 OSS 或分组表。"""
    from app.core.database import async_session
    if date_from is None or date_to is None:
        default_from, default_to = default_history_window(as_of=date_to, years=years)
        date_from = date_from or default_from
        date_to = date_to or default_to
    if date_from > date_to:
        raise ValueError("date_from must be no later than date_to")
    if redownload_stale_versions and force:
        raise ValueError("redownload_stale_versions 与 force 不能同时使用")
    # 下载机据此只把“当前算法版本”的景算作已有日期，旧版本（旧网格）景会被重下。
    extra_params = (
        {"existing_algorithm_version": CURRENT_S2_ALGORITHM_VERSION}
        if redownload_stale_versions
        else None
    )

    requested = list(dict.fromkeys(str(value).strip() for value in (land_ids or ()) if str(value).strip()))
    execution_id = parent_job_id or uuid.uuid4()
    async with async_session() as db:
        # 全量历史回填只需要ID和几何规划字段，避免把来源/土壤等大JSON列全部读入API进程。
        stmt = select(LandParcel).options(
            load_only(
                LandParcel.land_id,
                LandParcel.boundary_geojson,
                LandParcel.boundary_srid,
            )
        ).where(LandParcel.deleted_at.is_(None))
        if land_ids is not None:
            stmt = stmt.where(LandParcel.land_id.in_(requested))
        lands = list((await db.execute(stmt.order_by(LandParcel.land_id))).scalars().all())

        valid_lands: list[LandParcel] = []
        skipped_land_ids: list[str] = []
        invalid_ids = await find_invalid_satellite_lands(lands)
        for land in lands:
            if str(land.land_id) in invalid_ids:
                skipped_land_ids.append(str(land.land_id))
                continue
            valid_lands.append(land)

        groups, jobs, _ = await create_satellite_batch_jobs(
            db,
            valid_lands,
            date_from=date_from,
            date_to=date_to,
            sensors=sensors,
            force=force,
            parent_job_id=execution_id,
            chunk_days=settings.index_backfill_chunk_days,
            extra_params=extra_params,
        )
        found_ids = {str(land.land_id) for land in lands}
        missing_land_ids = [land_id for land_id in requested if land_id not in found_ids]
        parent = Job(
            id=execution_id,
            type="satellite_history_backfill",
            status="running" if jobs else "completed",
            progress_json={
                "stage": "queued" if jobs else "completed",
                "land_count": len(valid_lands),
                "group_count": len(groups),
                "job_count": len(jobs),
                "queued_count": len(jobs),
                "failed_count": 0,
            },
            params_json={
                "land_ids": [str(land.land_id) for land in valid_lands],
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "sensors": list(sensors),
                "force": force,
                "redownload_stale_versions": redownload_stale_versions,
                "grouping_algorithm": "dynamic-window-greedy-10km-v1",
            },
        )
        db.add(parent)
        await db.commit()

    result_status = "queued" if jobs else "completed"
    return {
        "status": result_status,
        "parent_job_id": str(execution_id),
        "land_count": len(valid_lands),
        "skipped_land_count": len(skipped_land_ids),
        "skipped_land_ids": skipped_land_ids,
        "missing_land_ids": missing_land_ids,
        "group_count": len(groups),
        "job_count": len(jobs),
        "queued_job_ids": [str(job.id) for job in jobs],
        "failed_job_ids": [],
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }


__all__ = ["DEFAULT_HISTORY_YEARS", "backfill_satellite_history", "default_history_window"]
