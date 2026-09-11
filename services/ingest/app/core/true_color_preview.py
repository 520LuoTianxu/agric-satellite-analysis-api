"""Parcel true-color PNG from S2 B04/B03/B02 → OSS preview URLs."""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import structlog
from PIL import Image

from openfarm_common.settings import settings
from openfarm_common.storage import get_storage

logger = structlog.get_logger()


def field_rgb_oss_key(land_id: str, date_str: str, sensor: str = "S2") -> str:
    """Stable OSS key for parcel true-color PNG (sibling of scene JSON prefix)."""
    json_prefix = (settings.oss_prefix or "s1s2_parcel/json/").rstrip("/")
    # s1s2_parcel/json → s1s2_parcel/img
    if json_prefix.endswith("/json"):
        img_root = json_prefix[: -len("/json")] + "/img"
    else:
        img_root = "s1s2_parcel/img"
    return f"{img_root}/{land_id}/{date_str}_{sensor}/field_rgb.png"


def _percentile_stretch(band: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """2–98% stretch to uint8; masked-out cells → 0."""
    out = np.zeros(band.shape, dtype=np.uint8)
    valid = mask & np.isfinite(band)
    if not np.any(valid):
        return out
    vals = band[valid].astype(np.float64)
    lo, hi = np.percentile(vals, [2, 98])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(np.nanmin(vals))
        hi = float(np.nanmax(vals))
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            return out
    scaled = (band.astype(np.float64) - lo) / (hi - lo)
    scaled = np.clip(scaled, 0.0, 1.0)
    out[valid] = (scaled[valid] * 255.0).astype(np.uint8)
    return out


def render_field_rgb_png(
    bands: dict[str, np.ndarray],
    field_mask: np.ndarray,
) -> bytes | None:
    """Build RGBA PNG (B04/B03/B02); transparent outside parcel mask."""
    need = ("B04", "B03", "B02")
    if any(k not in bands for k in need):
        return None
    if field_mask is None or field_mask.shape != bands["B04"].shape:
        return None
    mask = field_mask.astype(bool)
    r = _percentile_stretch(bands["B04"], mask)
    g = _percentile_stretch(bands["B03"], mask)
    b = _percentile_stretch(bands["B02"], mask)
    alpha = np.where(mask, 255, 0).astype(np.uint8)
    rgba = np.dstack([r, g, b, alpha])
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def upload_field_rgb_preview(
    *,
    land_id: str,
    date_str: str,
    bands: dict[str, np.ndarray],
    field_mask: np.ndarray,
    sensor: str = "S2",
) -> dict[str, str | None]:
    """Render + upload; return rgb_oss_key / rgb_url (nulls on soft failure)."""
    empty = {"rgb_oss_key": None, "rgb_url": None, "large_rgb_url": None}
    try:
        png = render_field_rgb_png(bands, field_mask)
        if not png:
            return empty
        key = field_rgb_oss_key(land_id, date_str, sensor)
        storage = get_storage()
        storage.put_bytes(key, png, content_type="image/png")
        # Private bucket: browser needs signed GET (20y), not bare public_url.
        url = storage.presigned_get(key)
        return {"rgb_oss_key": key, "rgb_url": url, "large_rgb_url": None}
    except Exception as exc:  # noqa: BLE001 — preview must not fail ingest
        logger.warning(
            "field_rgb_upload_failed",
            land_id=land_id,
            date=date_str,
            error=str(exc),
        )
        return empty


