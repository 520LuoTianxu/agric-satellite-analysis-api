"""Sentinel-1 STAC / GDAL access helpers (stdlib only).

Element84 ``sentinel-1-grd`` assets live in the AWS Open Data bucket
``sentinel-s1-l1c`` with ``storage:requester_pays: true``. Unsigned
``AWS_NO_SIGN_REQUEST=YES`` (and blanked keys) yields HTTP 403, so STAC
search succeeds but every scene fails and processed stays 0.
"""

from __future__ import annotations

import os
from typing import Any


S1_AWS_BUCKET_HINT = "sentinel-s1-l1c"
S1_REQUESTER_PAYS_REGION = "eu-central-1"


def s1_credential_pair() -> tuple[str | None, str | None]:
    """Prefer S1-specific AWS keys; fall back to process AWS_*. Never OSS keys."""
    key = (os.environ.get("S1_AWS_ACCESS_KEY_ID") or "").strip() or None
    secret = (os.environ.get("S1_AWS_SECRET_ACCESS_KEY") or "").strip() or None
    if key and secret:
        return key, secret
    key = (os.environ.get("AWS_ACCESS_KEY_ID") or "").strip() or None
    secret = (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip() or None
    if key and secret:
        return key, secret
    return None, None


def s1_has_aws_credentials() -> bool:
    key, secret = s1_credential_pair()
    return bool(key and secret)


def s1_gdal_env() -> dict[str, str]:
    """Thread-local GDAL/AWS options for requester-pays S1 GRD COGs.

    Do not set AWS_NO_SIGN_REQUEST=YES and do not blank AWS_ACCESS_KEY_ID:
    that combination cannot read sentinel-s1-l1c. Do not mutate process-wide
    os.environ (would race OSS uploads on the same worker).
    """
    env: dict[str, str] = {
        "AWS_NO_SIGN_REQUEST": "NO",
        "AWS_REQUEST_PAYER": "requester",
        "AWS_VIRTUAL_HOSTING": "TRUE",
        "AWS_HTTPS": "YES",
        "AWS_REGION": S1_REQUESTER_PAYS_REGION,
        "AWS_DEFAULT_REGION": S1_REQUESTER_PAYS_REGION,
        "AWS_S3_ENDPOINT": f"s3.{S1_REQUESTER_PAYS_REGION}.amazonaws.com",
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    }
    key, secret = s1_credential_pair()
    if key and secret:
        env["AWS_ACCESS_KEY_ID"] = key
        env["AWS_SECRET_ACCESS_KEY"] = secret
    return env


def s1_open_path(href: str) -> str:
    """Map an S3 href to a GDAL /vsis3/ path; leave https:// unchanged."""
    if href.startswith("s3://"):
        return href.replace("s3://", "/vsis3/", 1)
    return href


def stac_asset_href(asset: Any) -> str | None:
    """Prefer an https alternate when STAC provides one; else asset.href."""
    if asset is None:
        return None
    extra = getattr(asset, "extra_fields", None)
    if extra is None and isinstance(asset, dict):
        extra = asset
    if isinstance(extra, dict):
        alts = extra.get("alternate") or extra.get("alternates") or {}
        if isinstance(alts, dict):
            for key in ("https", "HTTPS", "http"):
                alt = alts.get(key)
                if isinstance(alt, dict):
                    href = alt.get("href")
                    if href:
                        return str(href)
                elif isinstance(alt, str) and alt.startswith("http"):
                    return alt
    href = getattr(asset, "href", None)
    if href is None and isinstance(asset, dict):
        href = asset.get("href")
    return str(href) if href else None


def s1_missing_credentials_hint() -> str:
    return (
        "sentinel-1-grd assets are requester-pays "
        f"(s3://{S1_AWS_BUCKET_HINT}/, eu-central-1). "
        "Set S1_AWS_ACCESS_KEY_ID and S1_AWS_SECRET_ACCESS_KEY on the download "
        "host (do not reuse Aliyun OSS keys). Unsigned reads return 403."
    )
