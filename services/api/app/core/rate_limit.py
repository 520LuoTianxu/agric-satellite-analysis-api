"""Rate limiting configuration - shared limiter instance."""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings
from app.core.redis_client import REDIS_RESP2_PROTOCOL


def _key_func(request: Request) -> str:
    """Extract rate-limit key: authenticated user ID or remote IP."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from jose import jwt as _jwt

            token = auth.removeprefix("Bearer ").strip()
            payload = _jwt.decode(
                token,
                settings.openfarm_jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )
            sub = payload.get("sub")
            if sub:
                return f"user:{sub}"
        except Exception:
            pass
    return get_remote_address(request)


limiter = Limiter(
    key_func=_key_func,
    default_limits=["120/minute"],
    storage_uri=settings.redis_url,
    # limits uses sync redis.from_url; pin RESP2 so AUTH does not send HELLO.
    storage_options={"protocol": REDIS_RESP2_PROTOCOL},
)
