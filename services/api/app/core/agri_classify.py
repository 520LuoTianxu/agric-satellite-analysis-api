"""Drought / flood classifiers mirroring apps/web/src/lib/agri-classify.ts.

Keep this file in sync with services/ingest/app/core/agri_classify.py.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
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
) -> float | None:
    if parcel_cloud_cover_pct is not None and _finite(float(parcel_cloud_cover_pct)):
        return float(parcel_cloud_cover_pct)
    if cloud_cover is not None and _finite(float(cloud_cover)):
        return float(cloud_cover)
    return None


def pick_official_optical(scenes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick one S2 product for a date: clear raw, else good decloud.

    fair/bad decloud is never chosen. Caller should pass same-date scenes.
    """
    if not scenes:
        return None
    official: list[dict[str, Any]] = []
    for s in scenes:
        if is_official_optical_product(
            source=s.get("source"),
            scene_id=s.get("scene_id"),
            decloud_quality=s.get("decloud_quality"),
            parcel_cloud_cover_pct=s.get("parcel_cloud_cover_pct"),
            cloud_cover=s.get("cloud_cover"),
            cloud_cover_over_30=s.get("cloud_cover_over_30"),
        ):
            official.append(s)
    raw_off = [
        s
        for s in official
        if not is_decloud_product(s.get("source"), s.get("scene_id"))
    ]
    pool = raw_off or official
    if not pool:
        return None

    def _key(s: dict[str, Any]) -> tuple[float, str]:
        pct = cloud_pct(s.get("parcel_cloud_cover_pct"), s.get("cloud_cover"))
        return (pct if pct is not None else 999.0, str(s.get("scene_id") or ""))

    return sorted(pool, key=_key)[0]


def pick_optical_for_ndvi(scenes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Official product if any; else raw (including cloudy). Never fair/bad decloud."""
    picked = pick_official_optical(scenes)
    if picked is not None:
        return picked
    raw = [
        s for s in scenes if not is_decloud_product(s.get("source"), s.get("scene_id"))
    ]
    if not raw:
        return None

    def _key(s: dict[str, Any]) -> tuple[float, str]:
        pct = cloud_pct(s.get("parcel_cloud_cover_pct"), s.get("cloud_cover"))
        return (pct if pct is not None else 999.0, str(s.get("scene_id") or ""))

    return sorted(raw, key=_key)[0]


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
        }
    reasons = scene.get("decloud_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        reasons = []
    source = scene.get("source")
    scene_id = scene.get("scene_id")
    quality = scene.get("decloud_quality")
    return {
        "cloud_cover": cloud_pct(
            scene.get("parcel_cloud_cover_pct"), scene.get("cloud_cover")
        ),
        "decloud_quality": quality,
        "decloud_reasons": [str(r) for r in reasons if r],
        "product_source": source,
        "scene_id": scene_id,
        "is_decloud": is_decloud_product(source, scene_id),
        "is_official": is_official_optical_product(
            source=source,
            scene_id=scene_id,
            decloud_quality=quality,
            parcel_cloud_cover_pct=scene.get("parcel_cloud_cover_pct"),
            cloud_cover=scene.get("cloud_cover"),
            cloud_cover_over_30=scene.get("cloud_cover_over_30"),
        ),
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
