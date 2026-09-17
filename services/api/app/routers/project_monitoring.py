"""种植项目监测入口，group_id 与历史 farm_id 使用不同身份。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.auth import OrgContext, require_roles
from app.schemas.project_monitoring import ProjectMonitoringOut
from app.services.project_monitoring import get_project_monitoring

router = APIRouter()
_reader = require_roles("owner", "admin", "member", "viewer")


@router.get("/projects/{group_id}/monitoring", response_model=ProjectMonitoringOut)
async def project_monitoring(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    group_id: Annotated[str, Path(min_length=1, max_length=64)],
    freshness_days: Annotated[int, Query(ge=1, le=90)] = 14,
):
    """有效期是可调整的数据新鲜度策略，不改变现有农情预警阈值。"""
    return await get_project_monitoring(db, group_id, freshness_days=freshness_days)
