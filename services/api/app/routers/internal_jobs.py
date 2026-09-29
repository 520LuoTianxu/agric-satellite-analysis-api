"""Internal jobs get/patch for download-host workers (D2)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import Job

router = APIRouter(prefix="/internal/jobs", tags=["internal-jobs"])

_INSERT_PRODUCT_RECEIPTS = text("""
    INSERT INTO agric_satellite.satellite_job_product_receipts
        (job_id, land_id, product_date, sensor, scene_id)
    VALUES
        (:job_id, :land_id, :product_date, :sensor, :scene_id)
    ON CONFLICT DO NOTHING
""")


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
    """合并轻量任务进度；产品回执由关系表单独保存，不进入 JSONB。"""
    merged = dict(current) if isinstance(current, dict) else {}
    incoming = dict(patch)
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

    return merged


def _product_receipt_rows(
    job_id: uuid.UUID,
    products: Any,
    default_sensor: str | None,
) -> list[dict[str, Any]]:
    """校验 worker 回执并转换为可幂等写入的地块、日期和场景键。"""
    if products is None:
        return []
    if not isinstance(products, list):
        raise HTTPException(status_code=422, detail="published products must be a list")

    rows = []
    for product in products:
        if not isinstance(product, dict):
            raise HTTPException(
                status_code=422,
                detail="published product entries must be objects",
            )
        land_id = str(product.get("land_id") or "").strip()
        raw_date = str(product.get("date") or "").strip()
        if not land_id or not raw_date:
            raise HTTPException(
                status_code=422,
                detail="published products require land_id and date",
            )
        try:
            product_date = date.fromisoformat(raw_date[:10])
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="published product date must be ISO format"
            ) from exc

        sensor = str(product.get("sensor") or default_sensor or "").strip().upper()
        if sensor not in {"S1", "S2"}:
            raise HTTPException(
                status_code=422,
                detail="published products require a valid S1 or S2 sensor",
            )
        rows.append(
            {
                "job_id": job_id,
                "land_id": land_id,
                "product_date": product_date,
                "sensor": sensor,
                "scene_id": str(product.get("scene_id") or "").strip(),
            }
        )
    return rows


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
        # 兼容旧 worker 的累计列表，并接受新 worker 每景发送的增量；
        # 数据库复合主键负责重试去重，避免在应用内扫描整段历史列表。
        current_progress = (
            job.progress_json if isinstance(job.progress_json, dict) else {}
        )
        incoming_progress = dict(body.progress_json)
        published_delta = incoming_progress.pop("published_products_delta", None)
        published_full = incoming_progress.pop("published_products", None)
        product_rows = _product_receipt_rows(
            job.id,
            current_progress.get("published_products"),
            (job.params_json or {}).get("sensor"),
        )
        product_rows.extend(
            _product_receipt_rows(
                job.id, published_full, (job.params_json or {}).get("sensor")
            )
        )
        product_rows.extend(
            _product_receipt_rows(
                job.id, published_delta, (job.params_json or {}).get("sensor")
            )
        )
        if product_rows:
            await db.execute(_INSERT_PRODUCT_RECEIPTS, product_rows)

        if body.merge_progress:
            job.progress_json = _merge_progress_json(
                job.progress_json, incoming_progress
            )
        else:
            job.progress_json = incoming_progress
        if isinstance(job.progress_json, dict):
            # 不再让历史 worker 的累计产品数组留在高频更新的任务 JSONB 中。
            job.progress_json.pop("published_products", None)
            job.progress_json.pop("published_products_delta", None)
            if (
                current_progress.get("product_receipts_normalized")
                or "published_products" in current_progress
                or published_full is not None
                or published_delta is not None
            ):
                # 标记后，即使重试把本轮计数归零，总览仍会核验此前已提交的场景回执。
                job.progress_json["product_receipts_normalized"] = True
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
