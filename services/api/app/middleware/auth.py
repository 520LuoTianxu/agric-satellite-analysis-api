"""Auth dependencies — OpenFarm login/orgs removed; anonymous bypass.

Independent login will be added later. Until then every route is open:
JWT and X-Org-Id are ignored, role checks always pass, and org scoping
is disabled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header
from sqlalchemy import ColumnElement, true as sql_true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

AUTH_DISABLED = True

ANON_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@dataclass
class CurrentUser:
    """Anonymous stand-in until independent auth is added."""

    id: uuid.UUID
    email: str
    name: str


@dataclass
class OrgContext:
    """Legacy request context; org_id is always None after auth removal."""

    user: CurrentUser
    org_id: uuid.UUID | None
    role: str


ANON_USER = CurrentUser(
    id=ANON_USER_ID,
    email="anonymous@local",
    name="Anonymous",
)


def org_matches(*_args: Any, **_kwargs: Any) -> bool:
    """Org scoping removed — all rows visible."""
    return True


def org_scope(_column: Any = None, _ctx: OrgContext | None = None) -> ColumnElement[bool]:
    """Org scoping removed — no-op SQL filter."""
    return sql_true()


async def get_current_user(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> CurrentUser:
    return ANON_USER


async def get_org_context(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    x_org_id: Annotated[str | None, Header(alias="X-Org-Id")] = None,
) -> OrgContext:
    return OrgContext(user=user, org_id=None, role="owner")


def require_roles(*_allowed_roles: str):
    async def _check(
        ctx: Annotated[OrgContext, Depends(get_org_context)],
    ) -> OrgContext:
        return ctx

    return _check
