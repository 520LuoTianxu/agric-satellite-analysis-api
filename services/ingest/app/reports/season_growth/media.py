# -*- coding: utf-8 -*-
"""Download season-growth RGB previews into a temp dir for PDF embedding."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _save_bytes(tag: str, data: bytes, out_dir: Path) -> Path | None:
    if not data:
        return None
    ext = ".png"
    if data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    elif data[:4] == b"RIFF":
        ext = ".webp"
    path = out_dir / f"{tag}{ext}"
    path.write_bytes(data)
    return path if path.exists() else None


def _resolve_url(url: str | None, oss_key: str | None) -> str | None:
    """Prefer existing URL; if missing/empty, re-sign from rgb_oss_key."""
    if url and isinstance(url, str) and url.strip().startswith("http"):
        return url.strip()
    if not oss_key:
        return None
    try:
        from openfarm_common.storage import get_parcel_product_storage

        storage = get_parcel_product_storage()
        return storage.presigned_get(str(oss_key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("presign rgb_oss_key failed: %s", exc)
        return None


def download_spatial_media(
    spatial: dict[str, Any] | None,
    out_dir: Path | str,
) -> dict[str, Path]:
    """Download latest/peak RGB into out_dir; mutate spatial with local paths.

    Returns logical-name -> Path (latest_rgb, peak_rgb, …).
    """
    from app.reports.land_assessment.data_loader import download_url_bytes

    spatial = spatial if isinstance(spatial, dict) else {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    def _one(
        *,
        tag: str,
        url: str | None,
        oss_key: str | None,
        path_keys: tuple[str, ...],
    ) -> Path | None:
        resolved = _resolve_url(url, oss_key)
        if not resolved:
            return None
        data = download_url_bytes(resolved)
        if not data and oss_key:
            # URL may be expired — re-sign and retry once
            resolved2 = _resolve_url(None, oss_key)
            if resolved2 and resolved2 != resolved:
                data = download_url_bytes(resolved2)
        if not data:
            logger.warning("RGB download failed for %s", tag)
            return None
        path = _save_bytes(tag, data, out_dir)
        if path is None:
            return None
        written[tag] = path
        for k in path_keys:
            spatial[k] = str(path)
        return path

    # Prefer large_rgb for PDF readability (field_rgb can be tiny parcel chips)
    latest = _one(
        tag="latest_rgb",
        url=(
            spatial.get("latest_large_rgb_url")
            or spatial.get("large_rgb_url")
            or spatial.get("latest_rgb_url")
            or spatial.get("rgb_url")
        ),
        oss_key=spatial.get("latest_rgb_oss_key"),
        path_keys=("latest_rgb_path", "rgb_local_path"),
    )
    if latest is None:
        _one(
            tag="latest_rgb",
            url=spatial.get("latest_rgb_url") or spatial.get("rgb_url"),
            oss_key=spatial.get("latest_rgb_oss_key"),
            path_keys=("latest_rgb_path", "rgb_local_path"),
        )

    peak_date = spatial.get("peak_rgb_date")
    latest_date = spatial.get("latest_rgb_date")
    if peak_date and peak_date != latest_date:
        _one(
            tag="peak_rgb",
            url=(
                spatial.get("peak_large_rgb_url")
                or spatial.get("peak_rgb_url")
            ),
            oss_key=spatial.get("peak_rgb_oss_key"),
            path_keys=("peak_rgb_path",),
        )
    elif spatial.get("peak_rgb_url") and not spatial.get("peak_rgb_path"):
        # Same scene as latest — alias path if already downloaded
        if spatial.get("latest_rgb_path"):
            spatial["peak_rgb_path"] = spatial["latest_rgb_path"]

    return written
