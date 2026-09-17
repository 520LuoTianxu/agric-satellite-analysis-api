"""租户预警列表、处理状态及用户独立已读。"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.logging import logger
from app.middleware.alert_auth import AlertContext, get_alert_context
from app.models.tables import Alert, AlertRead, Farm, LandParcel
from app.schemas.common import PaginatedResponse
from app.schemas.monitoring import (
    AlertOut,
    AlertReadResult,
    AlertSummaryOut,
    AlertUpdate,
)

router = APIRouter()
Context = Annotated[AlertContext, Depends(get_alert_context)]
Database = Annotated[AsyncSession, Depends(get_db)]


def _scoped_alerts(ctx: AlertContext):
    # 地块所属基地是租户边界；读取、计数、单条和批量操作必须使用相同范围。
    return (
        select(Alert)
        .join(LandParcel, Alert.land_id == LandParcel.land_id)
        .outerjoin(Farm, (LandParcel.farm_id == Farm.id) & Farm.deleted_at.is_(None))
        .outerjoin(
            AlertRead,
            (AlertRead.alert_id == Alert.id)
            & (AlertRead.base_id == ctx.base_id)
            & (AlertRead.user_id == ctx.user_id),
        )
        .where(LandParcel.base_id == ctx.base_id, LandParcel.deleted_at.is_(None))
    )


def _with_display_columns(stmt):
    return stmt.add_columns(LandParcel.land_name, Farm.id, Farm.name, AlertRead.read_at)


def _alert_out(row) -> AlertOut:
    alert, land_name, farm_id, farm_name, read_at = row
    return AlertOut.model_validate(alert).model_copy(
        update={
            "land_name": land_name,
            "farm_id": farm_id,
            "farm_name": farm_name,
            "read_at": read_at,
            "is_read": read_at is not None,
        }
    )


async def _page(db, base, limit, offset):
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        await db.execute(
            _with_display_columns(base)
            .order_by(Alert.created_at.desc(), Alert.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return PaginatedResponse(
        items=[_alert_out(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/alerts", response_model=PaginatedResponse[AlertOut])
async def list_alerts(
    ctx: Context,
    db: Database,
    land_id: str | None = Query(None),
    farm_id: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    severity: str | None = Query(None),
    index_type: str | None = Query(None),
    is_read: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    base = _scoped_alerts(ctx)
    if land_id:
        base = base.where(Alert.land_id == land_id)
    if status_filter:
        base = base.where(Alert.status == status_filter)
    if severity:
        base = base.where(Alert.severity == severity)
    if index_type:
        base = base.where(Alert.index_type == index_type)
    if farm_id:
        base = base.where(LandParcel.farm_id == farm_id)
    if is_read is not None:
        base = base.where(
            AlertRead.read_at.is_not(None) if is_read else AlertRead.read_at.is_(None)
        )
    return await _page(db, base, limit, offset)


@router.get("/alerts/summary", response_model=AlertSummaryOut)
async def alerts_summary(ctx: Context, db: Database):
    # 未读与未关闭分别统计，关闭预警不会替其他用户标记已读。
    scope = _scoped_alerts(ctx).add_columns(AlertRead.read_at).subquery()
    active = scope.c.status == "open"
    row = (
        (
            await db.execute(
                select(
                    func.count().filter(active).label("open_total"),
                    func.count()
                    .filter(scope.c.read_at.is_(None))
                    .label("unread_total"),
                    func.count()
                    .filter(active & (scope.c.severity == "high"))
                    .label("high"),
                    func.count()
                    .filter(active & (scope.c.severity == "medium"))
                    .label("medium"),
                    func.count()
                    .filter(active & (scope.c.severity == "low"))
                    .label("low"),
                ).select_from(scope)
            )
        )
        .mappings()
        .one()
    )
    return AlertSummaryOut(**row)


def _read_insert(ctx: AlertContext, alert_id: uuid.UUID | None = None):
    # INSERT SELECT 覆盖当前租户全部分页，以语句快照为界，不吞掉之后新增的预警。
    source = (
        _scoped_alerts(ctx)
        .with_only_columns(
            literal(ctx.base_id), literal(ctx.user_id), Alert.id, func.now()
        )
        .where(AlertRead.read_at.is_(None))
    )
    if alert_id is not None:
        source = source.where(Alert.id == alert_id)
    return (
        insert(AlertRead)
        .from_select(["base_id", "user_id", "alert_id", "read_at"], source)
        # 并发点击、重复请求均幂等，保留首次阅读时间。
        .on_conflict_do_nothing(index_elements=["base_id", "user_id", "alert_id"])
        .returning(AlertRead.alert_id)
    )


@router.post("/alerts/read-all", response_model=AlertReadResult)
async def mark_all_read(ctx: Context, db: Database):
    inserted = _read_insert(ctx).cte("marked")
    count = (await db.execute(select(func.count()).select_from(inserted))).scalar_one()
    return AlertReadResult(marked_count=count)


@router.post("/alerts/{alert_id}/read", response_model=AlertOut)
async def mark_read(alert_id: uuid.UUID, ctx: Context, db: Database):
    query = _with_display_columns(_scoped_alerts(ctx).where(Alert.id == alert_id))
    if (await db.execute(query)).first() is None:
        raise HTTPException(404, "Alert not found")
    await db.execute(_read_insert(ctx, alert_id))
    return _alert_out((await db.execute(query)).one())


@router.get("/lands/{land_id}/alerts", response_model=PaginatedResponse[AlertOut])
async def list_field_alerts(
    land_id: str,
    ctx: Context,
    db: Database,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await _page(
        db, _scoped_alerts(ctx).where(Alert.land_id == land_id), limit, offset
    )


@router.patch("/alerts/{alert_id}", response_model=AlertOut)
async def update_alert(
    alert_id: uuid.UUID, body: AlertUpdate, ctx: Context, db: Database
):
    query = _with_display_columns(_scoped_alerts(ctx).where(Alert.id == alert_id))
    row = (await db.execute(query)).first()
    if row is None:
        raise HTTPException(404, "Alert not found")
    if body.status not in ("open", "closed"):
        raise HTTPException(400, "Status must be 'open' or 'closed'")
    row[0].status = body.status
    await db.flush()
    logger.info("alert_updated", alert_id=str(alert_id), new_status=body.status)
    return _alert_out(row)
