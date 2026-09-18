"""下载机回报管理员任务实际运行状态的内部接口。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import AdminTaskRun

router = APIRouter(prefix="/internal/admin", tags=["internal-admin"])


class AdminTaskStatusRequest(BaseModel):
    worker_name: str = Field(..., min_length=1, max_length=256)
    celery_task_id: str = Field(..., min_length=1, max_length=255)
    status: Literal["running", "success", "failed", "cancelled"]
    result: Any | None = None
    error: str | None = Field(default=None, max_length=4000)


@router.post("/task-runs/{run_id}/status")
async def update_task_status(
    run_id: uuid.UUID,
    body: AdminTaskStatusRequest,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    run = await db.get(AdminTaskRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="admin task run not found")
    if run.celery_task_id and run.celery_task_id != body.celery_task_id:
        raise HTTPException(status_code=409, detail="celery task id mismatch")
    run.celery_task_id = body.celery_task_id
    run.status = body.status
    run.updated_at = datetime.now(timezone.utc)
    if body.status == "running":
        run.started_at = run.started_at or run.updated_at
    else:
        run.finished_at = run.finished_at or run.updated_at
        if body.status == "success":
            run.result_json = body.result
            run.error = None
        else:
            run.error = body.error or "Celery task failed"
    await db.commit()
    return {"ok": True, "run_id": str(run.id), "status": run.status}


__all__ = ["router"]
