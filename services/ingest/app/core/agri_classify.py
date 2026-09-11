"""Drought / flood classifiers mirroring apps/web/src/lib/agri-classify.ts.

Keep this file in sync with services/ingest/app/core/agri_classify.py.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import date as date_cls
from typing import Any, Literal, TypedDict

DroughtClass = Literal[
    "severe", "moderate", "mild", "normal", "unreliable", "out_of_season"
]
DroughtPixelClass = Literal["severe", "moderate", "mild", "normal"]
FloodClass = Literal["flood_severe", "flood_moderate", "watch", "dry"]
# Backward alias used by overview JSON (watch used to be flood_mild).
FloodOverviewClass = Literal[
    "flood_severe", "flood_moderate", "flood_mild", "watch", "dry"
]

CLOUD_MAX_PCT = 30.0
WEAK_NDVI_LT = 0.25
# Growing-season window for official drought (June-September). Override per call.
PHENOLOGY_MONTHS = (6, 7, 8, 9)

# ESA SCL classes treated as cloud/shadow inside the field (not nodata=0).
# 3=cloud shadow, 8=medium cloud, 9=high cloud, 10=thin cirrus.
SCL_CLOUD_CLASSES = frozenset({3, 8, 9, 10})
# Sen2Cor L2A SCL is 0-11. 0=nodata; 1-11 are real classes. Values outside
# that range (palette RGB, reflectance DN, fill 255) must not count as clear.
SCL_CLASS_MIN = 0
SCL_CLASS_MAX = 11
SCL_VALID_MIN = 1
SCL_VALID_MAX = 11

# How parcel_cloud_cover_pct was computed. Trusted sources are in-polygon.
# Missing / unknown = legacy zonal quality_score (window fill), untrustworthy.
PARCEL_CLOUD_SOURCE_SCL = "scl"
PARCEL_CLOUD_SOURCE_LONLAT = "lonlat_clear"
PARCEL_CLOUD_SOURCES_TRUSTED = frozenset(
    {PARCEL_CLOUD_SOURCE_SCL, PARCEL_CLOUD_SOURCE_LONLAT}
)

# Legacy window-fill artifact: parcel = (1 - finite/window)*100.
# Small padded parcels cluster ~70-90% while STAC eo:cloud_cover varies.
# Only applied when parcel_cloud_source is not a trusted in-polygon source.
LEGACY_PARCEL_CLOUD_MIN = 70.0
LEGACY_STAC_CLOUD_MAX = 40.0
LEGACY_PARCEL_STAC_GAP = 40.0
# If almost all lonlat pixels are clear but stored parcel cloud is high, prefer STAC.
CLEAR_PIXEL_FRACTION_TRUST = 0.9
SUSPICIOUS_PARCEL_VS_CLEAR = 50.0
# Inverse artifact: parcel ~0% while STAC eo:cloud_cover is nearly overcast.
# Caused by nodata/out-of-range SCL counted as clear, missing SCL defaulting
# clear=1, or good-decloud rows forcing parcel_cloud_cover_pct=0.
SUSPICIOUS_CLEAR_PARCEL_MAX = 5.0
SUSPICIOUS_STAC_OVERCAST_MIN = 80.0
# Trusted SCL/lonlat can still under-report vs Element84 eo:cloud_cover
# (tiny clear hole, bad SCL read). Prefer STAC when the gap is large.
SUSPICIOUS_STAC_OVER_PARCEL_GAP = 25.0
SUSPICIOUS_STAC_MIN = 20.0

# Official pick: compare raw vs good decloud to nearby clear dates.
NEARBY_CLEAR_DAYS = 45
BORDERLINE_PARCEL_MIN = 20.0
BORDERLINE_PARCEL_MAX = 40.0
# NDVI / growth pick (not drought): prefer the product whose canopy index
# matches nearby clear-raw phenology. A "good" reconstruct that sits far
# from the seasonal baseline loses to raw when raw fits better.
PHYSIOLOGY_NDVI_ABSURD_GAP = 0.20
PHYSIOLOGY_GROWING_NDVI_FLOOR = 0.15
PHYSIOLOGY_GREEN_NEIGHBOR_NDVI = 0.40

# NDDI-primary drought bands for agri parcels (keep in sync with
# apps/web/src/lib/agri-classify.ts). Citations:
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

# Multi-indicator drought (scene / timeseries). Official only, Jun-Sep.
# Flag drought when (NDDI absolute OR same-month NDDI percentile) AND
# (NDMI dry OR NDVI drop vs same-month median). Fair/bad decloud is excluded
# from both the scene under test and the month baselines.
NDDI_PCTL_DRY = 80.0
MIN_MONTH_SAMPLES = 3
NDMI_DRY_ABS = 0.10
NDMI_DROP_VS_MEDIAN = 0.05
NDVI_DROP_VS_MEDIAN = 0.08
NDVI_DROP_MODERATE = 0.12
NDVI_DROP_SEVERE = 0.20

# Sentinel-1 flood (scene-level). Flood only when ALL of:
#   VV <= -17 dB, VV - per-orbit baseline <= -3 dB,
#   helper (VH <= -22 dB OR VV-VH diff <= per-orbit p40).
# Watch is near-threshold. VV-VH alone never flags flood.
# Spring (Mar-May) hits may be puddling / irrigation, not disaster flood.
FLOOD_VV_MAX = -17.0
FLOOD_VV_DROP = -3.0
FLOOD_VH_MAX = -22.0
WATCH_VV_MAX = -15.0
WATCH_VV_DROP = -2.0
WATCH_VH_MAX = -20.0
FLOOD_VV_SEVERE = -20.0
MIN_ORBIT_SAMPLES = 3
VV_VH_DIFF_PCTL = 40.0
FLOOD_SPRING_MONTHS = (3, 4, 5)

# Legacy S2 grid pixel tuple: [row, col, evi, cire, ndmi, ndre, ndvi, mndwi]
# Mirrors apps/web/src/lib/agri-heatmap.ts S2_VALUE_INDEX.
S2_GRID_NDMI_IDX = 4
S2_GRID_NDVI_IDX = 6

# ESA relative-orbit offsets (175-orbit cycle).
_S1_ORBIT_OFFSET = {"S1A": 73, "S1B": 27, "S1C": 172}
_S1_ID_RE = re.compile(
    r"^(S1[ABC])_IW_GRD[HM]?_1S[DS][VH]_"
    r"\d{8}T\d{6}_\d{8}T\d{6}_(\d{6})",
    re.IGNORECASE,
)

# Additive UnCRtainTS decloud products (keep in sync with ingest
# app.core.decloud / agri_classify and apps/web agri-classify.ts).
DECLOUD_SOURCE = "uncrtaints_decloud"
DECLOUD_SCENE_ID_SUFFIX = "_decloud"
DECLOUD_QUALITY_GOOD = "good"
DECLOUD_QUALITY_FAIR = "fair"
DECLOUD_QUALITY_BAD = "bad"


class OpticalObs(TypedDict, total=False):
    date: str
    ndvi: float | None
    ndmi: float | None
    official: bool
    source: str | None
    scene_id: str | None
    decloud_quality: str | None
    cloud_cover: float | None
    parcel_cloud_cover_pct: float | None
    parcel_cloud_source: str | None
    cloud_cover_over_30: bool | None


class SarObs(TypedDict, total=False):
    date: str
    vv: float | None
    vh: float | None
    scene_id: str | None
    relative_orbit: int | None


def compute_nddi(ndvi: float, ndmi: float) -> float | None:
    """NDDI = (NDVI - NDMI) / (NDVI + NDMI); None if denom ~ 0."""
    denom = ndvi + ndmi
    if abs(denom) < 1e-6:
        return None
    return (ndvi - ndmi) / denom


def classify_drought(
    ndvi: float | None, ndmi: float | None
) -> DroughtPixelClass | None:
    """Pixel / snapshot NDDI class (heatmap). Not season-gated.

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


