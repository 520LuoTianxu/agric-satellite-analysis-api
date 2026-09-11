"""Parcel + scene-context true-color previews from S2 visual or B04/B03/B02.

Uses a reflectance-aware *joint* stretch (shared lo/hi) so agricultural RGB
keeps natural band ratios instead of the washed cyan/white look from
independent per-band percentile stretches on tiny parcel masks.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import structlog
from PIL import Image

from openfarm_common.settings import settings
from openfarm_common.storage import get_storage

logger = structlog.get_logger()

# Fixed display window in reflectance units (typical vegetated S2 landscapes).
_DISPLAY_LO = 0.02
_DISPLAY_HI = 0.35
_DISPLAY_GAMMA = 1.2


def field_rgb_oss_key(land_id: str, date_str: str, sensor: str = "S2") -> str:
    """Stable OSS key for parcel true-color PNG (sibling of scene JSON prefix)."""
    return f"{_img_root()}/{land_id}/{date_str}_{sensor}/field_rgb.png"


def scene_rgb_oss_key(land_id: str, date_str: str, sensor: str = "S2") -> str:
    """Stable OSS key for opaque scene-context true-color JPEG."""
    return f"{_img_root()}/{land_id}/{date_str}_{sensor}/scene_rgb.jpg"


def _img_root() -> str:
    json_prefix = (settings.oss_prefix or "s1s2_parcel/json/").rstrip("/")
    if json_prefix.endswith("/json"):
        return json_prefix[: -len("/json")] + "/img"
    return "s1s2_parcel/img"



def compute_scene_preview_grid(
    field_bounds: tuple[float, float, float, float],
    *,
    pad_km: float = 1.5,
    target_min_px: int = 512,
    target_max_px: int = 1024,
    pixel_size: float = 0.0001,
) -> tuple[Any, tuple[int, int], tuple[float, float, float, float]]:
    """Padded lon/lat grid for opaque scene-context true-color (~1–2 km).

    Returns ``(transform, (height, width), bounds)`` in EPSG:4326.
    """
    import math

    from rasterio.transform import from_bounds

    minx, miny, maxx, maxy = [float(v) for v in field_bounds]
    lat_c = 0.5 * (miny + maxy)
    cos_lat = max(0.2, abs(math.cos(math.radians(lat_c))))
    pad_lat = float(pad_km) / 111.0
    pad_lon = float(pad_km) / (111.0 * cos_lat)
    minx -= pad_lon
    maxx += pad_lon
    miny -= pad_lat
    maxy += pad_lat

    width = max(int((maxx - minx) / pixel_size), 1)
    height = max(int((maxy - miny) / pixel_size), 1)
    longest = max(width, height)
    if longest > target_max_px:
        scale = target_max_px / longest
        width = max(int(width * scale), 1)
        height = max(int(height * scale), 1)
    elif longest < target_min_px:
        # Grow pad until the longer side reaches target_min_px.
        scale = target_min_px / max(longest, 1)
        cx, cy = 0.5 * (minx + maxx), 0.5 * (miny + maxy)
        half_w = 0.5 * (maxx - minx) * scale
        half_h = 0.5 * (maxy - miny) * scale
        minx, maxx = cx - half_w, cx + half_w
        miny, maxy = cy - half_h, cy + half_h
        width = max(int((maxx - minx) / pixel_size), 1)
        height = max(int((maxy - miny) / pixel_size), 1)
        longest = max(width, height)
        if longest > target_max_px:
            scale = target_max_px / longest
            width = max(int(width * scale), 1)
            height = max(int(height * scale), 1)

    transform = from_bounds(minx, miny, maxx, maxy, width, height)
    return transform, (height, width), (minx, miny, maxx, maxy)


def _as_reflectance(arr: np.ndarray) -> np.ndarray:
    """Map DN or reflectance to ~0–1 reflectance.

    Sentinel-2 L2A COGs are commonly stored as uint16 DN scaled by 10000.
    If max DN > 1.5 treat as 0–10000; otherwise assume already reflectance.
    """
    a = arr.astype(np.float64, copy=False)
    finite = a[np.isfinite(a)]
    if finite.size == 0:
        return a
    if float(np.nanmax(finite)) > 1.5:
        return a / 10000.0
    return a


def _is_display_uint8_rgb(rgb: np.ndarray) -> bool:
    """True when array already looks like display-ready uint8 RGB."""
    if rgb.dtype == np.uint8:
        return True
    finite = rgb[np.isfinite(rgb)]
    if finite.size == 0:
        return False
    mx = float(np.nanmax(finite))
    mn = float(np.nanmin(finite))
    return mx > 1.5 and mx <= 255.0 and mn >= 0.0


def _normalize_visual_rgb(
    visual: np.ndarray | bytes | bytearray | memoryview,
) -> np.ndarray | None:
    """Accept HxWx3 / 3xHxW ndarray or encoded image bytes → HxWx3 float/uint8."""
    if isinstance(visual, (bytes, bytearray, memoryview)):
        try:
            img = Image.open(io.BytesIO(bytes(visual))).convert("RGB")
            return np.asarray(img)
        except Exception as exc:  # noqa: BLE001
            logger.warning("visual_bytes_decode_failed", error=str(exc))
            return None
    arr = np.asarray(visual)
    if arr.ndim == 3 and arr.shape[0] == 3 and arr.shape[-1] != 3:
        arr = np.transpose(arr, (1, 2, 0))
    if arr.ndim != 3 or arr.shape[-1] < 3:
        return None
    return arr[..., :3]


def joint_stretch_rgb(
    r: np.ndarray,
    g: np.ndarray,
    b: np.ndarray,
    *,
    valid_mask: np.ndarray | None = None,
    lo: float = _DISPLAY_LO,
    hi: float = _DISPLAY_HI,
    gamma: float = _DISPLAY_GAMMA,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Shared lo/hi stretch + optional gamma → three uint8 bands.

    Preserves relative band ratios (unlike independent percentile stretch).
    When ``valid_mask`` is set, only those pixels contribute to optional
    luminance refinement; output outside the mask is left at 0.
    """
    rf = _as_reflectance(r)
    gf = _as_reflectance(g)
    bf = _as_reflectance(b)

    if valid_mask is not None:
        mask = valid_mask.astype(bool) & np.isfinite(rf) & np.isfinite(gf) & np.isfinite(
            bf
        )
    else:
        mask = np.isfinite(rf) & np.isfinite(gf) & np.isfinite(bf)

    use_lo, use_hi = float(lo), float(hi)
    if np.any(mask):
        # Mild luminance-based refinement when fixed window is empty/flat.
        lum = (0.2126 * rf + 0.7152 * gf + 0.0722 * bf)[mask]
        p2, p98 = np.percentile(lum, [2, 98])
        if np.isfinite(p2) and np.isfinite(p98) and p98 > p2:
            # Blend toward data range but keep agricultural display window bias.
            use_lo = max(0.0, min(use_lo, float(p2)))
            use_hi = min(1.0, max(use_hi, float(p98)))
            if use_hi <= use_lo:
                use_lo, use_hi = float(p2), float(p98)

    def _one(band: np.ndarray) -> np.ndarray:
        out = np.zeros(band.shape, dtype=np.uint8)
        if use_hi <= use_lo:
            return out
        scaled = (band - use_lo) / (use_hi - use_lo)
        scaled = np.clip(scaled, 0.0, 1.0)
        if gamma and abs(gamma - 1.0) > 1e-6:
            scaled = np.power(scaled, 1.0 / gamma)
        out[mask] = (scaled[mask] * 255.0).astype(np.uint8)
        return out

    return _one(rf), _one(gf), _one(bf)


