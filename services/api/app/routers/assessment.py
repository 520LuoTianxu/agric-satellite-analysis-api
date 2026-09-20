# -*- coding: utf-8 -*-
"""Land assessment (选地体检) PDF report endpoints."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.task_priority import INTERACTIVE_REPORT_PRIORITY
from app.core.config import settings
from app.core.database import get_db
from app.core.logging import logger
from app.core.rate_limit import limiter
from app.core.storage import get_storage
from app.middleware.auth import OrgContext, get_org_context, require_roles, org_scope
from app.models.tables import LandParcel, Job
from app.reports.land_assessment.scorecard_view import scorecard_public_view
from app.reports.land_assessment.window import resolve_assessment_window
from app.schemas.monitoring import JobOut
from app.schemas.satellite_batch import SatelliteBatchGroup
from app.services.cdfinance_report_prefetch import (
    land_has_site_admission,
    normalize_optional_group_id,
    normalize_optional_hr_base_id,
    prefetch_cdfinance_for_report,
    resolve_request_token,
)
from app.services.mysql_land_sync import sync_selected_lands
from app.services.report_urls import report_progress_for_response, signed_report_url
from app.services.satellite_batch import build_satellite_batch_jobs
from pydantic import (
    AliasChoices,
    BaseModel,
    Field as PydanticField,
    field_validator,
    model_validator,
)


class AssessmentGenerateRequest(BaseModel):
    """Crop bind + optional historical window + one-click data pull."""

    crop_type: str | None = PydanticField(
        default=None,
        description="Catalog key from GET /v1/crops; binds to field if provided/missing",
    )
    date_from: str | None = PydanticField(
        default=None,
        description="YYYY-MM-DD start of historical window; date_to is always today",
    )
    years: int | None = PydanticField(
        default=None,
        ge=1,
        le=20,
        description="If date_from omitted, start = today − years (default 3)",
    )
    pull_data: bool = PydanticField(
        default=True,
        description="Queue weather + RS indices + soil bootstrap before PDF",
    )
    cdfinance_token: str | None = PydanticField(
        default=None,
        description="Temporary cdfinance H5 Bearer for site-admission / NPK prefetch",
    )
    token: str | None = PydanticField(
        default=None,
        description="Alias of cdfinance_token",
    )
    group_id: str | int | None = PydanticField(
        default=None,
        description="cdfinance groupId for groupSiteAdmission questionnaire",
    )
    hr_base_id: str | int | None = PydanticField(
        default=None,
        description="Override CDFINANCE_HR_BASE_ID for site-admission / NPK headers",
    )

    @field_validator("date_from", mode="before")
    @classmethod
    def _empty_date_from(cls, v: Any) -> Any:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @model_validator(mode="after")
    def _validate_date_from_iso(self) -> "AssessmentGenerateRequest":
        if self.date_from:
            try:
                date.fromisoformat(self.date_from[:10])
            except ValueError as e:
                raise ValueError("date_from must be YYYY-MM-DD") from e
            self.date_from = self.date_from[:10]
        return self


class AssessmentBatchRequest(BaseModel):
    """批量选地报告请求；地块边界先从 Smart 源库同步再统一拉取遥感。"""

    land_ids: list[
        Annotated[str, PydanticField(min_length=1, max_length=64)]
    ] = PydanticField(
        min_length=1,
        max_length=1000,
        validation_alias=AliasChoices("landIdList", "landIdlist", "land_ids"),
        serialization_alias="landIdList",
    )
    crop_type: str | None = PydanticField(
        default=None,
        description="批量地块共用作物；不传时使用各地块已有 crop_type",
    )
    date_from: str | None = PydanticField(
        default=None, description="YYYY-MM-DD；不传时按 years 回溯"
    )
    years: int | None = PydanticField(default=None, ge=1, le=20)
    sensors: list[Literal["S1", "S2"]] = PydanticField(
        default_factory=lambda: ["S1", "S2"], min_length=1, max_length=2
    )
    force: bool = PydanticField(
        default=False, description="是否重新拉取已有日期的遥感产品"
    )

    @field_validator("land_ids", mode="before")
    @classmethod
    def _normalize_land_ids(cls, value: Any) -> Any:
        # 前端可能传数字编号或重复编号；统一成 Smart/遥感主表使用的字符串键。
        if isinstance(value, list):
            if len(value) > 1000:
                raise ValueError("landIdList最多包含1000个地块")
            if any(
                isinstance(item, bool) or not isinstance(item, (str, int))
                for item in value
            ):
                raise ValueError("landIdList必须包含字符串或整数编号")
            normalized = [str(item).strip() for item in value]
            if any(not item for item in normalized):
                raise ValueError("landIdList不能包含空编号")
            return list(dict.fromkeys(normalized))
        return value

    @field_validator("sensors", mode="before")
    @classmethod
    def _unique_sensors(cls, value: Any) -> Any:
        if isinstance(value, list):
            return list(dict.fromkeys(value))
        return value

    @field_validator("date_from", mode="before")
    @classmethod
    def _empty_date_from(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @model_validator(mode="after")
    def _validate_date_from_iso(self) -> "AssessmentBatchRequest":
        if self.date_from:
            try:
                date.fromisoformat(self.date_from[:10])
            except ValueError as exc:
                raise ValueError("date_from must be YYYY-MM-DD") from exc
            self.date_from = self.date_from[:10]
        return self


class AssessmentBatchReportOut(BaseModel):
    job_id: uuid.UUID
    land_id: str
    status: str
    progress_json: dict[str, Any] | None = None
    error: str | None = None


class AssessmentBatchResponse(BaseModel):
    batch_id: uuid.UUID
    status: str
    land_ids: list[str]
    date_from: date
    date_to: date
    group_count: int
    satellite_job_count: int
    report_job_count: int
    groups: list[SatelliteBatchGroup]
    reports: list[AssessmentBatchReportOut]


def _job_response_with_report_url(job: Job) -> JobOut:
    """返回 Job 时动态签名 OSS 报告地址，不把临时链接写回数据库。"""
    result = JobOut.model_validate(job)
    result.progress_json = report_progress_for_response(result.progress_json)
    return result


class AssessmentDimensionOut(BaseModel):
    key: str
    score: float
    light: str | None = None
    weight: str | None = None


class AssessmentOverallOut(BaseModel):
    score: float
    grade: str | None = None
    light: str | None = None
    one_liner: str | None = None


class AssessmentConfidenceOut(BaseModel):
    score: float


class AssessmentAiReferenceOut(BaseModel):
    """Optional AI reference score — never an admission / program overall."""

    score: float
    grade: str | None = None
    light: str | None = None
    rationale: str | None = None
    disclaimer: str = "AI参考分 · 不可作为准入结论"


class AssessmentScorecardOut(BaseModel):
    """Six-dimension land-assessment scorecard from a succeeded report job."""

    job_id: uuid.UUID
    overall: AssessmentOverallOut
    dimensions: list[AssessmentDimensionOut]
    confidence: AssessmentConfidenceOut | None = None
    ai_reference: AssessmentAiReferenceOut | None = None
    generated_at: datetime | None = None


router = APIRouter()
_writer = require_roles("owner", "admin", "member")
_ASSESSMENT_BATCH_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "agric-satellite/assessment-report-batch"
)


async def _get_field(land_id: str, org_id: uuid.UUID, db: AsyncSession) -> LandParcel:
    field = await db.get(LandParcel, land_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="LandParcel not found")
    return field


async def _maybe_enqueue_assessment_work(
    db,
    *,
    job_id: str,
    land_id: str,
    extras: dict,
    priority: int = INTERACTIVE_REPORT_PRIORITY,
) -> str | None:
    """Insert work_items row when WORK_QUEUE_MODE is claim|dual."""
    from app.services.work_items import enqueue_work_item, should_enqueue_work_items

    if not should_enqueue_work_items():
        return None
    payload = {
        "land_id": land_id,
        "extras": dict(extras),
    }
    item = await enqueue_work_item(
        db,
        type="assessment_report",
        payload=payload,
        priority=priority,
        idempotency_key=f"assessment_report:{job_id}",
    )
    await db.commit()
    return str(item.id)


def _assessment_batch_id(body: AssessmentBatchRequest, *, date_from: str, date_to: str, crop_type: str | None) -> uuid.UUID:
    """根据请求内容生成稳定批次 ID，重复点击不会重复创建下载任务。"""
    key = json.dumps(
        {
            "land_ids": sorted(body.land_ids),
            "crop_type": crop_type or "",
            "date_from": date_from,
            "date_to": date_to,
            "sensors": sorted(body.sensors),
            "force": body.force,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid.uuid5(_ASSESSMENT_BATCH_NAMESPACE, key)


async def _assessment_batch_response(
    db: AsyncSession, batch_id: uuid.UUID
) -> AssessmentBatchResponse:
    parent = await db.get(Job, batch_id)
    if not parent or parent.type != "assessment_batch":
        raise HTTPException(status_code=404, detail="Assessment batch not found")

    children = (
        await db.execute(
            select(Job)
            .where(Job.parent_job_id == batch_id)
            .order_by(Job.created_at.asc(), Job.id.asc())
        )
    ).scalars().all()
    params = parent.params_json or {}
    groups = [
        SatelliteBatchGroup.model_validate(item)
        for item in (params.get("groups") or [])
    ]
    reports = [
        AssessmentBatchReportOut(
            job_id=job.id,
            land_id=str(job.land_id),
            status=job.status,
            progress_json=report_progress_for_response(job.progress_json),
            error=job.error,
        )
        for job in children
        if job.type == "assessment_report" and job.land_id
    ]
    report_statuses = [item.status for item in reports]
    failed = sum(status in {"failed", "cancelled"} for status in report_statuses)
    succeeded = sum(status == "succeeded" for status in report_statuses)
    if failed and succeeded:
        batch_status = "partial"
    elif failed:
        batch_status = "failed"
    elif reports and succeeded == len(reports):
        batch_status = "succeeded"
    elif parent.status in {"failed", "cancelled"}:
        batch_status = parent.status
    elif parent.status in {"running", "completed"} or any(
        job.status in {"running", "completed"} for job in children
    ):
        batch_status = "running"
    else:
        batch_status = "queued"

    return AssessmentBatchResponse(
        batch_id=batch_id,
        status=batch_status,
        land_ids=[str(value) for value in (params.get("land_ids") or [])],
        date_from=date.fromisoformat(str(params["date_from"])[:10]),
        date_to=date.fromisoformat(str(params["date_to"])[:10]),
        group_count=len(groups),
        satellite_job_count=sum(job.type == "satellite_batch" for job in children),
        report_job_count=len(reports),
        groups=groups,
        reports=reports,
    )


@router.post(
    "/lands/assessment-reports/batch",
    response_model=AssessmentBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("2/minute")
async def create_assessment_reports_batch(
    request: Request,
    body: AssessmentBatchRequest,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """同步 Smart 指定地块后，共享遥感窗口并逐地块生成选地报告。"""
    del ctx  # 权限依赖仍需执行；当前认证关闭时不需要读取上下文内容。
    from app.core.crops import crop_name_zh, normalize_crop_key, require_crop_key

    try:
        date_from, date_to, weather_days, years_used = resolve_assessment_window(
            date_from=body.date_from,
            years=body.years,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    requested_crop = normalize_crop_key(body.crop_type)
    if body.crop_type and not requested_crop:
        try:
            requested_crop = require_crop_key(body.crop_type)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    batch_id = _assessment_batch_id(
        body, date_from=date_from, date_to=date_to, crop_type=requested_crop
    )
    existing = await db.get(Job, batch_id)
    if existing:
        return await _assessment_batch_response(db, batch_id)

    if not settings.mysql_source_enabled:
        raise HTTPException(
            status_code=503,
            detail="Smart/MySQL source is not enabled; set MYSQL_SOURCE_ENABLED=true",
        )

    try:
        source_summary = await sync_selected_lands(body.land_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("assessment_batch_smart_sync_failed", batch_id=str(batch_id))
        raise HTTPException(status_code=503, detail="Smart 地块数据同步失败") from exc

    sync_status = source_summary.get("status")
    if sync_status == "skipped_locked":
        raise HTTPException(status_code=409, detail="Smart 地块同步正在进行，请稍后重试")
    if sync_status == "not_found":
        raise HTTPException(
            status_code=404,
            detail={"missing_land_ids": source_summary.get("missing_land_ids", [])},
        )
    if sync_status in {"filtered", "invalid"}:
        raise HTTPException(
            status_code=422,
            detail={
                "filtered_land_ids": source_summary.get("filtered_land_ids", []),
                "invalid_land_ids": source_summary.get("invalid_land_ids", []),
            },
        )
    if sync_status != "completed":
        raise HTTPException(status_code=503, detail="Smart 地块数据同步未完成")

    lands = (
        await db.execute(
            select(LandParcel).where(
                LandParcel.land_id.in_(body.land_ids),
                LandParcel.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    lands_by_id = {str(land.land_id): land for land in lands}
    missing_after_sync = [land_id for land_id in body.land_ids if land_id not in lands_by_id]
    if missing_after_sync:
        raise HTTPException(status_code=404, detail={"missing_land_ids": missing_after_sync})
    ordered_lands = [lands_by_id[land_id] for land_id in body.land_ids]

    missing_crops: list[str] = []
    land_crops: dict[str, str] = {}
    for land in ordered_lands:
        crop_key = requested_crop or normalize_crop_key(land.crop_type)
        if not crop_key:
            missing_crops.append(str(land.land_id))
            continue
        land_crops[str(land.land_id)] = crop_key
        if land.crop_type != crop_key:
            # Smart 源库的 planting_type 语义不强行映射；报告批次明确的公共作物
            # 或地块已有作物才写入平台标准 crop_type，保证后续物候计算一致。
            land.crop_type = crop_key
    if missing_crops:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "crop_required",
                "message": "请传入 crop_type，或先为所有地块绑定作物",
                "missing_land_ids": missing_crops,
            },
        )

    try:
        groups, satellite_jobs = await asyncio.to_thread(
            build_satellite_batch_jobs,
            ordered_lands,
            date_from=date.fromisoformat(date_from),
            date_to=date.fromisoformat(date_to),
            sensors=body.sensors,
            force=body.force,
            parent_job_id=batch_id,
            id_namespace=batch_id,
            chunk_days=settings.index_backfill_chunk_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    report_jobs: list[Job] = []
    for land in ordered_lands:
        land_id = str(land.land_id)
        report_job_id = uuid.uuid5(batch_id, f"assessment_report:{land_id}")
        report_jobs.append(
            Job(
                id=report_job_id,
                land_id=land_id,
                type="assessment_report",
                status="pending",
                parent_job_id=batch_id,
                params_json={
                    "kind": "land_assessment_plain",
                    "batch_id": str(batch_id),
                    "crop_type": land_crops[land_id],
                    "crop_name_zh": crop_name_zh(land_crops[land_id]),
                    "date_from": date_from,
                    "date_to": date_to,
                    "years": years_used,
                    "pull_data": True,
                    "shared_satellite_batch": True,
                },
            )
        )

    parent = Job(
        id=batch_id,
        type="assessment_batch",
        status="pending",
        progress_json={
            "stage": "queued",
            "land_count": len(ordered_lands),
            "group_count": len(groups),
            "satellite_job_count": len(satellite_jobs),
            "report_job_count": len(report_jobs),
        },
        params_json={
            "land_ids": [str(land.land_id) for land in ordered_lands],
            "date_from": date_from,
            "date_to": date_to,
            "years": years_used,
            "sensors": list(body.sensors),
            "force": body.force,
            "source": "smart_mysql_selected",
            "groups": [group.model_dump(mode="json") for group in groups],
            "satellite_job_ids": [str(job.id) for job in satellite_jobs],
            "report_job_ids": [str(job.id) for job in report_jobs],
        },
    )
    db.add(parent)
    for job in satellite_jobs:
        db.add(job)
    for job in report_jobs:
        db.add(job)
    await db.commit()

    try:
        from app.mq_publish import publish_api_task

        # 先派发共享遥感任务，再派发天气/土壤和报告 follow-up，
        # 确保报告任务天然等待同一批次的遥感覆盖，而不是重复逐地块下载。
        for job in satellite_jobs:
            task_id = str(job.id)
            mq_task_id = await asyncio.to_thread(
                publish_api_task,
                type="satellite_batch",
                land_id=str(job.land_id),
                task_id=task_id,
                extras={"job_id": task_id, "assessment_batch_id": str(batch_id)},
                priority=INTERACTIVE_REPORT_PRIORITY,
            )
            job.params_json = {
                **(job.params_json or {}),
                "dispatch_status": "queued",
                "mq_task_id": mq_task_id,
            }

        report_by_land = {str(job.land_id): job for job in report_jobs}
        for land in ordered_lands:
            land_id = str(land.land_id)
            report_job = report_by_land[land_id]
            report_mq_task_id = uuid.uuid5(batch_id, f"report_mq:{land_id}")
            bootstrap_task_id = uuid.uuid5(batch_id, f"bootstrap:{land_id}")
            await asyncio.to_thread(
                publish_api_task,
                type="land_bootstrap",
                land_id=land_id,
                task_id=str(bootstrap_task_id),
                priority=INTERACTIVE_REPORT_PRIORITY,
                extras={
                    "date_from": date_from,
                    "date_to": date_to,
                    "days": weather_days,
                    "weather_days": weather_days,
                    "source": "assessment_batch",
                    "skip_indices": True,
                    "followup_assessment": {
                        "job_id": str(report_job.id),
                        "mq_task_id": str(report_mq_task_id),
                        "crop_type": land_crops[land_id],
                        "crop_name_zh": crop_name_zh(land_crops[land_id]),
                        "date_from": date_from,
                        "date_to": date_to,
                        "years": years_used,
                    },
                },
            )
            report_job.params_json = {
                **(report_job.params_json or {}),
                "dispatch_status": "queued",
                "bootstrap_task_id": str(bootstrap_task_id),
                "mq_task_id": str(report_mq_task_id),
            }

        parent.status = "running"
        parent.progress_json = {
            **(parent.progress_json or {}),
            "stage": "dispatched",
            "percent": 5,
        }
        await db.commit()
    except Exception as exc:
        logger.exception("assessment_batch_dispatch_failed", batch_id=str(batch_id))
        parent.status = "failed"
        parent.error = "批量选地报告任务派发失败"
        parent.progress_json = {
            **(parent.progress_json or {}),
            "stage": "dispatch_failed",
        }
        for job in satellite_jobs + report_jobs:
            if (job.params_json or {}).get("dispatch_status") != "queued":
                job.status = "failed"
                job.error = "批量任务派发失败"
        await db.commit()
        raise HTTPException(status_code=503, detail="批量选地报告任务派发失败") from exc

    return await _assessment_batch_response(db, batch_id)


@router.get(
    "/lands/assessment-reports/batch/{batch_id}",
    response_model=AssessmentBatchResponse,
)
async def get_assessment_reports_batch(
    batch_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """返回批量选地报告的聚合状态和每块地的 PDF Job 状态。"""
    del ctx
    return await _assessment_batch_response(db, batch_id)


@router.post(
    "/lands/{land_id}/assessment-report",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("5/minute")
async def create_assessment_report(
    request: Request,
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: AssessmentGenerateRequest | None = None,
    authorization: Annotated[str | None, Header()] = None,
):
    """Enqueue data pulls (optional) + 选地体检（白话版）PDF generation.

    Never refuses generation merely because weather / soil / RS are incomplete;
    missing series are scored with available data inside the PDF worker.
    """
    field = await _get_field(land_id, ctx.org_id, db)

    from app.core.crops import crop_name_zh, normalize_crop_key, require_crop_key

    req = body or AssessmentGenerateRequest()
    crop_key = normalize_crop_key(field.crop_type)
    requested = req.crop_type or None
    crop_changed = False
    if requested:
        try:
            new_key = require_crop_key(requested)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        if crop_key != new_key:
            field.crop_type = new_key
            crop_changed = True
            await db.flush()
        crop_key = new_key
    if not crop_key:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "crop_required",
                "message": "请先选择作物后再生成选地报告",
                "crops_path": "/v1/crops",
            },
        )

    try:
        date_from, date_to, weather_days, years_used = resolve_assessment_window(
            date_from=req.date_from,
            years=req.years,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    pull_data = bool(req.pull_data)

    # Soft cdfinance prefetch (site admission + NPK) before enqueue — never blocks PDF.
    cdfinance_token = resolve_request_token(
        cdfinance_token=req.cdfinance_token,
        token=req.token,
        authorization=authorization,
    )
    group_id = normalize_optional_group_id(req.group_id)
    hr_base_id = normalize_optional_hr_base_id(req.hr_base_id)
    cdfinance_prefetch: dict[str, Any] | None = None
    if cdfinance_token:
        cdfinance_prefetch = await prefetch_cdfinance_for_report(
            db,
            field,
            token=cdfinance_token,
            group_id=group_id,
            hr_base_id=hr_base_id,
            force=True,
        )
        await db.flush()
    elif not await land_has_site_admission(db, land_id):
        # Soft hint only — UI may toast; generation still proceeds.
        logger.info(
            "assessment_site_admission_missing",
            land_id=str(land_id),
            hint="pass cdfinance_token + group_id to prefetch questionnaire",
        )

    # Reuse in-flight job if one is pending/running
    existing = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.land_id == land_id,
                Job.type == "assessment_report",
                Job.status.in_(("pending", "running")),
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        # Persist crop bind even when PDF job is already in flight
        if crop_changed:
            await db.commit()
        return existing

    job = Job(
        land_id=land_id,
        type="assessment_report",
        status="pending",
        params_json={
            "kind": "land_assessment_plain",
            "crop_type": crop_key,
            "crop_name_zh": crop_name_zh(crop_key),
            "date_from": date_from,
            "date_to": date_to,
            "years": years_used,
            "pull_data": pull_data,
            "weather_days": weather_days,
            # Never store Bearer token; only soft status for debugging.
            "cdfinance_prefetch": cdfinance_prefetch,
            "group_id": group_id,
            "hr_base_id": hr_base_id,
        },
    )
    db.add(job)
    await db.flush()

    await db.commit()

    try:
        from app.mq_publish import publish_api_task

        assessment_extras: dict[str, Any] = {
            "job_id": str(job.id),
            "crop_type": crop_key,
            "crop_name_zh": crop_name_zh(crop_key),
            "date_from": date_from,
            "date_to": date_to,
            "years": years_used,
            "pull_data": pull_data,
            "cdfinance_prefetched": bool(
                cdfinance_prefetch and cdfinance_prefetch.get("token_provided")
            ),
            "group_id": group_id,
            "hr_base_id": hr_base_id,
        }
        # pull_data → land_bootstrap(+followup) only. Do not also enqueue a naked
        # assessment_report work_item (claim would race PDF ahead of pulls).
        if not pull_data:
            work_id = await _maybe_enqueue_assessment_work(
                db,
                job_id=str(job.id),
                land_id=str(land_id),
                extras=assessment_extras,
                priority=INTERACTIVE_REPORT_PRIORITY,
            )
            if work_id:
                logger.info(
                    "assessment_work_item_enqueued",
                    job_id=str(job.id),
                    work_id=work_id,
                    pull_data=False,
                )

        if pull_data:
            # Do NOT publish assessment_report in parallel — that raced PDF ahead of
            # weather/RS (soil-only reports). Bootstrap fans out canonical pulls and
            # enqueues assessment as followup after Celery ids are known.
            # publish_api_task also inserts work_items when dual|claim (D4).
            assessment_mq_task_id = str(uuid.uuid4())
            bootstrap_extras: dict[str, Any] = {
                "date_from": date_from,
                "date_to": date_to,
                "days": weather_days,
                "weather_days": weather_days,
                "years": years_used,
                "source": "assessment_one_click",
                "with_bridge": True,
                "followup_assessment": {
                    "job_id": str(job.id),
                    "mq_task_id": assessment_mq_task_id,
                    "crop_type": crop_key,
                    "crop_name_zh": crop_name_zh(crop_key),
                    "date_from": date_from,
                    "date_to": date_to,
                    "years": years_used,
                    "cdfinance_prefetched": bool(
                        cdfinance_prefetch and cdfinance_prefetch.get("token_provided")
                    ),
                    "group_id": group_id,
                    "hr_base_id": hr_base_id,
                },
            }
            bootstrap_task_id = publish_api_task(
                type="land_bootstrap",
                land_id=str(land_id),
                extras=bootstrap_extras,
                priority=INTERACTIVE_REPORT_PRIORITY,
            )
            logger.info(
                "assessment_bootstrap_dispatched",
                job_id=str(job.id),
                land_id=str(land_id),
                mq_task_id=bootstrap_task_id,
                assessment_mq_task_id=assessment_mq_task_id,
                date_from=date_from,
                date_to=date_to,
                weather_days=weather_days,
                pull_data=True,
            )
        else:
            mq_task_id = publish_api_task(
                type="assessment_report",
                land_id=str(land_id),
                extras=assessment_extras,
                priority=INTERACTIVE_REPORT_PRIORITY,
            )
            logger.info(
                "assessment_job_dispatched",
                job_id=str(job.id),
                land_id=str(land_id),
                mq_task_id=mq_task_id,
                date_from=date_from,
                date_to=date_to,
                pull_data=False,
            )
    except Exception as e:
        logger.error(
            "assessment_job_dispatch_failed",
            job_id=str(job.id),
            error=str(e),
        )
        # Re-open session state after commit for failure marking
        job = await db.get(Job, job.id) or job
        job.status = "failed"
        job.error = f"dispatch failed: {e}"
        await db.commit()

    return job


@router.get("/lands/{land_id}/assessment-report/latest")
async def get_latest_assessment_report(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Download the latest succeeded assessment PDF for a field."""
    await _get_field(land_id, ctx.org_id, db)

    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.land_id == land_id,
                Job.type == "assessment_report",
                Job.status == "succeeded",
            )
            .order_by(Job.finished_at.desc().nullslast(), Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="No assessment report yet")

    progress = job.progress_json or {}
    object_key = progress.get("object_key")
    if not object_key:
        raise HTTPException(status_code=404, detail="Report file missing")

    storage = get_storage()
    if not storage.exists(object_key):
        raise HTTPException(
            status_code=404, detail="Report object not found in storage"
        )

    # 仍由 API 读取并流式返回，避免下载机或浏览器因私有 OSS ACL 直接被拒绝；
    # 如需直连，响应头会提供短期签名 URL。
    data = storage.get_bytes(object_key)
    filename = progress.get("filename") or "选地分析报告.pdf"
    # RFC 5987 for Chinese filenames
    disp = (
        f"attachment; filename=\"assessment.pdf\"; filename*=UTF-8''{quote(filename)}"
    )
    headers = {
        "Content-Disposition": disp,
        "X-Assessment-Job-Id": str(job.id),
        "X-Assessment-Score": str(progress.get("score", "")),
    }
    public_url = signed_report_url(object_key)
    if public_url:
        headers["X-Assessment-Public-Url"] = str(public_url)
    return Response(content=data, media_type="application/pdf", headers=headers)


