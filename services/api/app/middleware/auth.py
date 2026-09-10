"""Auth dependencies — OpenFarm login/orgs removed; anonymous bypass.

Independent login will be added later. Until then every route is open:
JWT and X-Org-Id are ignored, role checks always pass, and org scoping
is disabled (see org_scope / org_matches).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header
from sqlalchemy import ColumnElement, true as sql_true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

# Feature flag: OpenFarm users/orgs/auth fully bypassed.
AUTH_DISABLED = True

# Stable anonymous identity for optional created_by writes (no users row required
# once user FKs are dropped by migration 0017).
ANON_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@dataclass
class CurrentUser:
    """Authenticated user extracted from JWT (or anonymous when auth disabled)."""

    id: uuid.UUID
    email: str
    name: str


@dataclass
class OrgContext:
    """Org context for the request (org_id is None when auth disabled)."""

    user: CurrentUser
    org_id: uuid.UUID | None
    role: str  # owner | admin | member | viewer


ANON_USER = CurrentUser(
    id=ANON_USER_ID,
    email="anonymous@local",
    name="Anonymous",
)


def org_matches(row_org_id: uuid.UUID | None, ctx_org_id: uuid.UUID | None) -> bool:
    """Return True if the row is visible under the current org scope."""
    if AUTH_DISABLED or ctx_org_id is None:
        return True
    return row_org_id == ctx_org_id


def org_scope(column: Any, ctx: OrgContext) -> ColumnElement[bool]:
    """SQLAlchemy WHERE fragment for org scoping (no-op when auth disabled)."""
    if AUTH_DISABLED or ctx.org_id is None:
        return sql_true()
    return column == ctx.org_id


async def get_current_user(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> CurrentUser:
    """Return anonymous user when auth disabled; otherwise legacy JWT path."""
    if AUTH_DISABLED:
        return ANON_USER

    # Legacy path kept for a future independent auth integration.
    from fastapi import HTTPException, status
    from jose import JWTError, jwt

    from app.core.config import settings

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(
            token, settings.openfarm_jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing subject"
        )

    return CurrentUser(
        id=uuid.UUID(user_id),
        email=payload.get("email", ""),
        name=payload.get("name", ""),
    )


async def get_org_context(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    x_org_id: Annotated[str | None, Header(alias="X-Org-Id")] = None,
) -> OrgContext:
    """Build org context. When auth disabled, X-Org-Id is optional and ignored."""
    if AUTH_DISABLED:
        org_uuid: uuid.UUID | None = None
        if x_org_id:
            try:
                org_uuid = uuid.UUID(x_org_id)
            except ValueError:
                org_uuid = None
        # Role elevated so require_roles always passes under AUTH_DISABLED.
        return OrgContext(user=user, org_id=org_uuid, role="owner")

    from fastapi import HTTPException, status

    if not x_org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Org-Id header is required",
        )

    try:
        org_uuid = uuid.UUID(x_org_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Org-Id format"
        )

    from app.models.tables import Org, OrgMember
    from sqlalchemy import select

    result = await db.execute(
        select(OrgMember.role)
        .join(Org, Org.id == OrgMember.org_id)
        .where(
            OrgMember.org_id == org_uuid,
            OrgMember.user_id == user.id,
            Org.deleted_at.is_(None),
        )
    )
    row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not a member of this org"
        )

    return OrgContext(user=user, org_id=org_uuid, role=row)


def require_roles(*allowed_roles: str):
    """Dependency factory — when auth disabled, always returns org context."""

    async def _check(
        ctx: Annotated[OrgContext, Depends(get_org_context)],
    ) -> OrgContext:
        if AUTH_DISABLED:
            return ctx
        from fastapi import HTTPException, status

        if ctx.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Role '{ctx.role}' not permitted. "
                    f"Required: {', '.join(allowed_roles)}"
                ),
            )
        return ctx

    return _check
