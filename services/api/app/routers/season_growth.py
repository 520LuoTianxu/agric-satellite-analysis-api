# -*- coding: utf-8 -*-
"""Season growth (生育期长势) PDF report endpoints."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field as PydanticField, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.task_priority import INTERACTIVE_REPORT_PRIORITY
from app.core.database import get_db
from app.core.logging import logger
from app.core.rate_limit import limiter
from app.core.storage import get_storage
from app.middleware.auth import OrgContext, get_org_context, require_roles, org_scope
from app.models.tables import LandParcel, Job
from app.schemas.monitoring import JobOut
from app.services.cdfinance_report_prefetch import (
    normalize_optional_group_id,
    normalize_optional_hr_base_id,
    prefetch_cdfinance_for_report,
    resolve_request_token,
)


class SeasonGrowthGenerateRequest(BaseModel):
    start_date: str = PydanticField(..., description="YYYY-MM-DD")
    end_date: str = PydanticField(..., description="YYYY-MM-DD")
    crops: list[str] = PydanticField(default_factory=list)
    label: str | None = None
    material_keys: list[str] = PydanticField(default_factory=list)
    pull_data: bool = PydanticField(
        default=True,
        description="Queue weather + agri RS indices + soil bootstrap before PDF",
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

    @field_validator("start_date", "end_date")
    @classmethod
    def _valid_iso(cls, v: str) -> str:
        try:
            date.fromisoformat(str(v)[:10])
        except ValueError as e:
            raise ValueError("date must be YYYY-MM-DD") from e
        return str(v)[:10]


class MaterialUploadOut(BaseModel):
    key: str
    url: str | None = None
    filename: str | None = None
    bytes: int | None = None


router = APIRouter()
_writer = require_roles("owner", "admin", "member")


async def _get_field(land_id: str, org_id: uuid.UUID, db: AsyncSession) -> LandParcel:
    field = await db.get(LandParcel, land_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="LandParcel not found")
    return field


def _window_key(
    start: str, end: str, crops: list[str] | None, label: str | None
) -> str:
    crops_s = ",".join(sorted(crops or []))
    return f"{start}|{end}|{crops_s}|{label or ''}"


async def _maybe_enqueue_season_growth_work(
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
        type="season_growth_report",
        payload=payload,
        priority=priority,
        idempotency_key=f"season_growth_report:{job_id}",
    )
    await db.commit()
    return str(item.id)


@router.post(
    "/lands/{land_id}/season-growth-report",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("5/minute")
async def create_season_growth_report(
    request: Request,
    land_id: str,
    body: SeasonGrowthGenerateRequest,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
):
    """Enqueue data pulls (optional) + 生育期长势 PDF generation.

    Never refuses generation merely because weather / soil / RS are incomplete;
    missing series are handled inside the PDF worker. When pull_data=true,
    land_bootstrap fans out pulls first and season_growth follows (no race).
    """
    field = await _get_field(land_id, ctx.org_id, db)

    start = date.fromisoformat(body.start_date)
    end = date.fromisoformat(body.end_date)
    if end < start:
        raise HTTPException(status_code=422, detail="end_date must be >= start_date")

    pull_data = bool(body.pull_data)
    weather_days = max(1, (end - start).days)

    cdfinance_token = resolve_request_token(
        cdfinance_token=body.cdfinance_token,
        token=body.token,
        authorization=authorization,
    )
    group_id = normalize_optional_group_id(body.group_id)
    hr_base_id = normalize_optional_hr_base_id(body.hr_base_id)
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

    # Reuse in-flight pending/running job for same field (like assessment)
    existing = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.land_id == land_id,
                Job.type == "season_growth_report",
                Job.status.in_(("pending", "running")),
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    params: dict[str, Any] = {
        "kind": "season_growth",
        "start_date": body.start_date,
        "end_date": body.end_date,
        "crops": list(body.crops or []),
        "label": body.label,
        "material_keys": list(body.material_keys or []),
        "window_key": _window_key(
            body.start_date, body.end_date, body.crops, body.label
        ),
        "pull_data": pull_data,
        "weather_days": weather_days,
        "cdfinance_prefetch": cdfinance_prefetch,
        "group_id": group_id,
        "hr_base_id": hr_base_id,
    }
    job = Job(
        land_id=land_id,
        type="season_growth_report",
        status="pending",
        params_json=params,
    )
    db.add(job)
    await db.flush()
    await db.commit()

    try:
        from app.mq_publish import publish_api_task

        season_extras: dict[str, Any] = {
            "job_id": str(job.id),
            "start_date": body.start_date,
            "end_date": body.end_date,
            "crops": list(body.crops or []),
            "label": body.label,
            "material_keys": list(body.material_keys or []),
            "pull_data": pull_data,
            "cdfinance_prefetched": bool(
                cdfinance_prefetch and cdfinance_prefetch.get("token_provided")
            ),
            "group_id": group_id,
            "hr_base_id": hr_base_id,
        }
        # pull_data → land_bootstrap(+followup) only (same race fix as assessment).
        if not pull_data:
            work_id = await _maybe_enqueue_season_growth_work(
                db,
                job_id=str(job.id),
                land_id=str(land_id),
                extras=season_extras,
                priority=INTERACTIVE_REPORT_PRIORITY,
            )
            if work_id:
                logger.info(
                    "season_growth_work_item_enqueued",
                    job_id=str(job.id),
                    work_id=work_id,
                    pull_data=False,
                )

        if pull_data:
            # 生育期报告也使用 10km 共享窗口下载；bootstrap 只负责天气/土壤，
            # 防止报告入口重新走逐地块遥感链路。
            # publish_api_task also inserts work_items when dual|claim (D4).
            from app.services.satellite_batch import create_satellite_batch_jobs

            _, satellite_jobs, _ = await create_satellite_batch_jobs(
                db,
                [field],
                date_from=start,
                date_to=end,
                sensors=("S1", "S2"),
                force=False,
                parent_job_id=job.id,
            )
            satellite_job_ids = [str(item.id) for item in satellite_jobs]
            job.params_json = {
                **(job.params_json or {}),
                "satellite_batch_job_ids": satellite_job_ids,
            }
            await db.commit()
            for satellite_job in satellite_jobs:
                publish_api_task(
                    type="satellite_batch",
                    land_id=satellite_job.land_id,
                    task_id=str(satellite_job.id),
                    extras={"job_id": str(satellite_job.id)},
                    priority=INTERACTIVE_REPORT_PRIORITY,
                )

            season_mq_task_id = str(uuid.uuid4())
            bootstrap_extras: dict[str, Any] = {
                "date_from": body.start_date,
                "date_to": body.end_date,
                "days": weather_days,
                "weather_days": weather_days,
                "source": "season_growth_one_click",
                "skip_indices": True,
                "satellite_job_ids": satellite_job_ids,
                "followup_season_growth": {
                    "job_id": str(job.id),
                    "mq_task_id": season_mq_task_id,
                    "start_date": body.start_date,
                    "end_date": body.end_date,
                    "crops": list(body.crops or []),
                    "label": body.label,
                    "material_keys": list(body.material_keys or []),
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
                "season_growth_bootstrap_dispatched",
                job_id=str(job.id),
                land_id=str(land_id),
                mq_task_id=bootstrap_task_id,
                season_mq_task_id=season_mq_task_id,
                start_date=body.start_date,
                end_date=body.end_date,
                weather_days=weather_days,
                pull_data=True,
            )
        else:
            mq_task_id = publish_api_task(
                type="season_growth_report",
                land_id=str(land_id),
                extras=season_extras,
                priority=INTERACTIVE_REPORT_PRIORITY,
            )
            logger.info(
                "season_growth_job_dispatched",
                job_id=str(job.id),
                land_id=str(land_id),
                mq_task_id=mq_task_id,
                pull_data=False,
            )
    except Exception as e:
        logger.error(
            "season_growth_job_dispatch_failed",
            job_id=str(job.id),
            error=str(e),
        )
        job = await db.get(Job, job.id) or job
        job.status = "failed"
        job.error = f"dispatch failed: {e}"
        await db.commit()

    return job


@router.post(
    "/lands/{land_id}/season-growth-report/materials",
    response_model=MaterialUploadOut,
)
@limiter.limit("20/minute")
async def upload_season_growth_material(
    request: Request,
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    file: UploadFile = File(...),
):
    """Upload an optional material file; returns storage key for generate body."""
    await _get_field(land_id, ctx.org_id, db)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty file")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="file too large (max 20MB)")

    safe_name = (file.filename or "material.bin").replace("/", "_").replace("\\", "_")
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    object_key = f"reports/season_growth/{land_id}/{ts}-{safe_name}"
    storage = get_storage()
    content_type = file.content_type or "application/octet-stream"
    storage.put_bytes(object_key, raw, content_type=content_type)

    url = None
    try:
        url = storage.public_url(object_key)
    except Exception:
        url = None
    return MaterialUploadOut(
        key=object_key, url=url, filename=safe_name, bytes=len(raw)
    )


@router.get("/lands/{land_id}/season-growth-report/latest")
async def get_latest_season_growth_report(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Download the latest succeeded season-growth PDF for a field."""
    await _get_field(land_id, ctx.org_id, db)

    job = (
        await db.execute(
            select(Job)
            .where(
                org_scope(None, ctx),
                Job.land_id == land_id,
                Job.type == "season_growth_report",
                Job.status == "succeeded",
            )
            .order_by(Job.finished_at.desc().nullslast(), Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="No season growth report yet")

    progress = job.progress_json or {}
    object_key = progress.get("object_key")
    if not object_key:
        raise HTTPException(status_code=404, detail="Report file missing")

    storage = get_storage()
    if not storage.exists(object_key):
        raise HTTPException(
            status_code=404, detail="Report object not found in storage"
        )

    data = storage.get_bytes(object_key)
    filename = progress.get("filename") or "生育期长势分析报告.pdf"
    disp = (
        f'attachment; filename="season-growth.pdf"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    headers = {
        "Content-Disposition": disp,
        "X-Season-Growth-Job-Id": str(job.id),
    }
    public_url = progress.get("public_url")
    if public_url:
        headers["X-Season-Growth-Public-Url"] = str(public_url)
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


@router.get(
    "/lands/{land_id}/season-growth-report/latest/meta",
    response_model=JobOut,
)
async def get_latest_season_growth_meta(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the latest season-growth job for UI polling / download binding.

    While generating, returns the in-flight job. If the newest job is a
    terminal failure/cancel but an earlier succeeded PDF exists, prefer that
    succeeded job so the UI is not shadowed by a zombie error.
    """
    await _get_field(land_id, ctx.org_id, db)
    job = await _latest_report_job_for_meta(
        db, ctx=ctx, land_id=land_id, job_type="season_growth_report"
    )
    if not job:
        raise HTTPException(status_code=404, detail="No season growth report yet")
    return job