def _cloud_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if _finite(f) else None


def _scl_class_code(value: Any) -> int | None:
    """Rounded SCL class, or None if missing / non-numeric."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not _finite(f):
        return None
    return int(round(f))


def is_scl_valid_class(value: Any) -> bool:
    """True for Sen2Cor classes 1-11 (excludes nodata=0 and out-of-range)."""
    code = _scl_class_code(value)
    if code is None:
        return False
    return SCL_VALID_MIN <= code <= SCL_VALID_MAX


def is_scl_cloudy_class(value: Any) -> bool:
    """True when an SCL class is cloud, shadow, or cirrus (not vegetation/soil)."""
    code = _scl_class_code(value)
    if code is None:
        return False
    return code in SCL_CLOUD_CLASSES


def parcel_cloud_from_counts(
    cloudy_pixels: int,
    valid_pixels: int,
) -> float | None:
    """In-polygon cloud %. ``valid_pixels`` is the field-mask count, not window size."""
    if valid_pixels <= 0:
        return None
    cloudy = max(0, int(cloudy_pixels))
    valid = int(valid_pixels)
    return max(0.0, min(100.0, 100.0 * cloudy / valid))


def parcel_cloud_from_scl_values(values: Any) -> float | None:
    """Cloud % from SCL class samples already clipped to the polygon.

    ``None`` when SCL is missing, the mask is empty, or every sample is
    nodata / out of 1-11. Out-of-range values are not treated as clear.
    """
    if values is None:
        return None
    try:
        seq = list(values)
    except TypeError:
        return None
    if not seq:
        return None
    cloudy = 0
    valid = 0
    for raw in seq:
        code = _scl_class_code(raw)
        if code is None:
            continue
        if code < SCL_VALID_MIN or code > SCL_VALID_MAX:
            continue
        valid += 1
        if code in SCL_CLOUD_CLASSES:
            cloudy += 1
    return parcel_cloud_from_counts(cloudy, valid)


def parcel_cloud_from_lonlat_pixels(pixels: Any) -> float | None:
    """Fraction of lonlat pixels with clear==0. None if no pixels or no clear flag."""
    if not isinstance(pixels, list) or not pixels:
        return None
    n = 0
    cloudy = 0
    saw_flag = False
    for p in pixels:
        if not isinstance(p, dict):
            continue
        n += 1
        if "clear" in p:
            saw_flag = True
            if p.get("clear") == 0:
                cloudy += 1
    if n <= 0 or not saw_flag:
        return None
    return parcel_cloud_from_counts(cloudy, n)


def lonlat_clear_fraction(pixels: Any) -> float | None:
    """clear==1 share among lonlat pixels that carry a clear flag."""
    if not isinstance(pixels, list) or not pixels:
        return None
    n = 0
    clear = 0
    for p in pixels:
        if not isinstance(p, dict) or "clear" not in p:
            continue
        n += 1
        if p.get("clear") == 1:
            clear += 1
    if n <= 0:
        return None
    return clear / n


def parcel_cloud_is_untrusted_clear(
    parcel_cloud_cover_pct: float | None,
    cloud_cover: float | None,
    *,
    source: str | None = None,
    scene_id: str | None = None,
) -> bool:
    """True when a stored ~0% parcel cloud is not real in-polygon clear.

    Two artifacts write 0% that is not cloud:
    - SCL/lonlat counted nodata or defaulted ``clear=1``, while STAC
      ``eo:cloud_cover`` is nearly overcast (80%+).
    - Good decloud rows forced ``parcel_cloud_cover_pct=0`` so drought SQL
      would treat them as clear. Display must use STAC / the raw parcel.
    """
    parcel = _cloud_float(parcel_cloud_cover_pct)
    stac = _cloud_float(cloud_cover)
    if parcel is None or parcel > SUSPICIOUS_CLEAR_PARCEL_MAX:
        return False
    if is_decloud_product(source, scene_id) and stac is not None:
        return True
    if stac is None:
        return False
    return stac >= SUSPICIOUS_STAC_OVERCAST_MIN


def parcel_cloud_is_legacy_window_fill(
    parcel_cloud_cover_pct: float | None,
    cloud_cover: float | None,
    *,
    parcel_cloud_source: str | None = None,
    clear_frac: float | None = None,
) -> bool:
    """True when stored parcel cloud looks like padded-window quality, not SCL.

    Old writes used ``(1 - zonal_quality_score) * 100`` over the full padded
    array, so small fields sat near ~82.5% on every date. Trusted SCL / lonlat
    sources are never treated as this artifact.
    """
    src = (parcel_cloud_source or "").strip().lower()
    if src in PARCEL_CLOUD_SOURCES_TRUSTED:
        return False
    parcel = _cloud_float(parcel_cloud_cover_pct)
    stac = _cloud_float(cloud_cover)
    if (
        clear_frac is not None
        and _finite(clear_frac)
        and clear_frac >= CLEAR_PIXEL_FRACTION_TRUST
        and parcel is not None
        and parcel > SUSPICIOUS_PARCEL_VS_CLEAR
    ):
        return True
    if parcel is None or stac is None:
        return False
    return (
        parcel >= LEGACY_PARCEL_CLOUD_MIN
        and stac <= LEGACY_STAC_CLOUD_MAX
        and (parcel - stac) >= LEGACY_PARCEL_STAC_GAP
    )


def effective_cloud_pct(
    parcel_cloud_cover_pct: float | None,
    cloud_cover: float | None,
    *,
    parcel_cloud_source: str | None = None,
    clear_frac: float | None = None,
    source: str | None = None,
    scene_id: str | None = None,
) -> float | None:
    """Cloud % for tooltips / official filters: real parcel, else STAC.

    Invented zeros and legacy window-fill parcel values are treated as missing.
    When in-polygon parcel is far below Element84 ``eo:cloud_cover``, prefer
    STAC so UI/drought match the catalog the user queries (not a false 0%).
    """
    parcel = _cloud_float(parcel_cloud_cover_pct)
    stac = _cloud_float(cloud_cover)
    if parcel_cloud_is_untrusted_clear(parcel, stac, source=source, scene_id=scene_id):
        return stac
    if parcel_cloud_is_legacy_window_fill(
        parcel,
        stac,
        parcel_cloud_source=parcel_cloud_source,
        clear_frac=clear_frac,
    ):
        return stac
    if (
        parcel is not None
        and stac is not None
        and stac >= SUSPICIOUS_STAC_MIN
        and (stac - parcel) >= SUSPICIOUS_STAC_OVER_PARCEL_GAP
    ):
        return stac
    if parcel is not None:
        return parcel
    return stac


def scene_cloud_fields(
    stac_cloud: float | None,
    parcel_cloud: float | None,
    *,
    cloud_max_pct: float = CLOUD_MAX_PCT,
) -> tuple[float | None, bool, float | None]:
    """Return (cloud_cover, cloud_cover_over_30, parcel_cloud_cover_pct).

    ``parcel_cloud`` is in-polygon cloud % (SCL or lonlat clear flags). It is
    not zonal ``quality_score`` and must not be ``(1 - window_fill) * 100``.
    STAC ``eo:cloud_cover`` is always stored in ``cloud_cover``.
    ``cloud_cover_over_30`` uses the parcel metric when present, else STAC.
    """
    stac = _cloud_float(stac_cloud)
    parcel = _cloud_float(parcel_cloud)
    if parcel_cloud_is_untrusted_clear(parcel, stac):
        parcel = None
    if parcel is not None:
        parcel = max(0.0, min(100.0, parcel))
    if (
        parcel is not None
        and stac is not None
        and stac >= SUSPICIOUS_STAC_MIN
        and (stac - parcel) >= SUSPICIOUS_STAC_OVER_PARCEL_GAP
    ):
        over = stac > cloud_max_pct
    elif parcel is not None:
        over = parcel > cloud_max_pct
    else:
        over = bool(stac is not None and stac > cloud_max_pct)
    return stac, over, parcel


def classify_flood(vv_db: float | None, vh_db: float | None) -> FloodClass | None:
    """Pixel / snapshot S1 class without an orbit baseline.

    Without a per-orbit drop, do not call a date a flood. Very low VV
    plus the VH helper is ``watch``. VV-VH difference alone never flags.
    """
    if vv_db is None or not _finite(vv_db):
        return None
    vh = float(vh_db) if vh_db is not None and _finite(vh_db) else None
    vv = float(vv_db)
    helper = vh is not None and vh <= FLOOD_VH_MAX
    watch_vh = vh is not None and vh <= WATCH_VH_MAX
    if vv <= WATCH_VV_MAX and (helper or watch_vh or vv <= FLOOD_VV_MAX):
        return "watch"
    return "dry"


def is_open_water_flood(cls: FloodClass | str | None) -> bool:
    """Severe + moderate ≈ confirmed flood (backward-compat ``flood`` count)."""
    return cls in ("flood_severe", "flood_moderate")


def is_flood_alert(cls: FloodClass | str | None) -> bool:
    """Confirmed flood only. Watch is near-threshold, not a flood flag."""
    return cls in ("flood_severe", "flood_moderate")


def overview_flood_bucket(cls: FloodClass | str | None) -> str | None:
    """Map scene class onto overview count keys (watch -> flood_mild)."""
    if cls is None:
        return None
    if cls == "watch":
        return "flood_mild"
    if cls in ("flood_severe", "flood_moderate", "flood_mild", "dry"):
        return str(cls)
    return None


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
) -> tuple[DroughtPixelClass | None, float | None, int]:
    """Majority drought class over pixels; also severe_pixel_share and n.

    Returns (class, severe_pixel_share, n_classified). Share is None if n=0.
    """
    pairs = _pixel_ndvi_ndmi_pairs(pixel_data)
    if not pairs:
        return None, None, 0
    classes: list[DroughtPixelClass] = []
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
    parcel_cloud_source: str | None = None,
    clear_frac: float | None = None,
    source: str | None = None,
    scene_id: str | None = None,
) -> bool:
    """Optical clear filter: real parcel cloud, else STAC. Legacy fill is ignored."""
    cloud = effective_cloud_pct(
        parcel_cloud_cover_pct,
        cloud_cover,
        parcel_cloud_source=parcel_cloud_source,
        clear_frac=clear_frac,
        source=source,
        scene_id=scene_id,
    )
    if cloud is not None:
        return float(cloud) <= cloud_max_pct
    if cloud_cover_over_30 is True:
        return False
    return True


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
    parcel_cloud_source: str | None = None,
    clear_frac: float | None = None,
) -> bool:
    """Whether a scene may feed drought / timeseries / land metrics.

    Raw S2 still uses the cloud > 30% skip (real parcel, else STAC).
    Decloud rows are official only when quality is ``good``.
    ``fair`` / ``bad`` are stored for audit and never enter drought.
    """
    if is_decloud_product(source, scene_id):
        return (decloud_quality or "").strip().lower() == DECLOUD_QUALITY_GOOD
    return is_clear_scene(
        parcel_cloud_cover_pct,
        cloud_cover,
        cloud_cover_over_30,
        cloud_max_pct=cloud_max_pct,
        parcel_cloud_source=parcel_cloud_source,
        clear_frac=clear_frac,
        source=source,
        scene_id=scene_id,
    )


def official_s2_sql(
    alias: str = "s",
    *,
    cloud_param: str = "cloud_max",
) -> str:
    """SQL predicate: clear raw S2, or good-quality decloud only.

    Raw cloud uses in-polygon parcel % when ``parcel_cloud_source`` is scl /
    lonlat_clear. Parcel ~0% while STAC is nearly overcast is treated as
    missing (nodata counted as clear / invented zeros). Legacy window-fill
    parcel (~82% on small padded fields) falls back to STAC ``cloud_cover``.
    ``cloud_cover_over_30`` is not used alone because old writes set it from
    that fill ratio.
    """
    a = f"{alias}." if alias else ""
    trusted = ",".join(f"'{s}'" for s in sorted(PARCEL_CLOUD_SOURCES_TRUSTED))
    effective = f"""(
        CASE
          WHEN {a}parcel_cloud_cover_pct IS NOT NULL
               AND {a}cloud_cover IS NOT NULL
               AND {a}parcel_cloud_cover_pct <= {SUSPICIOUS_CLEAR_PARCEL_MAX}
               AND {a}cloud_cover >= {SUSPICIOUS_STAC_OVERCAST_MIN}
          THEN {a}cloud_cover
          WHEN {a}pixel_data->>'parcel_cloud_source' IN ({trusted})
          THEN coalesce({a}parcel_cloud_cover_pct, {a}cloud_cover)
          WHEN {a}parcel_cloud_cover_pct IS NOT NULL
               AND {a}cloud_cover IS NOT NULL
               AND {a}parcel_cloud_cover_pct >= {LEGACY_PARCEL_CLOUD_MIN}
               AND {a}cloud_cover <= {LEGACY_STAC_CLOUD_MAX}
               AND ({a}parcel_cloud_cover_pct - {a}cloud_cover)
                   >= {LEGACY_PARCEL_STAC_GAP}
          THEN {a}cloud_cover
          ELSE coalesce({a}parcel_cloud_cover_pct, {a}cloud_cover)
        END
      )"""
    return f"""(
      (
        COALESCE({a}pixel_data->>'source', '') <> '{DECLOUD_SOURCE}'
        AND COALESCE({a}scene_id, '') NOT LIKE '%{DECLOUD_SCENE_ID_SUFFIX}'
        AND NOT ({effective} > :{cloud_param})
      )
      OR (
        (
          {a}pixel_data->>'source' = '{DECLOUD_SOURCE}'
          OR {a}scene_id LIKE '%{DECLOUD_SCENE_ID_SUFFIX}'
        )
        AND {a}pixel_data->>'decloud_quality' = '{DECLOUD_QUALITY_GOOD}'
      )
    )"""


def month_from_date(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        return int(str(date_str)[5:7])
    except (TypeError, ValueError, IndexError):
        return None


def is_drought_season(
    date_str: str | None,
    season_months: tuple[int, ...] | list[int] = PHENOLOGY_MONTHS,
) -> bool:
    month = month_from_date(date_str)
    if month is None:
        return False
    return month in set(int(m) for m in season_months)


def cloud_pct(
    parcel_cloud_cover_pct: float | None,
    cloud_cover: float | None,
    *,
    parcel_cloud_source: str | None = None,
    clear_frac: float | None = None,
    source: str | None = None,
    scene_id: str | None = None,
) -> float | None:
    return effective_cloud_pct(
        parcel_cloud_cover_pct,
        cloud_cover,
        parcel_cloud_source=parcel_cloud_source,
        clear_frac=clear_frac,
        source=source,
        scene_id=scene_id,
    )


def _scene_cloud_pct(scene: dict[str, Any]) -> float | None:
    return effective_cloud_pct(
        scene.get("parcel_cloud_cover_pct"),
        scene.get("cloud_cover"),
        parcel_cloud_source=scene.get("parcel_cloud_source"),
        clear_frac=_num(scene.get("clear_frac")),
        source=scene.get("source"),
        scene_id=scene.get("scene_id"),
    )


def _scene_iso_date(scene: dict[str, Any]) -> str:
    raw = scene.get("date")
    if raw is None:
        return ""
    if isinstance(raw, date_cls):
        return raw.isoformat()
    return str(raw)[:10]


def _parse_iso_date(date_str: str | None) -> date_cls | None:
    if not date_str:
        return None
    try:
        return date_cls.fromisoformat(str(date_str)[:10])
    except (TypeError, ValueError):
        return None


def _scene_ndvi(scene: dict[str, Any]) -> float | None:
    return _num(scene.get("ndvi", scene.get("ndvi_avg")))


def _scene_ndmi(scene: dict[str, Any]) -> float | None:
    return _num(scene.get("ndmi", scene.get("ndmi_avg")))


def _cloud_sort_key(scene: dict[str, Any]) -> tuple[float, str]:
    pct = _scene_cloud_pct(scene)
    return (pct if pct is not None else 999.0, str(scene.get("scene_id") or ""))


def _is_raw_scene(scene: dict[str, Any]) -> bool:
    return not is_decloud_product(scene.get("source"), scene.get("scene_id"))


def _official_kwargs(scene: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": scene.get("source"),
        "scene_id": scene.get("scene_id"),
        "decloud_quality": scene.get("decloud_quality"),
        "parcel_cloud_cover_pct": scene.get("parcel_cloud_cover_pct"),
        "cloud_cover": scene.get("cloud_cover"),
        "cloud_cover_over_30": scene.get("cloud_cover_over_30"),
        "parcel_cloud_source": scene.get("parcel_cloud_source"),
        "clear_frac": _num(scene.get("clear_frac")),
    }


def nearby_clear_index_medians(
    neighbors: list[dict[str, Any]] | None,
    target_date: str,
    *,
    window_days: int = NEARBY_CLEAR_DAYS,
) -> tuple[float | None, float | None]:
    """Median NDVI/NDMI of nearby clear raw dates (±window_days, else same month).

    Neighbors must be other dates. Used so raw vs good decloud can pick the
    product closer to recent clear canopy, not a cloudy NDVI dip.
    """
    target = _parse_iso_date(target_date)
    if target is None or not neighbors:
        return None, None
    windowed: list[tuple[float, float | None]] = []
    same_month: list[tuple[float, float | None]] = []
    for s in neighbors:
        if not _is_raw_scene(s):
            continue
        ds = _scene_iso_date(s)
        d = _parse_iso_date(ds)
        if d is None or d == target:
            continue
        if not is_official_optical_product(**_official_kwargs(s)):
            continue
        ndvi = _scene_ndvi(s)
        if ndvi is None:
            continue
        rec = (ndvi, _scene_ndmi(s))
        if abs((d - target).days) <= window_days:
            windowed.append(rec)
        if d.month == target.month:
            same_month.append(rec)
    pool = windowed or same_month
    if not pool:
        return None, None
    ndvi_med = _median([p[0] for p in pool])
    ndmi_vals = [p[1] for p in pool if p[1] is not None]
    ndmi_med = _median(ndmi_vals) if ndmi_vals else None
    return ndvi_med, ndmi_med


def _needs_closer_to_truth(raw: dict[str, Any]) -> bool:
    """Borderline parcel, or STAC-clear while in-polygon parcel is cloudy."""
    parcel = _cloud_float(raw.get("parcel_cloud_cover_pct"))
    stac = _cloud_float(raw.get("cloud_cover"))
    if parcel_cloud_is_legacy_window_fill(
        parcel,
        stac,
        parcel_cloud_source=raw.get("parcel_cloud_source"),
        clear_frac=_num(raw.get("clear_frac")),
    ):
        return False
    if parcel is None:
        return False
    if BORDERLINE_PARCEL_MIN < parcel <= BORDERLINE_PARCEL_MAX:
        return True
    if parcel > CLOUD_MAX_PCT and stac is not None and stac <= CLOUD_MAX_PCT:
        return True
    return False


def _truth_sort_key(
    scene: dict[str, Any],
    ndvi_med: float | None,
    ndmi_med: float | None,
) -> tuple[float, float, int, str]:
    ndvi = _scene_ndvi(scene)
    ndmi = _scene_ndmi(scene)
    d_ndvi = (
        abs(ndvi - ndvi_med) if ndvi is not None and ndvi_med is not None else 999.0
    )
    d_ndmi = (
        abs(ndmi - ndmi_med) if ndmi is not None and ndmi_med is not None else 999.0
    )
    # Tie-break: prefer raw over decloud, then stable scene_id.
    decloud_rank = 0 if _is_raw_scene(scene) else 1
    return (d_ndvi, d_ndmi, decloud_rank, str(scene.get("scene_id") or ""))


def _physiology_sort_key(
    scene: dict[str, Any],
    ndvi_med: float | None,
    ndmi_med: float | None,
    target_date: str,
) -> tuple[int, float, float, int, str]:
    """Lower is better for NDVI/growth pick vs nearby clear phenology."""
    ndvi = _scene_ndvi(scene)
    ndmi = _scene_ndmi(scene)
    d_ndvi = (
        abs(ndvi - ndvi_med) if ndvi is not None and ndvi_med is not None else 999.0
    )
    d_ndmi = (
        abs(ndmi - ndmi_med) if ndmi is not None and ndmi_med is not None else 999.0
    )
    absurd = 0
    if ndvi is not None and ndvi_med is not None:
        if abs(ndvi - ndvi_med) >= PHYSIOLOGY_NDVI_ABSURD_GAP:
            absurd = 1
        month = month_from_date(target_date)
        if (
            month is not None
            and month in PHENOLOGY_MONTHS
            and ndvi_med >= PHYSIOLOGY_GREEN_NEIGHBOR_NDVI
            and ndvi < PHYSIOLOGY_GROWING_NDVI_FLOOR
        ):
            absurd = 1
    decloud_rank = 0 if _is_raw_scene(scene) else 1
    return (absurd, d_ndvi, d_ndmi, decloud_rank, str(scene.get("scene_id") or ""))


def _raw_fits_phenology_better(
    raw: dict[str, Any],
    decloud: dict[str, Any],
) -> bool:
    """Without neighbors: keep raw if it looks like canopy and decloud does not."""
    target = _scene_iso_date(raw) or _scene_iso_date(decloud)
    month = month_from_date(target)
    raw_ndvi = _scene_ndvi(raw)
    dec_ndvi = _scene_ndvi(decloud)
    if raw_ndvi is None or dec_ndvi is None:
        return False
    in_season = month is not None and month in PHENOLOGY_MONTHS
    if in_season:
        raw_ok = PHYSIOLOGY_GROWING_NDVI_FLOOR <= raw_ndvi <= 0.95
        dec_bad = dec_ndvi < PHYSIOLOGY_GROWING_NDVI_FLOOR
        return raw_ok and dec_bad
    return False


def pick_official_optical(
    scenes: list[dict[str, Any]],
    *,
    neighbors: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Pick one S2 product for a date.

    Rules (fair/bad decloud never chosen):
    1. Truly clear raw (real parcel cloud <= 30%, or STAC if parcel missing /
       legacy fill): prefer raw.
    2. Cloudy raw (real parcel > 30%) and good decloud exists: prefer good
       decloud, unless step 3 applies.
    3. Both exist and raw is borderline (parcel 20-40%) *or* STAC is clear
       while parcel is cloudy: pick the product whose NDVI (then NDMI) is
       closer to the median of nearby clear raw dates (±45d, else same month).
       Tie-break: raw, then scene_id.
    """
    if not scenes:
        return None
    raw_scenes = [s for s in scenes if _is_raw_scene(s)]
    good_decloud = [
        s
        for s in scenes
        if not _is_raw_scene(s)
        and (
            str(s.get("decloud_quality") or "").strip().lower() == DECLOUD_QUALITY_GOOD
        )
    ]
    best_raw = sorted(raw_scenes, key=_cloud_sort_key)[0] if raw_scenes else None
    best_decloud = (
        sorted(good_decloud, key=_cloud_sort_key)[0] if good_decloud else None
    )
    raw_clear = bool(
        best_raw is not None
        and is_official_optical_product(**_official_kwargs(best_raw))
    )

    if best_raw is not None and best_decloud is None:
        return best_raw if raw_clear else None
    if best_raw is None:
        return best_decloud

    compare = _needs_closer_to_truth(best_raw)
    if compare:
        ndvi_med, ndmi_med = nearby_clear_index_medians(
            neighbors, _scene_iso_date(best_raw)
        )
        if ndvi_med is not None:
            return sorted(
                [best_raw, best_decloud],
                key=lambda s: _truth_sort_key(s, ndvi_med, ndmi_med),
            )[0]
    if raw_clear:
        return best_raw
    return best_decloud


