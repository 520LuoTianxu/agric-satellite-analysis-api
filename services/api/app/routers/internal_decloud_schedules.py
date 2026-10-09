"""下载机 S2 去云排程 Outbox 的受保护写入、租约领取和确认接口。"""

from __future__ import annotations

import math
import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import Job, SatelliteDecloudScheduleOutbox

router = APIRouter(
    prefix="/internal/decloud-schedules", tags=["internal-decloud-schedules"]
)

# 同一条语句完成领取与租约更新；SKIP LOCKED允许多个dispatcher分批并行，
# 过期的processing租约也可重新领取，避免worker异常退出后排程永久卡住。
_CLAIM_SCHEDULES = text("""
    WITH eligible AS (
        SELECT schedule_key
        FROM agric_satellite.satellite_decloud_schedule_outbox
        WHERE (
            (status = 'pending' AND available_at <= now())
            OR (status = 'processing' AND lease_until <= now())
        )
          -- 空值代表领取所有可用排程；显式类型转换避免PostgreSQL无法推断NULL参数类型。
          AND (CAST(:schedule_key AS text) IS NULL OR schedule_key = :schedule_key)
        ORDER BY available_at, created_at
        FOR UPDATE SKIP LOCKED
        LIMIT :limit
    )
    UPDATE agric_satellite.satellite_decloud_schedule_outbox AS outbox
    SET status = 'processing',
        attempts = outbox.attempts + 1,
        lease_owner = :worker_id,
        lease_until = now() + make_interval(secs => :lease_seconds),
        updated_at = now()
    FROM eligible
    WHERE outbox.schedule_key = eligible.schedule_key
    RETURNING
        outbox.schedule_key,
        outbox.job_id,
        outbox.land_id,
        outbox.date_from,
        outbox.date_to,
        outbox.raw_results,
        outbox.mq_task_id,
        outbox.season_months,
        outbox.crop_type,
        outbox.attempts
""")

# 完成时同时核对租约持有人，作为fencing保护：旧worker不能覆盖新worker重新领取后的状态。
_COMPLETE_SCHEDULE = text("""
    UPDATE agric_satellite.satellite_decloud_schedule_outbox
    SET status = 'completed',
        lease_owner = NULL,
        lease_until = NULL,
        last_error = NULL,
        completed_at = now(),
        updated_at = now()
    WHERE schedule_key = :schedule_key
      AND status = 'processing'
      AND lease_owner = :worker_id
    RETURNING schedule_key
""")

# 失败后释放租约并按5秒起步、指数增长且最多1小时的间隔重试；attempts在领取时递增。
_FAIL_SCHEDULE = text("""
    UPDATE agric_satellite.satellite_decloud_schedule_outbox
    SET status = 'pending',
        available_at = now() + make_interval(
            secs => LEAST(3600, 5 * power(2, LEAST(attempts - 1, 10))::integer)
        ),
        lease_owner = NULL,
        lease_until = NULL,
        last_error = :error,
        updated_at = now()
    WHERE schedule_key = :schedule_key
      AND status = 'processing'
      AND lease_owner = :worker_id
    RETURNING schedule_key, attempts, available_at
""")


class DecloudScheduleCreate(BaseModel):
    schedule_key: str = Field(min_length=1, max_length=256)
    job_id: uuid.UUID
    land_id: str = Field(min_length=1, max_length=64)
    date_from: date
    date_to: date
    raw_results: list[dict[str, Any]] = Field(min_length=1, max_length=2000)
    mq_task_id: str | None = Field(default=None, max_length=256)
    season_months: list[int] | None = Field(default=None, max_length=12)
    crop_type: str | None = Field(default=None, max_length=100)


