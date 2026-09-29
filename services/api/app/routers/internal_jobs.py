"""Internal jobs get/patch for download-host workers (D2)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import Job

router = APIRouter(prefix="/internal/jobs", tags=["internal-jobs"])


class InternalJobOut(BaseModel):
    id: uuid.UUID
    land_id: str | None = None
    type: str
    parent_job_id: uuid.UUID | None = None
    status: str
    progress_json: dict[str, Any] | None = None
    error: str | None = None
    params_json: dict[str, Any] | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


class InternalJobPatch(BaseModel):
    status: str | None = Field(default=None, max_length=20)
    progress_json: dict[str, Any] | None = None
    error: str | None = None
    # When true, merge progress_json into existing rather than replace.
    merge_progress: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    touch_started: bool = False
    touch_finished: bool = False


def _to_out(job: Job) -> InternalJobOut:
    return InternalJobOut.model_validate(job)


def _patch_out(job: Job, *, include_progress: bool) -> InternalJobOut:
    if include_progress:
        return _to_out(job)
    # 高频增量PATCH只返回轻量状态，避免校验、序列化并传输完整进度与任务参数。
    return InternalJobOut(
        id=job.id,
        land_id=job.land_id,
        type=job.type,
        parent_job_id=job.parent_job_id,
        status=job.status,
        progress_json=None,
        error=job.error,
        params_json=None,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _merge_progress_json(
    current: dict[str, Any] | None, patch: dict[str, Any]
) -> dict[str, Any]:
    """合并任务进度，并按稳定场景键幂等追加已发布产品增量。"""
    merged = dict(current) if isinstance(current, dict) else {}
    incoming = dict(patch)
    published_delta = incoming.pop("published_products_delta", None)
    merged.update(incoming)

    old_steps = current.get("steps") if isinstance(current, dict) else None
    new_steps = patch.get("steps")
    if isinstance(old_steps, dict) and isinstance(new_steps, dict):
        # 分阶段任务会分别补写步骤进度；合并同名步骤字段，保留其他步骤的已完成状态。
        steps = dict(old_steps)
        for key, value in new_steps.items():
            if isinstance(value, dict) and isinstance(steps.get(key), dict):
                entry = dict(steps[key])
                entry.update(value)
                steps[key] = entry
            else:
                steps[key] = value
        merged["steps"] = steps

    if published_delta is not None:
        if not isinstance(published_delta, list):
            raise HTTPException(
                status_code=422, detail="published_products_delta must be a list"
            )
        products = merged.get("published_products")
        if not isinstance(products, list):
            products = []

        def product_key(product: dict[str, Any]) -> tuple[str, str, str] | None:
            land_id = str(product.get("land_id") or "").strip()
            product_date = str(product.get("date") or "").strip()[:10]
            if not land_id or not product_date:
                return None
            return land_id, product_date, str(product.get("scene_id") or "").strip()

        # PATCH 重试可能重复提交同一景增量，重复回执会干扰总览的入库核对。
        known: set[tuple[str, str, str]] = set()
        for product in products:
            if isinstance(product, dict):
                key = product_key(product)
                if key is not None:
                    known.add(key)
        for product in published_delta:
            if not isinstance(product, dict):
                raise HTTPException(
                    status_code=422,
                    detail="published_products_delta entries must be objects",
                )
            key = product_key(product)
            if key is None:
                raise HTTPException(
                    status_code=422,
                    detail="published products require land_id and date",
                )
            if key not in known:
                products.append(dict(product))
                known.add(key)
        merged["published_products"] = products

    return merged


@router.get("/{job_id}", response_model=InternalJobOut)
async def get_job(
    job_id: uuid.UUID,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _to_out(job)


@router.patch("/{job_id}", response_model=InternalJobOut)
async def patch_job(
    job_id: uuid.UUID,
    body: InternalJobPatch,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    include_progress: bool = True,
):
    """Update job status / progress (compat with ingest SyncSession writes).

    D2 callers may still write via DATABASE_URL; this endpoint exists for
    gradual cutover and for workers that already use API_BASE_URL.
    """
    # 增量进度需串行合并同一任务的 JSON，避免并发 worker 覆盖彼此的产品回执。
    job = await db.get(Job, job_id, with_for_update=True)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    stale_recovered = isinstance(job.progress_json, dict) and job.progress_json.get(
        "stale_recovered"
    )
    if (
        stale_recovered
        and (
            body.progress_json is not None
            or body.status in {"pending", "running", "succeeded", "completed"}
        )
    ):
        # 旧下载机即使在回收后迟到上报，也不能把已判失败的任务重新改成成功，
        # 也不能用不含回收标记的进度覆盖失败依据；否则父任务可能再次被错误地
        # 标记为未完成或覆盖补偿依据。
        return _patch_out(job, include_progress=include_progress)

    now = datetime.now(timezone.utc)
    if body.status is not None:
        job.status = body.status
        if body.status == "running" and job.started_at is None:
            job.started_at = now
        if body.status in ("succeeded", "failed", "cancelled", "completed"):
            if job.finished_at is None:
                job.finished_at = now

    if body.progress_json is not None:
        if body.merge_progress:
            job.progress_json = _merge_progress_json(
                job.progress_json, body.progress_json
            )
        else:
            job.progress_json = body.progress_json
        flag_modified(job, "progress_json")

    if body.error is not None:
        job.error = body.error

    if body.started_at is not None:
        job.started_at = body.started_at
    elif body.touch_started and job.started_at is None:
        job.started_at = now

    if body.finished_at is not None:
        job.finished_at = body.finished_at
    elif body.touch_finished:
        job.finished_at = now

    await db.commit()
    await db.refresh(job)
    return _patch_out(job, include_progress=include_progress)
