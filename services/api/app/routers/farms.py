"""Farm container API.

Farms are optional grouping metadata.  Parcel identity and all parcel data
remain in agric_satellite.land_parcels and are never translated elsewhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.logging import logger
from app.middleware.auth import OrgContext, get_org_context, require_roles
from app.models.tables import Farm, LandParcel
from app.routers.lands import _land_to_out
from app.schemas.common import PaginatedResponse
from app.schemas.farm import FarmCreate, FarmOut, FarmUpdate, LandParcelOut

router = APIRouter()
_reader = require_roles("owner", "admin", "member", "viewer")
_writer = require_roles("owner", "admin", "member")


@router.get("/farms", response_model=PaginatedResponse[FarmOut])
async def list_farms(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None),
):
    filters = [Farm.deleted_at.is_(None)]
    if q and q.strip():
        filters.append(Farm.name.ilike(f"%{q.strip()}%"))
    base = select(Farm).where(*filters)
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        await db.execute(
            base.order_by(Farm.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return PaginatedResponse(items=rows, total=int(total), limit=limit, offset=offset)


@router.post("/farms", response_model=FarmOut, status_code=status.HTTP_201_CREATED)
async def create_farm(
    body: FarmCreate,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    farm = Farm(
        name=body.name,
        country=body.country,
        region=body.region,
        timezone=body.timezone,
    )
    db.add(farm)
    await db.flush()
    logger.info("farm_created", farm_id=str(farm.id), name=body.name)
    return farm


@router.get("/farms/{farm_id}", response_model=FarmOut)
async def get_farm(
    farm_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    farm = await db.get(Farm, farm_id)
    if not farm or farm.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Farm not found")
    return farm


@router.put("/farms/{farm_id}", response_model=FarmOut)
async def update_farm(
    farm_id: str,
    body: FarmUpdate,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    farm = await db.get(Farm, farm_id)
    if not farm or farm.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Farm not found")
    for name in ("name", "country", "region", "timezone"):
        value = getattr(body, name)
        if value is not None:
            setattr(farm, name, value)
    farm.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return farm


@router.delete("/farms/{farm_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_farm(
    farm_id: str,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Soft-delete the container and all of its canonical parcel rows."""
    farm = await db.get(Farm, farm_id)
    if not farm or farm.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Farm not found")
    now = datetime.now(timezone.utc)
    farm.deleted_at = now
    await db.execute(
        update(LandParcel)
        .where(LandParcel.farm_id == farm_id, LandParcel.deleted_at.is_(None))
        .values(deleted_at=now, updated_at=now)
    )
    await db.commit()


@router.get(
    "/farms/{farm_id}/lands", response_model=PaginatedResponse[LandParcelOut]
)
async def list_farm_lands(
    farm_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    farm = await db.get(Farm, farm_id)
    if not farm or farm.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Farm not found")
    base = select(LandParcel).where(
        LandParcel.farm_id == farm_id, LandParcel.deleted_at.is_(None)
    )
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        await db.execute(
            base.order_by(LandParcel.created_at.desc(), LandParcel.land_id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return PaginatedResponse(
        items=[_land_to_out(row) for row in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )
