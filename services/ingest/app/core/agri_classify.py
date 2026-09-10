"""Drought / flood classifiers mirroring apps/web/src/lib/agri-heatmap.ts."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

DroughtClass = Literal["severe", "moderate", "mild", "normal"]
FloodClass = Literal["flood_severe", "flood_moderate", "flood_mild", "dry"]

CLOUD_MAX_PCT = 30.0
WEAK_NDVI_LT = 0.25
PHENOLOGY_MONTHS = (6, 7, 8, 9)

# Legacy S2 grid pixel tuple: [row, col, evi, cire, ndmi, ndre, ndvi, mndwi]
# Mirrors apps/web/src/lib/agri-heatmap.ts S2_VALUE_INDEX.
S2_GRID_NDMI_IDX = 4
S2_GRID_NDVI_IDX = 6


def compute_nddi(ndvi: float, ndmi: float) -> float | None:
    """NDDI = (NDVI − NDMI) / (NDVI + NDMI); None if denom ≈ 0."""
    denom = ndvi + ndmi
    if abs(denom) < 1e-6:
        return None
    return (ndvi - ndmi) / denom


def classify_drought(ndvi: float | None, ndmi: float | None) -> DroughtClass | None:
    """Classify drought from NDVI + NDMI (same thresholds as TS heatmap)."""
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
    """Sentinel-1 VV/VH finer flood tiers (重/中/轻 + dry).

    - flood_severe (重): VV ≤ -20, or (VV ≤ -18 and VH ≤ -24)
    - flood_moderate (中): VV ≤ -18 (former open-water / flood)
    - flood_mild (轻): former wet band — VV ≤ -15 or (VV ≤ -14 and VH ≤ -20)
    - dry: else
    """
    if vv_db is None or not _finite(vv_db):
        return None
    vh = float(vh_db) if vh_db is not None and _finite(vh_db) else None
    vv = float(vv_db)
    if vv <= -20 or (vv <= -18 and vh is not None and vh <= -24):
        return "flood_severe"
    if vv <= -18:
        return "flood_moderate"
    if vv <= -15 or (vh is not None and vv <= -14 and vh <= -20):
        return "flood_mild"
    return "dry"


def is_open_water_flood(cls: FloodClass | str | None) -> bool:
    """Severe + moderate ≈ open water (backward-compat ``flood`` count)."""
    return cls in ("flood_severe", "flood_moderate")


def is_flood_alert(cls: FloodClass | str | None) -> bool:
    """Any flooded / wet tier for map choropleth."""
    return cls in ("flood_severe", "flood_moderate", "flood_mild")


def _pixel_ndvi_ndmi_pairs(pixel_data: Any) -> list[tuple[float, float]]:
    """Extract (ndvi, ndmi) pairs from lonlat_v1 or legacy grid pixel_data."""
    if not isinstance(pixel_data, dict):
        return []
    out: list[tuple[float, float]] = []
    fmt = pixel_data.get("format")
    pixels = pixel_data.get("pixels")
    if not isinstance(pixels, list):
        return []

    if fmt == "lonlat_v1":
        for p in pixels:
            if not isinstance(p, dict):
                continue
            # Prefer clear pixels when flag present
            if "clear" in p and p.get("clear") == 0:
                continue
            ndvi = _num(p.get("NDVI", p.get("ndvi")))
            ndmi = _num(p.get("NDMI", p.get("ndmi")))
            if ndvi is None or ndmi is None:
                continue
            out.append((ndvi, ndmi))
        return out

    # Legacy grid: {grid, pixels:[[i,j,evi,cire,ndmi,ndre,ndvi,mndwi], ...]}
    if pixel_data.get("grid") is not None or fmt in (None, "", "grid"):
        need = max(S2_GRID_NDMI_IDX, S2_GRID_NDVI_IDX)
        for row in pixels:
            if not isinstance(row, (list, tuple)) or len(row) <= need:
                continue
            ndvi = _num(row[S2_GRID_NDVI_IDX])
            ndmi = _num(row[S2_GRID_NDMI_IDX])
            if ndvi is None or ndmi is None:
                continue
            out.append((ndvi, ndmi))
        return out

    return []


def classify_drought_from_pixels(
    pixel_data: Any,
) -> tuple[DroughtClass | None, float | None, int]:
    """Majority drought class over pixels; also severe_pixel_share and n.

    Returns (class, severe_pixel_share, n_classified). Share is None if n=0.
    """
    pairs = _pixel_ndvi_ndmi_pairs(pixel_data)
    if not pairs:
        return None, None, 0
    classes: list[DroughtClass] = []
    for ndvi, ndmi in pairs:
        cls = classify_drought(ndvi, ndmi)
        if cls is not None:
            classes.append(cls)
    if not classes:
        return None, None, 0
    counts = Counter(classes)
    majority = counts.most_common(1)[0][0]
    severe_share = counts.get("severe", 0) / len(classes)
    return majority, severe_share, len(classes)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if _finite(f) else None


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