class DecloudScheduleClaim(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    schedule_key: str | None = Field(default=None, min_length=1, max_length=256)
    limit: int = Field(default=25, ge=1, le=100)
    lease_seconds: int = Field(default=900, ge=30, le=3600)


class DecloudScheduleComplete(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)


class DecloudScheduleFail(DecloudScheduleComplete):
    error: str = Field(min_length=1, max_length=2000)


class DecloudScheduleRow(BaseModel):
    schedule_key: str
    job_id: uuid.UUID
    land_id: str
    date_from: date
    date_to: date
    raw_results: list[dict[str, Any]]
    mq_task_id: str | None = None
    season_months: list[int] | None = None
    crop_type: str | None = None
    attempts: int


class DecloudScheduleClaimOut(BaseModel):
    items: list[DecloudScheduleRow]


def _normalize_raw_results(
    raw_results: list[dict[str, Any]], *, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    """只保留去云择景需要的有限元数据，避免 Outbox 携带产品或像元大对象。

    日期窗口和云量范围在API侧再次核验，防止内部调用方把越界或非法值写入可重试的排程意图。
    """
    normalized: list[dict[str, Any]] = []
    for item in raw_results:
        raw_date = str(item.get("date") or "").strip()[:10]
        try:
            product_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="raw scene date must be ISO format"
            ) from exc
        if product_date < date_from or product_date > date_to:
            raise HTTPException(
                status_code=422, detail="raw scene date is outside the schedule window"
            )

        row: dict[str, Any] = {"date": product_date.isoformat()}
        scene_id = str(item.get("scene_id") or item.get("raw_scene_id") or "").strip()
        if scene_id:
            row["scene_id"] = scene_id[:512]

        for output_key, aliases in (
            ("cloud_cover", ("cloud_cover", "stac_cloud")),
            (
                "parcel_cloud_cover_pct",
                ("parcel_cloud_cover_pct", "parcel_cloud"),
            ),
        ):
            value = next((item.get(alias) for alias in aliases if alias in item), None)
            if value is None:
                continue
            if isinstance(value, bool):
                raise HTTPException(
                    status_code=422, detail=f"{output_key} must be numeric"
                )
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422, detail=f"{output_key} must be numeric"
                ) from exc
            if not math.isfinite(number) or not 0 <= number <= 100:
                raise HTTPException(
                    status_code=422, detail=f"{output_key} must be between 0 and 100"
                )
            row[output_key] = number

        cloud_over_30 = item.get("cloud_cover_over_30", item.get("cloud_over_30"))
        if cloud_over_30 is not None:
            if not isinstance(cloud_over_30, bool):
                raise HTTPException(
                    status_code=422, detail="cloud_cover_over_30 must be boolean"
                )
            row["cloud_cover_over_30"] = cloud_over_30
        normalized.append(row)

    return normalized


