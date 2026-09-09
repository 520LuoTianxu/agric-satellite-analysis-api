"""Drought / flood classifiers mirroring apps/web/src/lib/agri-heatmap.ts."""

from __future__ import annotations

from typing import Literal

DroughtClass = Literal["severe", "moderate", "mild", "normal"]
FloodClass = Literal["flood", "wet", "dry"]

CLOUD_MAX_PCT = 30.0
WEAK_NDVI_LT = 0.25
PHENOLOGY_MONTHS = (6, 7, 8, 9)


def compute_nddi(ndvi: float, ndmi: float) -> float | None:
    """NDDI = (NDVI − NDMI) / (NDVI + NDMI); None if denom ≈ 0."""
    denom = ndvi + ndmi
    if abs(denom) < 1e-6:
        return None
    return (ndvi - ndmi) / denom


def classify_drought(ndvi: float | None, ndmi: float | None) -> DroughtClass | None:
    """Classify drought from scene-average NDVI + NDMI (same thresholds as TS)."""
    if ndmi is None or not _finite(ndmi):
        return None
    if ndvi is not None and _finite(ndvi):
        nddi = compute_nddi(float(ndvi), float(ndmi))
        if nddi is not None:
            if nddi >= 0.5 or ndmi < -0.2:
                return "severe"
            if nddi >= 0.3 or ndmi < 0:
                return "moderate"
            if nddi >= 0.1 or ndmi < 0.1:
                return "mild"
            return "normal"
    # NDMI-only fallback
    if ndmi < -0.2:
        return "severe"
    if ndmi < 0:
        return "moderate"
    if ndmi < 0.1:
        return "mild"
    return "normal"


def classify_flood(vv_db: float | None, vh_db: float | None) -> FloodClass | None:
    """Sentinel-1 VV/VH backscatter thresholds (same as agri-heatmap classifyFlood)."""
    if vv_db is None or not _finite(vv_db):
        return None
    vh = float(vh_db) if vh_db is not None and _finite(vh_db) else None
    vv = float(vv_db)
    if vv <= -18 and (vh is None or vh <= -22):
        return "flood"
    if vv <= -18:
        return "flood"
    if vv <= -15 or (vh is not None and vv <= -14 and vh <= -20):
        return "wet"
    return "dry"


def is_clear_scene(
    parcel_cloud_cover_pct: float | None,
    cloud_cover: float | None,
    cloud_cover_over_30: bool | None,
    *,
    cloud_max_pct: float = CLOUD_MAX_PCT,
) -> bool:
    """Optical clear filter: prefer parcel_cloud_cover_pct else cloud_cover."""
    if cloud_cover_over_30 is True:
        return False
    cloud = (
        parcel_cloud_cover_pct if parcel_cloud_cover_pct is not None else cloud_cover
    )
    if cloud is not None and _finite(cloud) and float(cloud) > cloud_max_pct:
        return False
    return True


def _finite(v: float) -> bool:
    return v == v and v not in (float("inf"), float("-inf"))
