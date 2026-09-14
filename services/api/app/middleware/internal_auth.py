"""Bearer token auth for /v1/internal/* (download-host control plane)."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.core.config import settings


def require_internal_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Require Authorization: Bearer <INTERNAL_API_TOKEN>.

    Fails closed when token is unset in settings (503) so misconfigured
    deployments do not silently accept unauthenticated internal calls.
    """
    expected = (settings.internal_api_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="INTERNAL_API_TOKEN not configured",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    got = authorization.split(" ", 1)[1].strip()
    if not got or not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="Invalid internal token")


InternalAuth = Annotated[None, Depends(require_internal_token)]