async def _latest_report_job_for_meta(
    db: AsyncSession,
    *,
    ctx: OrgContext,
    land_id: str,
    job_type: str,
) -> Job | None:
    """Latest job for UI polling, without letting zombie failures shadow PDFs.

    - pending/running: return the in-flight job (polling).
    - failed/cancelled newest + a succeeded job with object_key: return succeeded
      so download UI binds to a downloadable report.
    - otherwise: return the newest job by created_at.
    """
    latest = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.land_id == land_id,
                Job.type == job_type,
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not latest:
        return None
    if latest.status in ("pending", "running"):
        return latest
    if latest.status in ("failed", "cancelled"):
        succeeded = (
            await db.execute(
                select(Job)
                .where(
                    org_scope(None, ctx),
                    Job.land_id == land_id,
                    Job.type == job_type,
                    Job.status == "succeeded",
                )
                .order_by(Job.finished_at.desc().nullslast(), Job.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if succeeded:
            progress = succeeded.progress_json or {}
            if progress.get("object_key"):
                return succeeded
    return latest


@router.get("/assessment-reports/latest/meta", response_model=JobOut)
async def get_latest_available_assessment_meta(
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """首页默认展示最近完成且有 PDF 的选地报告。"""
    # 关联地块排除已删除地块的报告，权限沿用现有报告接口的请求上下文。
    job = (
        await db.execute(
            select(Job)
            .join(LandParcel, LandParcel.land_id == Job.land_id)
            .where(
                org_scope(None, ctx),
                LandParcel.deleted_at.is_(None),
                Job.type == "assessment_report",
                Job.status == "succeeded",
                Job.progress_json["object_key"].as_string().is_not(None),
                Job.progress_json["object_key"].as_string() != "",
            )
            .order_by(Job.finished_at.desc().nullslast(), Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="No assessment report yet")
    return _job_response_with_report_url(job)


@router.get("/lands/{land_id}/assessment-report/latest/meta", response_model=JobOut)
async def get_latest_assessment_meta(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the latest assessment job for UI polling / download binding.

    While generating, returns the in-flight job. If the newest job is a
    terminal failure/cancel but an earlier succeeded PDF exists, prefer that
    succeeded job so the UI is not shadowed by a zombie error.
    """
    await _get_field(land_id, ctx.org_id, db)
    job = await _latest_report_job_for_meta(
        db, ctx=ctx, land_id=land_id, job_type="assessment_report"
    )
    if not job:
        raise HTTPException(status_code=404, detail="No assessment report yet")
    return _job_response_with_report_url(job)


def _scorecard_from_job(job: Job) -> dict[str, Any] | None:
    progress = job.progress_json or {}
    raw = progress.get("scorecard")
    if not isinstance(raw, dict):
        return None
    return scorecard_public_view(raw)


@router.get(
    "/lands/{land_id}/assessment-report/latest/scorecard",
    response_model=AssessmentScorecardOut,
)
async def get_latest_assessment_scorecard(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the six-dimension scorecard from the latest succeeded report.

    PDF download is unchanged. Jobs that finished before scorecards were
    persisted return ``scorecard_unavailable`` so the UI can ask the user
    to regenerate rather than inventing numbers.
    """
    await _get_field(land_id, ctx.org_id, db)
    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.land_id == land_id,
                Job.type == "assessment_report",
                Job.status == "succeeded",
            )
            .order_by(Job.finished_at.desc().nullslast(), Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "no_assessment_report",
                "message": "No assessment report yet",
            },
        )
    view = _scorecard_from_job(job)
    if not view:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "scorecard_unavailable",
                "message": "Latest report has no stored scorecard; generate a new report",
                "job_id": str(job.id),
            },
        )
    ai_ref = view.get("ai_reference")
    return AssessmentScorecardOut(
        job_id=job.id,
        overall=AssessmentOverallOut(**view["overall"]),
        dimensions=[AssessmentDimensionOut(**d) for d in view["dimensions"]],
        confidence=(
            AssessmentConfidenceOut(**view["confidence"])
            if view.get("confidence")
            else None
        ),
        ai_reference=(
            AssessmentAiReferenceOut(**ai_ref) if isinstance(ai_ref, dict) else None
        ),
        generated_at=job.finished_at or job.created_at,
    )
