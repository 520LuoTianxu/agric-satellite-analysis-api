"""Dev-only auth for the standalone biweekly trigger API.

This is NOT production authentication. Do not copy it into
services/api/app/main.py, and do not combine it with AUTH_DISABLED=True.
A future production path must use verified JWT + tenant + land authorization.
"""

from __future__ import annotations

import os

from .models import Principal, ReportError

ENV_ENABLE = "BIWEEKLY_LOCAL_API"
ENV_TOKEN = "BIWEEKLY_DEV_TOKEN"
HEADER_TOKEN = "X-Biweekly-Dev-Token"
HEADER_TENANT = "X-Biweekly-Dev-Tenant"
HEADER_SUBJECT = "X-Biweekly-Dev-Subject"
DEFAULT_TENANT = "dev-local"
DEFAULT_SUBJECT = "dev-operator"


def local_api_enabled(env: dict | None = None) -> bool:
    source = env if env is not None else os.environ
    return source.get(ENV_ENABLE) == "1"


def require_enabled(env: dict | None = None) -> None:
    if not local_api_enabled(env):
        raise ReportError("LOCAL_API_DISABLED")


def expected_token(env: dict | None = None) -> str:
    source = env if env is not None else os.environ
    token = (source.get(ENV_TOKEN) or "").strip()
    if not token or token.lower() in {"change-me", "changeme", "secret"}:
        raise ReportError("DEV_TOKEN_REQUIRED")
    return token


def authenticate(headers: dict, env: dict | None = None) -> Principal:
    require_enabled(env)
    token = expected_token(env)
    incoming = _header(headers, HEADER_TOKEN)
    if incoming != token:
        raise ReportError("ACCESS_DENIED")
    tenant = _header(headers, HEADER_TENANT) or DEFAULT_TENANT
    subject = _header(headers, HEADER_SUBJECT) or DEFAULT_SUBJECT
    if not subject or not tenant:
        raise ReportError("ACCESS_DENIED")
    return Principal(subject=subject, tenant_id=tenant)


def _header(headers: dict, name: str) -> str:
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    return lowered.get(name.lower(), "").strip()