def _rgb_from_visual(visual_rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split visual into R,G,B uint8 (pass-through or joint stretch)."""
    if _is_display_uint8_rgb(visual_rgb):
        u8 = np.clip(visual_rgb, 0, 255).astype(np.uint8)
        return u8[..., 0], u8[..., 1], u8[..., 2]
    return joint_stretch_rgb(
        visual_rgb[..., 0],
        visual_rgb[..., 1],
        visual_rgb[..., 2],
        valid_mask=None,
    )


def _rgb_from_bands(
    bands: dict[str, np.ndarray],
    *,
    valid_mask: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    need = ("B04", "B03", "B02")
    if any(k not in bands for k in need):
        return None
    return joint_stretch_rgb(
        bands["B04"],
        bands["B03"],
        bands["B02"],
        valid_mask=valid_mask,
    )


def render_field_rgb_png(
    bands: dict[str, np.ndarray],
    field_mask: np.ndarray,
    *,
    visual: np.ndarray | bytes | None = None,
) -> bytes | None:
    """Build RGBA PNG (B04=R, B03=G, B02=B); transparent outside parcel mask.

    Prefers STAC visual / true_color when provided; else joint-stretched bands.
    """
    if field_mask is None:
        return None
    mask = field_mask.astype(bool)

    r = g = b = None
    if visual is not None:
        vis = _normalize_visual_rgb(visual)
        if vis is not None and vis.shape[:2] == mask.shape:
            r, g, b = _rgb_from_visual(vis)
        elif vis is not None and vis.shape[:2] != mask.shape:
            logger.warning(
                "visual_shape_mismatch_field",
                visual_shape=list(vis.shape),
                mask_shape=list(mask.shape),
            )

    if r is None:
        rgb = _rgb_from_bands(bands, valid_mask=mask)
        if rgb is None:
            return None
        if bands["B04"].shape != mask.shape:
            return None
        r, g, b = rgb

    # Punch out outside parcel (keep zeros already set by joint stretch).
    r = np.where(mask, r, 0).astype(np.uint8)
    g = np.where(mask, g, 0).astype(np.uint8)
    b = np.where(mask, b, 0).astype(np.uint8)
    alpha = np.where(mask, 255, 0).astype(np.uint8)
    rgba = np.dstack([r, g, b, alpha])
    img = Image.fromarray(rgba, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_scene_rgb_jpeg(
    bands: dict[str, np.ndarray] | None = None,
    *,
    visual: np.ndarray | bytes | None = None,
    quality: int = 85,
) -> bytes | None:
    """Opaque true-color JPEG for scene-context / large preview (no mask punch-out)."""
    r = g = b = None
    if visual is not None:
        vis = _normalize_visual_rgb(visual)
        if vis is not None:
            r, g, b = _rgb_from_visual(vis)

    if r is None and bands is not None:
        # Full window (including padding around parcel) — use all finite pixels.
        shape = None
        for k in ("B04", "B03", "B02"):
            if k in bands:
                shape = bands[k].shape
                break
        if shape is not None:
            valid = np.ones(shape, dtype=bool)
            for k in ("B04", "B03", "B02"):
                if k in bands:
                    valid &= np.isfinite(bands[k]) & (bands[k] != 0)
            rgb = _rgb_from_bands(bands, valid_mask=valid)
            if rgb is not None:
                r, g, b = rgb

    if r is None:
        return None

    rgb = np.dstack([r, g, b])
    img = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def upload_field_rgb_preview(
    *,
    land_id: str,
    date_str: str,
    bands: dict[str, np.ndarray],
    field_mask: np.ndarray,
    sensor: str = "S2",
    visual: np.ndarray | bytes | None = None,
    scene_bands: dict[str, np.ndarray] | None = None,
    scene_visual: np.ndarray | bytes | None = None,
) -> dict[str, str | None]:
    """Render + upload parcel PNG and optional large scene JPEG.

    Returns rgb_oss_key / rgb_url / large_rgb_url (nulls on soft failure).

    - ``visual``: optional parcel-aligned RGB for field_rgb only.
    - ``scene_visual`` / ``scene_bands``: padded landscape window for large_rgb.
      When neither is provided, falls back to parcel ``bands`` (better stretch
      than nothing, but not true landscape context).
    """
    empty: dict[str, str | None] = {
        "rgb_oss_key": None,
        "rgb_url": None,
        "large_rgb_url": None,
    }
    out = dict(empty)
    try:
        storage = get_storage()
        png = render_field_rgb_png(bands, field_mask, visual=visual)
        if png:
            key = field_rgb_oss_key(land_id, date_str, sensor)
            storage.put_bytes(key, png, content_type="image/png")
            out["rgb_oss_key"] = key
            out["rgb_url"] = storage.presigned_get(key)

        large_visual = scene_visual if scene_visual is not None else None
        large_src = scene_bands if scene_bands is not None else (
            None if large_visual is not None else bands
        )
        jpg = render_scene_rgb_jpeg(large_src, visual=large_visual)
        if jpg:
            large_key = scene_rgb_oss_key(land_id, date_str, sensor)
            storage.put_bytes(large_key, jpg, content_type="image/jpeg")
            out["large_rgb_url"] = storage.presigned_get(large_key)
            if out["rgb_oss_key"] is None:
                out["rgb_oss_key"] = large_key

        return out
    except Exception as exc:  # noqa: BLE001 — preview must not fail ingest
        logger.warning(
            "field_rgb_upload_failed",
            land_id=land_id,
            date=date_str,
            error=str(exc),
        )
        return empty


# Back-compat alias used by older call sites / scripts.
def _percentile_stretch(band: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Deprecated independent stretch — kept for import compatibility."""
    out = np.zeros(band.shape, dtype=np.uint8)
    valid = mask & np.isfinite(band)
    if not np.any(valid):
        return out
    r, g, b = joint_stretch_rgb(band, band, band, valid_mask=valid)
    return r
