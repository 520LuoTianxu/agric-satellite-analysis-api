"""Users router - GET /users/me (stubbed after OpenFarm auth removal)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.logging import logger
from app.middleware.auth import (
    AUTH_DISABLED,
    ANON_USER,
    CurrentUser,
    get_current_user,
)
from app.schemas.auth import UserMeOut

router = APIRouter()


@router.get("/users/me", response_model=UserMeOut)
async def get_me(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return user profile. When auth disabled, return anonymous stub (no DB user)."""
    logger.info("get_me", user_id=str(current_user.id), auth_disabled=AUTH_DISABLED)

    if AUTH_DISABLED:
        return UserMeOut(
            id=ANON_USER.id,
            email=ANON_USER.email,
            name=ANON_USER.name,
            avatar_url=None,
            created_at=datetime.now(timezone.utc),
            orgs=[],
        )

    from fastapi import HTTPException, status
    from sqlalchemy import select

    from app.models.tables import Org, OrgMember, User
    from app.schemas.auth import OrgBrief

    user = await db.get(User, current_user.id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    result = await db.execute(
        select(OrgMember.role, Org.id, Org.name)
        .join(Org, OrgMember.org_id == Org.id)
        .where(Org.deleted_at.is_(None))
        .where(OrgMember.user_id == current_user.id)
    )
    orgs = [OrgBrief(id=row.id, name=row.name, role=row.role) for row in result.all()]

    return UserMeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        created_at=user.created_at,
        orgs=orgs,
    )
