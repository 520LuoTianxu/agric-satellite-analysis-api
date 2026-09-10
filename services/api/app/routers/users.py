"""User routes removed — OpenFarm auth/orgs dropped in migration 0018."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter()


@router.api_route("/users/me", methods=["GET", "PATCH"], include_in_schema=False)
@router.api_route("/users", methods=["GET", "POST"], include_in_schema=False)
@router.api_route(
    "/users/{path:path}",
    methods=["GET", "POST", "PATCH", "DELETE", "PUT"],
    include_in_schema=False,
)
async def users_gone(path: str | None = None) -> None:
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="User APIs removed (auth/orgs dropped). See AUTH_REMOVAL.md.",
    )
