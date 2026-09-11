"""Sentinel-1 STAC / GDAL access helpers (Planetary Computer).

Default catalog is Microsoft Planetary Computer ``sentinel-1-grd``.
Assets live on Azure Blob Storage; unsigned hrefs 404, so HREFs are SAS-signed
via ``planetary_computer`` (no AWS requester-pays keys required).

Optional override: set ``S1_STAC_API_URL`` (must still expose ``sentinel-1-grd``
with readable VV/VH assets after signing when using MPC).
"""

from __future__ import annotations

import os
from typing import Any

# Microsoft Planetary Computer STAC API (default for agri S1).
S1_STAC_API_URL_DEFAULT = "https://planetarycomputer.microsoft.com/api/stac/v1"
S1_STAC_COLLECTION = "sentinel-1-grd"


def s1_stac_api_url() -> str:
    return (
        (os.environ.get("S1_STAC_API_URL") or "").strip()
        or S1_STAC_API_URL_DEFAULT
    )


def s1_uses_planetary_computer() -> bool:
    url = s1_stac_api_url().lower()
    return "planetarycomputer.microsoft.com" in url


def sign_s1_href(href: str) -> str:
    """Attach a Planetary Computer SAS token when using MPC; else return href."""
    if not href:
        return href
    if not s1_uses_planetary_computer():
        return href
    import planetary_computer as pc

    return str(pc.sign(href))


def open_s1_stac_client():
    """Open the S1 STAC client; MPC results are signed in-place."""
    from pystac_client import Client as STACClient

    url = s1_stac_api_url()
    if s1_uses_planetary_computer():
        import planetary_computer as pc

        return STACClient.open(url, modifier=pc.sign_inplace)
    return STACClient.open(url)


def s1_gdal_env() -> dict[str, str]:
    """Thread-local GDAL options for signed HTTPS (Azure) S1 GRD assets.

    Do not mutate process-wide os.environ (would race OSS uploads on the same
    worker). AWS requester-pays knobs are intentionally omitted for the MPC path.
    """
    return {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "GDAL_HTTP_UNSAFESSL": "NO",
        "CPL_VSIL_CURL_USE_HEAD": "NO",
    }


def s1_open_path(href: str) -> str:
    """Sign (if MPC) then map s3:// to /vsis3/; leave https:// for GDAL curl."""
    signed = sign_s1_href(href)
    if signed.startswith("s3://"):
        return signed.replace("s3://", "/vsis3/", 1)
    return signed


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


def s1_access_hint() -> str:
    if s1_uses_planetary_computer():
        return (
            "S1 uses Microsoft Planetary Computer "
            f"({s1_stac_api_url()}, collection={S1_STAC_COLLECTION}). "
            "Assets need SAS signing via planetary_computer; install the "
            "planetary-computer package on the ingest image. No AWS "
            "requester-pays keys are required for this path."
        )
    return (
        f"S1 STAC is {s1_stac_api_url()} (collection={S1_STAC_COLLECTION}). "
        "Ensure VV/VH assets are readable by GDAL from this catalog."
    )


# Back-compat aliases used by older call sites / logs
def s1_has_aws_credentials() -> bool:
    """Deprecated: MPC path needs no AWS keys. Always True for readiness logs."""
    return True


def s1_missing_credentials_hint() -> str:
    return s1_access_hint()
