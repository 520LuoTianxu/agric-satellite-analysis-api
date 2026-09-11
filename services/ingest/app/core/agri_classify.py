"""Drought / flood classifiers mirroring apps/web/src/lib/agri-heatmap.ts."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

DroughtClass = Literal["severe", "moderate", "mild", "normal"]
FloodClass = Literal["flood_severe", "flood_moderate", "flood_mild", "dry"]

CLOUD_MAX_PCT = 30.0
WEAK_NDVI_LT = 0.25
PHENOLOGY_MONTHS = (6, 7, 8, 9)

# NDDI-primary drought bands for agri parcels (keep in sync with
# apps/web/src/lib/agri-heatmap.ts). Citations:
# - Gu, Brown, Verdin & Wardlow, 2007, Geophys. Res. Lett.:
#   NDDI = (NDVI - NDWI) / (NDVI + NDWI); higher NDDI = drier.
#   Gao (1996) NDMI (NIR/SWIR) stands in for NDWI here.
# - Later categorical NDDI applications (e.g. Frontiers in Env. Sci. 2023
#   and tropical NDDI papers) commonly use ~0.1-wide bins:
#   0-0.1 dry, 0.1-0.2 moderate, 0.2-0.3 severe, >=0.3-0.4 extreme.
# Agri mapping (four UI classes): literature 0 / 0.1 / 0.2 / 0.3 bins
# shifted +0.3 because 10 m crop canopy often has NDVI 0.6-0.8 and NDMI
# 0.2-0.4 (NDDI already ~0.2-0.5 when well watered). Copying 0.2 as
# "severe" would paint most green fields as drought. Result:
#   NDDI < 0.3           normal
#   0.3 <= NDDI < 0.4    mild
#   0.4 <= NDDI < 0.5    moderate
#   NDDI >= 0.5          severe (Gu-like high NDDI; previous NDDI severe)
# Loose NDMI OR shortcuts (ndmi < 0.1 / 0 / -0.2) over-flag healthy canopy.
NDDI_MILD_MIN = 0.3
NDDI_MODERATE_MIN = 0.4
NDDI_SEVERE_MIN = 0.5
NDMI_FALLBACK_SEVERE = -0.2

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
    """Classify drought from NDVI + NDMI (same thresholds as TS heatmap).

    NDDI-primary. NDMI is only a last-resort fallback when NDDI cannot be
    formed, and then only ndmi < -0.2 maps to severe (not mild/moderate).
    """
    if ndvi is not None and _finite(ndvi) and ndmi is not None and _finite(ndmi):
        nddi = compute_nddi(float(ndvi), float(ndmi))
        if nddi is not None:
            if nddi >= NDDI_SEVERE_MIN:
                return "severe"
            if nddi >= NDDI_MODERATE_MIN:
                return "moderate"
            if nddi >= NDDI_MILD_MIN:
                return "mild"
            return "normal"
    if ndmi is None or not _finite(ndmi):
        return None
    if ndmi < NDMI_FALLBACK_SEVERE:
        return "severe"
    return "normal"


def scene_cloud_fields(
    stac_cloud: float | None,
    ndvi_quality: float | None,
    *,
    cloud_max_pct: float = CLOUD_MAX_PCT,
) -> tuple[float | None, bool, float | None]:
    """Return (cloud_cover, cloud_cover_over_30, parcel_cloud_cover_pct).

    When STAC scene cloud > cloud_max_pct, skip parcel cloud metrics
    (leave parcel_cloud_cover_pct None) and flag over_30 from STAC.
    """
    stac: float | None = None
    if stac_cloud is not None:
        try:
            stac_f = float(stac_cloud)
        except (TypeError, ValueError):
            stac_f = None
        if stac_f is not None and _finite(stac_f):
            stac = stac_f
    if stac is not None and stac > cloud_max_pct:
        return stac, True, None

    parcel: float | None = None
    if ndvi_quality is not None:
        try:
            qf = float(ndvi_quality)
        except (TypeError, ValueError):
            qf = None
        else:
            if _finite(qf):
                parcel = max(0.0, min(100.0, (1.0 - qf) * 100.0))
    over = bool(parcel is not None and parcel > cloud_max_pct)
    return stac, over, parcel


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


# Additive UnCRtainTS decloud products (keep in sync with app.core.decloud
# and apps/web/src/lib/agri-heatmap.ts).
DECLOUD_SOURCE = "uncrtaints_decloud"
DECLOUD_SCENE_ID_SUFFIX = "_decloud"
DECLOUD_QUALITY_GOOD = "good"


def is_decloud_product(
    source: str | None = None,
    scene_id: str | None = None,
) -> bool:
    """True when the row is the additive decloud product, not raw S2."""
    if source == DECLOUD_SOURCE:
        return True
    if scene_id and str(scene_id).endswith(DECLOUD_SCENE_ID_SUFFIX):
        return True
    return False


def is_official_optical_product(
    *,
    source: str | None = None,
    scene_id: str | None = None,
    decloud_quality: str | None = None,
    parcel_cloud_cover_pct: float | None = None,
    cloud_cover: float | None = None,
    cloud_cover_over_30: bool | None = None,
    cloud_max_pct: float = CLOUD_MAX_PCT,
) -> bool:
    """Whether a scene may feed drought / timeseries / land metrics.

    Raw S2 still uses the cloud > 30% skip. Decloud rows are official only
    when quality is ``good``. ``fair`` / ``bad`` stay stored for audit.
    """
    if is_decloud_product(source, scene_id):
        return (decloud_quality or "").strip().lower() == DECLOUD_QUALITY_GOOD
    return is_clear_scene(
        parcel_cloud_cover_pct,
        cloud_cover,
        cloud_cover_over_30,
        cloud_max_pct=cloud_max_pct,
    )


def official_s2_sql(
    alias: str = "s",
    *,
    cloud_param: str = "cloud_max",
) -> str:
    """SQL predicate: clear raw S2, or good-quality decloud only."""
    a = f"{alias}." if alias else ""
    return f"""(
      (
        COALESCE({a}pixel_data->>'source', '') <> '{DECLOUD_SOURCE}'
        AND COALESCE({a}scene_id, '') NOT LIKE '%{DECLOUD_SCENE_ID_SUFFIX}'
        AND NOT (
          coalesce({a}parcel_cloud_cover_pct, {a}cloud_cover) > :{cloud_param}
          OR {a}cloud_cover_over_30 IS TRUE
        )
      )
      OR (
        (
          {a}pixel_data->>'source' = '{DECLOUD_SOURCE}'
          OR {a}scene_id LIKE '%{DECLOUD_SCENE_ID_SUFFIX}'
        )
        AND {a}pixel_data->>'decloud_quality' = '{DECLOUD_QUALITY_GOOD}'
      )
    )"""


def _finite(v: float) -> bool:
    return v == v and v not in (float("inf"), float("-inf"))