@router.post("")
async def create_decloud_schedule(
    body: DecloudScheduleCreate,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if body.date_from > body.date_to:
        raise HTTPException(status_code=422, detail="date_from must not exceed date_to")
    if body.season_months is not None and any(
        month < 1 or month > 12 for month in body.season_months
    ):
        raise HTTPException(status_code=422, detail="season_months values must be 1-12")

    job = await db.get(Job, body.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    job_params = job.params_json if isinstance(job.params_json, dict) else {}
    # 排程类型以已落库任务参数为准，不能只信worker请求体，避免给非S2任务挂接去云工作。
    if str(job_params.get("sensor") or "").upper() != "S2":
        raise HTTPException(
            status_code=422, detail="decloud schedule requires an S2 job"
        )
    land_id = body.land_id.strip()
    if not land_id:
        raise HTTPException(status_code=422, detail="land_id must not be blank")

    raw_results = _normalize_raw_results(
        body.raw_results,
        date_from=body.date_from,
        date_to=body.date_to,
    )
    statement = (
        insert(SatelliteDecloudScheduleOutbox)
        .values(
            schedule_key=body.schedule_key,
            job_id=body.job_id,
            land_id=land_id,
            date_from=body.date_from,
            date_to=body.date_to,
            raw_results=raw_results,
            mq_task_id=body.mq_task_id,
            season_months=body.season_months,
            crop_type=body.crop_type,
        )
        .on_conflict_do_nothing(index_elements=["schedule_key"])
        .returning(SatelliteDecloudScheduleOutbox.schedule_key)
    )
    inserted_key = (await db.execute(statement)).scalar_one_or_none()
    await db.commit()

    row = await db.get(SatelliteDecloudScheduleOutbox, body.schedule_key)
    if row is None:
        raise HTTPException(
            status_code=500, detail="decloud schedule was not persisted"
        )
    # 同一幂等键必须代表完全相同的排程意图；否则不能静默沿用旧窗口或作物参数。
    if not inserted_key and (
        row.job_id != body.job_id
        or row.land_id != land_id
        or row.date_from != body.date_from
        or row.date_to != body.date_to
        or row.raw_results != raw_results
        or row.mq_task_id != body.mq_task_id
        or row.season_months != body.season_months
        or row.crop_type != body.crop_type
    ):
        raise HTTPException(status_code=409, detail="schedule_key payload mismatch")
    return {
        "schedule_key": row.schedule_key,
        "status": row.status,
        "created": bool(inserted_key),
    }


@router.post("/claim", response_model=DecloudScheduleClaimOut)
async def claim_decloud_schedules(
    body: DecloudScheduleClaim,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        _CLAIM_SCHEDULES,
        {
            "schedule_key": body.schedule_key,
            "worker_id": body.worker_id,
            "limit": 1 if body.schedule_key else body.limit,
            "lease_seconds": body.lease_seconds,
        },
    )
    rows = [dict(row) for row in result.mappings().all()]
    await db.commit()
    return {"items": rows}


@router.post("/{schedule_key}/complete")
async def complete_decloud_schedule(
    schedule_key: str,
    body: DecloudScheduleComplete,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    completed = (
        await db.execute(
            _COMPLETE_SCHEDULE,
            {"schedule_key": schedule_key, "worker_id": body.worker_id},
        )
    ).scalar_one_or_none()
    if completed is None:
        row = await db.get(SatelliteDecloudScheduleOutbox, schedule_key)
        if row is None:
            raise HTTPException(status_code=404, detail="decloud schedule not found")
        if row.status != "completed":
            raise HTTPException(
                status_code=409, detail="decloud schedule lease is not owned"
            )
    await db.commit()
    return {"schedule_key": schedule_key, "status": "completed"}


@router.post("/{schedule_key}/fail")
async def fail_decloud_schedule(
    schedule_key: str,
    body: DecloudScheduleFail,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    failed = await db.execute(
        _FAIL_SCHEDULE,
        {
            "schedule_key": schedule_key,
            "worker_id": body.worker_id,
            "error": body.error,
        },
    )
    row = failed.mappings().first()
    if row is None:
        existing = await db.get(SatelliteDecloudScheduleOutbox, schedule_key)
        if existing is None:
            raise HTTPException(status_code=404, detail="decloud schedule not found")
        if existing.status == "completed":
            await db.commit()
            return {"schedule_key": schedule_key, "status": "completed"}
        if existing.status != "processing" or existing.lease_owner != body.worker_id:
            raise HTTPException(
                status_code=409, detail="decloud schedule lease is not owned"
            )
    await db.commit()
    return {
        "schedule_key": schedule_key,
        "status": "pending",
        "attempts": row["attempts"] if row else None,
        "available_at": row["available_at"] if row else None,
    }


@router.get("/health/pending")
async def decloud_schedule_pending_count(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        text("""
            SELECT status, count(*) AS count
            FROM agric_satellite.satellite_decloud_schedule_outbox
            GROUP BY status
        """)
    )
    counts = {"pending": 0, "processing": 0, "completed": 0}
    counts.update({row["status"]: int(row["count"]) for row in result.mappings().all()})
    return counts