def pick_optical_for_ndvi(
    scenes: list[dict[str, Any]],
    *,
    neighbors: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Growth-series pick: physiology-aware raw vs good decloud.

    Never uses fair/bad decloud (those stay stored and marked unreliable).
    When both raw and good decloud exist, prefer the product whose NDVI
    (then NDMI) is closer to nearby clear-raw seasonal baseline, and that
    is not absurd versus the crop calendar (Jun-Sep green canopy). Tie-break
    raw. Drought / land metrics still use ``pick_official_optical``.
    """
    if not scenes:
        return None
    raw_scenes = [s for s in scenes if _is_raw_scene(s)]
    good_decloud = [
        s
        for s in scenes
        if not _is_raw_scene(s)
        and (
            str(s.get("decloud_quality") or "").strip().lower() == DECLOUD_QUALITY_GOOD
        )
    ]
    best_raw = sorted(raw_scenes, key=_cloud_sort_key)[0] if raw_scenes else None
    best_decloud = (
        sorted(good_decloud, key=_cloud_sort_key)[0] if good_decloud else None
    )
    if best_raw is not None and best_decloud is not None:
        target = _scene_iso_date(best_raw) or _scene_iso_date(best_decloud)
        ndvi_med, ndmi_med = nearby_clear_index_medians(neighbors, target)
        if ndvi_med is not None:
            return sorted(
                [best_raw, best_decloud],
                key=lambda s: _physiology_sort_key(s, ndvi_med, ndmi_med, target),
            )[0]
        if _raw_fits_phenology_better(best_raw, best_decloud):
            return best_raw
    picked = pick_official_optical(scenes, neighbors=neighbors)
    if picked is not None:
        return picked
    if best_raw is not None:
        return best_raw
    return None


def optical_tooltip_fields(scene: dict[str, Any] | None) -> dict[str, Any]:
    """Cloud % + decloud quality description inputs for an NDVI point."""
    if not scene:
        return {
            "cloud_cover": None,
            "decloud_quality": None,
            "decloud_reasons": [],
            "product_source": None,
            "scene_id": None,
            "is_decloud": False,
            "is_official": False,
            "may_be_unreliable": False,
        }
    reasons = scene.get("decloud_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        reasons = []
    source = scene.get("source")
    scene_id = scene.get("scene_id")
    quality = scene.get("decloud_quality")
    is_decloud = is_decloud_product(source, scene_id)
    q = (quality or "").strip().lower()
    return {
        "cloud_cover": _scene_cloud_pct(scene),
        "decloud_quality": quality,
        "decloud_reasons": [str(r) for r in reasons if r],
        "product_source": source,
        "scene_id": scene_id,
        "is_decloud": is_decloud,
        "is_official": is_official_optical_product(**_official_kwargs(scene)),
        "may_be_unreliable": is_decloud and q != DECLOUD_QUALITY_GOOD,
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _percentile_rank(value: float, values: list[float]) -> float | None:
    if not values:
        return None
    n = len(values)
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (below + 0.5 * equal) / n * 100.0


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    x = max(0.0, min(100.0, p)) / 100.0 * (len(s) - 1)
    lo = int(math.floor(x))
    hi = int(math.ceil(x))
    if lo == hi:
        return s[lo]
    t = x - lo
    return s[lo] * (1.0 - t) + s[hi] * t


def build_month_drought_baselines(
    observations: list[OpticalObs],
    *,
    season_months: tuple[int, ...] | list[int] = PHENOLOGY_MONTHS,
) -> dict[int, dict[str, Any]]:
    """Same-month medians / NDDI lists from official in-season scenes only."""
    buckets: dict[int, dict[str, list[float]]] = defaultdict(
        lambda: {"ndvi": [], "ndmi": [], "nddi": []}
    )
    for obs in observations:
        if not obs.get("official"):
            continue
        date_str = str(obs.get("date") or "")
        if not is_drought_season(date_str, season_months):
            continue
        month = month_from_date(date_str)
        if month is None:
            continue
        ndvi = _num(obs.get("ndvi"))
        ndmi = _num(obs.get("ndmi"))
        if ndvi is None or ndmi is None:
            continue
        buckets[month]["ndvi"].append(ndvi)
        buckets[month]["ndmi"].append(ndmi)
        nddi = compute_nddi(ndvi, ndmi)
        if nddi is not None:
            buckets[month]["nddi"].append(nddi)
    out: dict[int, dict[str, Any]] = {}
    for month, b in buckets.items():
        out[month] = {
            "ndvi_median": _median(b["ndvi"]),
            "ndmi_median": _median(b["ndmi"]),
            "nddi_values": list(b["nddi"]),
            "n": len(b["ndvi"]),
        }
    return out


def _drought_severity(nddi: float | None, ndvi_drop: float | None) -> DroughtClass:
    if (nddi is not None and nddi >= NDDI_SEVERE_MIN) or (
        ndvi_drop is not None and ndvi_drop >= NDVI_DROP_SEVERE
    ):
        return "severe"
    if (nddi is not None and nddi >= NDDI_MODERATE_MIN) or (
        ndvi_drop is not None and ndvi_drop >= NDVI_DROP_MODERATE
    ):
        return "moderate"
    return "mild"


def classify_drought_scene(
    obs: OpticalObs,
    month_stats: dict[str, Any] | None,
    *,
    season_months: tuple[int, ...] | list[int] = PHENOLOGY_MONTHS,
) -> DroughtClass:
    """Growing-season multi-indicator drought class for one official scene."""
    date_str = str(obs.get("date") or "")
    if not is_drought_season(date_str, season_months):
        return "out_of_season"
    if not obs.get("official"):
        return "unreliable"
    ndvi = _num(obs.get("ndvi"))
    ndmi = _num(obs.get("ndmi"))
    if ndvi is None or ndmi is None:
        return "unreliable"
    nddi = compute_nddi(ndvi, ndmi)
    stats = month_stats or {}
    n = int(stats.get("n") or 0)
    ndvi_med = _num(stats.get("ndvi_median"))
    ndmi_med = _num(stats.get("ndmi_median"))
    nddi_vals = stats.get("nddi_values") or []

    nddi_abs = nddi is not None and nddi >= NDDI_MILD_MIN
    nddi_anom = False
    if n >= MIN_MONTH_SAMPLES and nddi is not None and nddi_vals:
        rank = _percentile_rank(nddi, list(nddi_vals))
        nddi_anom = rank is not None and rank >= NDDI_PCTL_DRY

    ndmi_dry = ndmi < NDMI_DRY_ABS
    if ndmi_med is not None:
        ndmi_dry = ndmi_dry or ndmi <= ndmi_med - NDMI_DROP_VS_MEDIAN

    ndvi_drop: float | None = None
    ndvi_dropped = False
    if ndvi_med is not None:
        ndvi_drop = ndvi_med - ndvi
        ndvi_dropped = ndvi <= ndvi_med - NDVI_DROP_VS_MEDIAN

    if (nddi_abs or nddi_anom) and (ndmi_dry or ndvi_dropped):
        return _drought_severity(nddi, ndvi_drop)
    return "normal"


def classify_drought_series(
    observations: list[OpticalObs],
    *,
    season_months: tuple[int, ...] | list[int] = PHENOLOGY_MONTHS,
) -> list[tuple[str, DroughtClass]]:
    """Classify each observation; baselines from official in-season only."""
    baselines = build_month_drought_baselines(observations, season_months=season_months)
    out: list[tuple[str, DroughtClass]] = []
    for obs in observations:
        date_str = str(obs.get("date") or "")
        month = month_from_date(date_str)
        stats = baselines.get(month) if month is not None else None
        out.append(
            (date_str, classify_drought_scene(obs, stats, season_months=season_months))
        )
    return out


def parse_s1_relative_orbit(
    scene_id: str | None,
    relative_orbit: int | None = None,
) -> int | None:
    """Relative orbit from STAC field or Sentinel-1 GRD product id."""
    if relative_orbit is not None:
        try:
            r = int(relative_orbit)
        except (TypeError, ValueError):
            r = None
        else:
            if 1 <= r <= 175:
                return r
    if not scene_id:
        return None
    m = _S1_ID_RE.match(str(scene_id).strip())
    if not m:
        return None
    mission = m.group(1).upper()
    try:
        abs_orbit = int(m.group(2))
    except (TypeError, ValueError):
        return None
    offset = _S1_ORBIT_OFFSET.get(mission, 73)
    return ((abs_orbit - offset) % 175) + 1


def orbit_group_key(obs: SarObs) -> str:
    rel = parse_s1_relative_orbit(obs.get("scene_id"), obs.get("relative_orbit"))
    if rel is None:
        return "unknown"
    return f"ron{rel}"


def classify_flood_scene(
    vv: float | None,
    vh: float | None,
    baseline_vv: float | None,
    orbit_diff_p40: float | None,
) -> FloodClass | None:
    """Scene flood/watch using per-orbit VV baseline. VV-VH alone is never flood."""
    if vv is None or not _finite(vv):
        return None
    vh_f = float(vh) if vh is not None and _finite(vh) else None
    drop = None
    if baseline_vv is not None and _finite(baseline_vv):
        drop = float(vv) - float(baseline_vv)
    vv_vh = None
    if vh_f is not None:
        vv_vh = float(vv) - vh_f

    helper = False
    if vh_f is not None and vh_f <= FLOOD_VH_MAX:
        helper = True
    if (
        vv_vh is not None
        and orbit_diff_p40 is not None
        and _finite(orbit_diff_p40)
        and vv_vh <= float(orbit_diff_p40)
    ):
        helper = True

    low_vv = float(vv) <= FLOOD_VV_MAX
    dropped = drop is not None and drop <= FLOOD_VV_DROP
    if low_vv and dropped and helper:
        if float(vv) <= FLOOD_VV_SEVERE:
            return "flood_severe"
        return "flood_moderate"

    watch_vv = float(vv) <= WATCH_VV_MAX
    watch_drop = drop is not None and drop <= WATCH_VV_DROP
    watch_vh = vh_f is not None and vh_f <= WATCH_VH_MAX
    if watch_vv and (watch_drop or watch_vh or helper or low_vv):
        return "watch"
    return "dry"


def classify_flood_series(
    observations: list[SarObs],
) -> list[tuple[str, FloodClass | None]]:
    """Per-orbit median VV baseline + p40 of VV-VH; classify each scene."""
    groups: dict[str, list[SarObs]] = defaultdict(list)
    for obs in observations:
        if _num(obs.get("vv")) is None:
            continue
        groups[orbit_group_key(obs)].append(obs)

    baselines: dict[str, tuple[float | None, float | None]] = {}
    for key, rows in groups.items():
        vvs = [_num(r.get("vv")) for r in rows]
        vvs_f = [v for v in vvs if v is not None]
        diffs: list[float] = []
        for r in rows:
            vv = _num(r.get("vv"))
            vh = _num(r.get("vh"))
            if vv is not None and vh is not None:
                diffs.append(vv - vh)
        if len(vvs_f) >= MIN_ORBIT_SAMPLES:
            baselines[key] = (_median(vvs_f), _percentile(diffs, VV_VH_DIFF_PCTL))
        else:
            # Too few in this orbit: fall back to all-scene median, still not VV-VH-only.
            all_vv = [_num(r.get("vv")) for r in observations]
            all_f = [v for v in all_vv if v is not None]
            all_diff: list[float] = []
            for r in observations:
                vv = _num(r.get("vv"))
                vh = _num(r.get("vh"))
                if vv is not None and vh is not None:
                    all_diff.append(vv - vh)
            baselines[key] = (
                _median(all_f) if len(all_f) >= MIN_ORBIT_SAMPLES else _median(vvs_f),
                _percentile(all_diff, VV_VH_DIFF_PCTL)
                if len(all_diff) >= MIN_ORBIT_SAMPLES
                else _percentile(diffs, VV_VH_DIFF_PCTL),
            )

    out: list[tuple[str, FloodClass | None]] = []
    for obs in observations:
        date_str = str(obs.get("date") or "")
        vv = _num(obs.get("vv"))
        vh = _num(obs.get("vh"))
        base, p40 = baselines.get(orbit_group_key(obs), (None, None))
        out.append((date_str, classify_flood_scene(vv, vh, base, p40)))
    return out


def is_drought_day_class(cls: DroughtClass | str | None) -> bool:
    return cls in ("mild", "moderate", "severe")


def _finite(v: float) -> bool:
    return v == v and v not in (float("inf"), float("-inf"))
