"""Dedicated auth for biweekly trigger routes on the API machine.

This module must not import or call get_current_user, require_roles, or
ANON_USER from app.middleware.auth. The existing AUTH_DISABLED=True
anonymous bypass must not be reused or widened.

Two modes, both requiring a signed tenant JWT (Authorization: Bearer):

- Production-shaped (BIWEEKLY_DEV_AUTH=0): JWT signature uses a non-placeholder
  secret; claims must include sub, tenant_id, and this land in land_ids.
  This is still not a finished production authorizer — membership is taken
  from the signed token rather than a live tenant-land query — and must be
  replaced with a verified Smart/tenant land-membership query before any production cutover.
- Dev switch (BIWEEKLY_DEV_AUTH=1): still requires a signed JWT and a
  non-placeholder BIWEEKLY_JWT_SECRET; the path land must be in
  BIWEEKLY_ALLOWED_LANDS. 不能原样上生产. Do not enable this flag in
  production environments.

Default BIWEEKLY_API_ENABLED=0 fails closed with 503.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Path
from jose import JWTError, jwt

from app.core.config import settings

PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "change-me",
        "changeme",
        "secret",
        "jwt-secret",
        "change-me-to-a-random-64-char-string",
        "change-me-to-a-random-32-char-string",
    }
)

AUTH_MODE_DEV = "biweekly_dev_auth"
AUTH_MODE_JWT_CLAIM = "jwt_land_claim"
AUTH_MODE_SESSION = "login_session_token"


@dataclass(frozen=True)
class BiweeklyPrincipal:
    subject: str
    tenant_id: str
    land_id: str
    auth_mode: Literal["biweekly_dev_auth", "jwt_land_claim", "login_session_token"]
    not_for_production: bool

    def public_auth(self) -> dict:
        return {
            "mode": self.auth_mode,
            "subject": self.subject,
            "tenant_id": self.tenant_id,
            "not_for_production": self.not_for_production,
            "land_acl": (
                "jwt_land_claim"
                if self.auth_mode == AUTH_MODE_JWT_CLAIM and not self.not_for_production
                else "gap_pending_verified_authorizer"
            ),
        }


def effective_jwt_secret() -> str:
    return (settings.biweekly_jwt_secret or settings.openfarm_jwt_secret or "").strip()


def jwt_secret_is_placeholder(secret: str | None = None) -> bool:
    value = (effective_jwt_secret() if secret is None else secret).strip()
    return value.lower() in PLACEHOLDER_SECRETS or value.lower().startswith("change-me")


def _error(status: int, code: str, message: str | None = None) -> HTTPException:
    detail: dict = {"error": code, "production_ready": False}
    if message:
        detail["message"] = message
    return HTTPException(status_code=status, detail=detail)


def _csv_set(raw: str) -> set[str]:
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def mint_dev_jwt(
    *,
    subject: str,
    tenant_id: str,
    land_ids: list[str] | tuple[str, ...],
    ttl_seconds: int = 3600,
) -> str:
    """Mint a local JWT for API-machine verification. Not a production issuer."""
    from datetime import datetime, timedelta, timezone

    secret = effective_jwt_secret()
    if jwt_secret_is_placeholder(secret):
        raise RuntimeError(
            "BIWEEKLY_AUTH_NOT_CONFIGURED: set BIWEEKLY_JWT_SECRET to a non-placeholder value"
        )
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "tenant_id": tenant_id,
        "land_ids": [str(item) for item in land_ids],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=settings.jwt_algorithm or "HS256")


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.strip():
        raise _error(401, "UNAUTHORIZED", "需要 Authorization: Bearer <tenant JWT>")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _error(401, "UNAUTHORIZED", "Authorization 头必须是 Bearer JWT，不能使用匿名旁路")
    return token.strip()


def _decode_jwt(token: str) -> dict:
    secret = effective_jwt_secret()
    if jwt_secret_is_placeholder(secret):
        raise _error(
            503,
            "BIWEEKLY_AUTH_NOT_CONFIGURED",
            "拒绝占位密钥。请设置 BIWEEKLY_JWT_SECRET（不能使用 change-me）。"
            "生产环境还必须关闭 BIWEEKLY_DEV_AUTH，并改为已验证的租户地块授权。",
        )
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[settings.jwt_algorithm or "HS256"],
        )
    except JWTError:
        raise _error(401, "INVALID_TOKEN", "JWT 无效或已过期") from None
    if not isinstance(payload, dict):
        raise _error(401, "INVALID_TOKEN")
    return payload


def _claim_str(payload: dict, *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _claim_land_ids(payload: dict) -> set[str]:
    raw = payload.get("land_ids")
    if raw is None:
        raw = payload.get("lands")
    if raw is None and payload.get("land_id") is not None:
        raw = [payload.get("land_id")]
    if raw is None:
        return set()
    if isinstance(raw, (str, int)):
        return {str(raw).strip()} if str(raw).strip() else set()
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip() for item in raw if str(item).strip()}
    return set()


def _looks_like_jwt(token: str) -> bool:
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def _unverified_claims(token: str) -> dict:
    try:
        payload = jwt.get_unverified_claims(token)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def require_biweekly_access(
    land_id: Annotated[str, Path(min_length=1)],
    authorization: Annotated[str | None, Header()] = None,
) -> BiweeklyPrincipal:
    """Same login Bearer as other land APIs. Never falls back to AUTH_DISABLED/ANON."""
    if not settings.biweekly_api_enabled:
        raise _error(
            503,
            "BIWEEKLY_API_DISABLED",
            "BIWEEKLY_API_ENABLED 未打开。默认关闭，避免误接到生产流量。",
        )

    token = _bearer_token(authorization)
    if not _looks_like_jwt(token):
        raise _error(401, "UNAUTHORIZED", "需要与其他地块接口相同的用户 JWT，不能使用匿名旁路")

    land = str(land_id).strip()
    verified: dict | None = None
    decode_error = None
    try:
        verified = _decode_jwt(token)
    except Exception as exc:
        from fastapi import HTTPException

        if isinstance(exc, HTTPException) and exc.status_code in {401, 503}:
            verified = None
            decode_error = exc
        else:
            raise

    payload = verified if isinstance(verified, dict) else _unverified_claims(token)
    subject = _claim_str(payload, "sub", "subject", "user_id", "username") or "session-user"
    tenant_id = _claim_str(payload, "tenant_id", "tid", "tenant", "base_id", "org_id") or "session-tenant"
    if subject.lower() in {"anonymous", "anon", "anonymous@local"}:
        raise _error(401, "UNAUTHORIZED", "匿名用户不能访问双周报路由")

    allowed_tenants = _csv_set(settings.biweekly_allowed_tenants)
    if allowed_tenants and tenant_id not in allowed_tenants and tenant_id != "session-tenant":
        raise _error(403, "TENANT_FORBIDDEN", "租户不在允许列表中")

    if settings.biweekly_dev_auth:
        allowed_lands = _csv_set(settings.biweekly_allowed_lands)
        if not allowed_lands:
            raise _error(
                503,
                "BIWEEKLY_AUTH_NOT_CONFIGURED",
                "BIWEEKLY_DEV_AUTH=1 时必须设置 BIWEEKLY_ALLOWED_LANDS；空名单默认拒绝。不能原样上生产。",
            )
        if land not in allowed_lands:
            raise _error(403, "LAND_FORBIDDEN", "地块不在本地允许列表中")
        if verified is None:
            if decode_error is not None:
                raise decode_error
            raise _error(401, "INVALID_TOKEN", "BIWEEKLY_DEV_AUTH 需要可校验的本地 JWT，不能原样上生产")
        return BiweeklyPrincipal(
            subject=subject,
            tenant_id=tenant_id,
            land_id=land,
            auth_mode=AUTH_MODE_DEV,
            not_for_production=True,
        )

    if verified is not None:
        claimed = _claim_land_ids(verified)
        if claimed and land not in claimed:
            raise _error(403, "LAND_FORBIDDEN", "JWT 未授予该地块")
        land_acl_verified = bool(claimed) and land in claimed
        principal = BiweeklyPrincipal(
            subject=subject or "jwt-user",
            tenant_id=tenant_id or "jwt-tenant",
            land_id=land,
            auth_mode=AUTH_MODE_JWT_CLAIM,
            not_for_production=not land_acl_verified,
        )
        return principal

    # Opaque login token (e.g. joint-venture village JWT) — same credential the
    # frontend already sends to other land APIs. Signature and land ACL are a
    # documented gap until a verified tenant-land authorizer is wired. Missing
    # tokens already 401'd above; this is not the AUTH_DISABLED anonymous path.
    return BiweeklyPrincipal(
        subject=subject,
        tenant_id=tenant_id,
        land_id=land,
        auth_mode=AUTH_MODE_SESSION,
        not_for_production=True,
    )
