"""虚拟项目区的人工初始化和历史回填入口。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.middleware.auth import OrgContext, require_roles
from app.schemas.virtual_area import (
    VirtualAreaOperationOut,
    VirtualAreaOperationRequest,
)
from app.services.virtual_area_service import (
    backfill_virtual_area_history,
    initialize_virtual_areas,
)

router = APIRouter(
    prefix="/admin/virtual-project-areas", tags=["virtual-project-areas"]
)
_admin = require_roles("owner", "admin")


@router.post("/initialize", response_model=VirtualAreaOperationOut, status_code=202)
async def initialize(
    body: VirtualAreaOperationRequest,
    _: Annotated[OrgContext, Depends(_admin)],
) -> VirtualAreaOperationOut:
    """初始化 vpa10 项目区主数据；重复调用只补齐未归属地块。"""
    try:
        result = await initialize_virtual_areas(
            land_ids=body.land_ids,
            parent_job_id=uuid.uuid4(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return VirtualAreaOperationOut.model_validate(result)


@router.post(
    "/history-backfill", response_model=VirtualAreaOperationOut, status_code=202
)
async def history_backfill(
    body: VirtualAreaOperationRequest,
    _: Annotated[OrgContext, Depends(_admin)],
) -> VirtualAreaOperationOut:
    """按项目区共享窗口下发历史 S1/S2 回填任务。"""
    try:
        result = await backfill_virtual_area_history(
            land_ids=body.land_ids,
            date_from=body.date_from,
            date_to=body.date_to,
            years=body.years,
            sensors=body.sensors,
            force=body.force,
            parent_job_id=uuid.uuid4(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return VirtualAreaOperationOut.model_validate(result)


__all__ = ["router"]
