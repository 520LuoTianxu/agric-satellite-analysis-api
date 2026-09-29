"""Drought / flood classifiers shared by API and ingest, mirroring the Web rules.

本模块是API与ingest共用的Python分类实现；旧的``app.core.agri_classify``路径仅负责兼容导入。
"""

from __future__ import annotations

import math
import re
from bisect import insort
from collections import Counter, defaultdict
from datetime import date as date_cls, datetime, timezone
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
# agric-satellite-analysis-web/src/lib/agri-classify.ts). Citations:
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
#   VV <= -17 dB, VV - comparable calibrated baseline <= -3 dB,
#   helper (VH <= -22 dB OR VV-VH diff <= comparable-scene p40).
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
# Mirrors agric-satellite-analysis-web/src/lib/agri-heatmap.ts S2_VALUE_INDEX.
S2_GRID_NDMI_IDX = 4
S2_GRID_NDVI_IDX = 6

# 仅在STAC相对轨道缺失时按175轨道周期解析产品ID；S1D偏移已用公开STAC绝对/相对轨道样例核验。
_S1_ORBIT_OFFSET = {"S1A": 73, "S1B": 27, "S1C": 172, "S1D": 42}
_S1_ID_RE = re.compile(
    r"^(?P<mission>S1[ABCD])_IW_GRD[HM]?_1S[DS][VH]_"
    r"(?P<acquisition_datetime>\d{8}T\d{6})_\d{8}T\d{6}_"
    r"(?P<absolute_orbit>\d{6})",
    re.IGNORECASE,
)
# ESA公告指出S1C新AUX_CAL配置从2026-02-03约15:14 UTC的数据开始生效。
_S1C_CALIBRATION_CUTOFF_UTC = datetime(2026, 2, 3, 15, 14, tzinfo=timezone.utc)
S1C_CALIBRATION_EPOCH_PRE = "s1c-auxcal-pre-2026-02-03"
S1C_CALIBRATION_EPOCH_POST = "s1c-auxcal-post-2026-02-03"
S1C_CALIBRATION_EPOCH_UNKNOWN = "s1c-auxcal-transition-unknown"

# 与ingest去云预处理和Web分类规则共用同一去云产品元数据契约。
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
    stac_item_id: str | None
    relative_orbit: int | None
    platform: str | None
    processing_version: str | None
    calibration_epoch: str | None
    acquisition_datetime: str | None
    calibration_method: str | None
    calibration_scale: float | None
    radiometric_calibration: dict[str, Any] | None


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

    中文业务约束：像元热力图只表达当前影像的指数等级，不套用生长季或历史月基线；
    只有NDDI因输入缺失或分母接近零而无法计算时，才用极低NDMI标记重旱，避免单个
    NDMI值制造轻、中旱等级。
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
        # 仅把 SCL 1–11 纳入地块像元分母，避免 nodata 或无效编码被误算为晴空。
        if code < SCL_VALID_MIN or code > SCL_VALID_MAX:
            continue
        valid += 1
        if code in SCL_CLOUD_CLASSES:
            cloudy += 1
    return parcel_cloud_from_counts(cloudy, valid)


def _lonlat_clear_flag(value: Any) -> int | None:
    """将经纬度像元的晴空标记限制为有效的0/1值。"""
    try:
        flag = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(flag) or flag not in (0.0, 1.0):
        return None
    return int(flag)


def parcel_cloud_from_lonlat_pixels(pixels: Any) -> float | None:
    """Fraction of valid lonlat clear flags with clear==0.

    中文业务约束：仅统计携带有效0/1标记的地块像元；SCL缺失或nodata的像元是未知，
    不能放进分母当作晴空，否则会低估地块云量。
    """
    if not isinstance(pixels, list) or not pixels:
        return None
    n = 0
    cloudy = 0
    for p in pixels:
        if not isinstance(p, dict) or "clear" not in p:
            continue
        flag = _lonlat_clear_flag(p.get("clear"))
        if flag is None:
            continue
        n += 1
        if flag == 0:
            cloudy += 1
    if n <= 0:
        return None
    return parcel_cloud_from_counts(cloudy, n)


def lonlat_clear_fraction(pixels: Any) -> float | None:
    """clear==1 share among lonlat pixels that carry a valid clear flag.

    中文业务约束：只用有效的0/1标记计算晴空比例；缺失标记与非法编码表示未知，
    不能视作晴空或云像元。
    """
    if not isinstance(pixels, list) or not pixels:
        return None
    n = 0
    clear = 0
    for p in pixels:
        if not isinstance(p, dict) or "clear" not in p:
            continue
        flag = _lonlat_clear_flag(p.get("clear"))
        if flag is None:
            continue
        n += 1
        if flag == 1:
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

    中文业务约束：当地块云量接近0但STAC显示大范围多云，或记录是去云派生产品时，
    不能把这个0%当作原始观测的地块内晴空证据。
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

    中文业务约束：旧版窗口填充质量分不是地块内云量；识别到这种历史值时应回退STAC，
    但来源明确为SCL或经纬度晴空标记的新数据必须保留。
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

    中文业务约束：按“异常/旧值回退STAC、可信地块云量优先、缺失时用STAC”的顺序统一
    地图提示和官方景筛选，避免同一产品在不同页面采用不同云量。
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

    中文业务约束：分别保存STAC云量与地块内云量，阈值判断才按可信度选用；
    不能把窗口填充质量或未知像元折算值伪装成地块云量。
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

    中文业务约束：没有同轨历史基线时，单景只能给出关注提示，不能确认洪涝；
    VV与VH差值本身不作为判定条件，避免地表类型差异造成误报。
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
    """Extract (ndvi, ndmi) pairs from lonlat_v1 or legacy grid pixel_data.

    中文业务约束：兼容新经纬度稀疏点与旧版网格数组；有晴空标记时排除云像元，
    并剔除任一指数缺失的样本，确保后续多数判级只使用可比较的有效像元。
    """
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
            # 有明确云标记时只排除云像元；缺失clear标记的历史记录仍由有效指数决定是否可用。
            if "clear" in p and p.get("clear") == 0:
                continue
            ndvi = _num(p.get("NDVI", p.get("ndvi")))
            ndmi = _num(p.get("NDMI", p.get("ndmi")))
            if ndvi is None or ndmi is None:
                continue
            out.append((ndvi, ndmi))
        return out

    # 旧版网格数组按固定字段位置读取，保持历史已入库像元仍可参与同一判级口径。
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

    中文业务约束：先逐像元过滤无效指数，再以有效像元多数等级代表当前景，
    重旱像元占比只在有效分类样本中计算，避免nodata稀释或抬高风险比例。
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
    # 优先使用可追溯的地块内云量；旧窗口填充比例不可信时由 effective_cloud_pct 回退到 STAC。
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
    if cloud_cover_over_30 is False:
        return True
    # 云量数值和阈值标记都缺失时质量不可验证，不能默认把未知观测作为晴空证据。
    return False


def is_decloud_product(
    source: str | None = None,
    scene_id: str | None = None,
) -> bool:
    """True when the row is the additive decloud product, not raw S2."""
    # 兼容不同历史写入路径产生的空白或大小写差异，避免派生产品落入原始景分支。
    if (source or "").strip().lower() == DECLOUD_SOURCE:
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

    中文业务约束：原始景按可信云量筛选，去云景按产品质量等级筛选；
    ``fair`` / ``bad`` 仍保留用于追溯，但不能混入官方指标基线。
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


def decloud_scene_id_sql(alias: str = "") -> str:
    """生成与Python/Web ``endswith('_decloud')``一致的字面后缀SQL条件。"""
    if alias and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("scene_id SQL alias must be a simple identifier")
    column = f"{alias}.scene_id" if alias else "scene_id"
    # SQL LIKE会把后缀中的下划线当通配符；长度与字面值均来自同一契约常量。
    return (
        f"RIGHT(COALESCE({column}, ''), {len(DECLOUD_SCENE_ID_SUFFIX)}) "
        f"= '{DECLOUD_SCENE_ID_SUFFIX}'"
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
    parcel (~82% on small padded fields), and a large STAC-to-parcel cloud gap,
    fall back to STAC ``cloud_cover``. A stored over-limit flag is a fallback
    only when both numeric cloud measures are missing, matching Python/Web;
    scalar columns are preferred and JSONB is read only when normalized metadata
    is absent, preserving compatibility without parsing large payloads normally.

    中文业务约束：此谓词必须与Python及Web的官方景筛选一致，否则同一景会在
    地图、预警、报告和调度中得到不同资格；因此优先读取规范化标量列并兼容历史JSONB。
    """
    a = f"{alias}." if alias else ""
    trusted = ",".join(f"'{s}'" for s in sorted(PARCEL_CLOUD_SOURCES_TRUSTED))
    decloud_scene_id = decloud_scene_id_sql(alias)
    # 标量列优先，只有为空时才回退 JSONB，兼顾新入库路径和历史/部分写入记录。
    source = f"LOWER(COALESCE(NULLIF(BTRIM({a}product_source), ''), NULLIF(BTRIM({a}pixel_data->>'source'), ''), ''))"
    quality = f"LOWER(COALESCE(NULLIF(BTRIM({a}decloud_quality), ''), NULLIF(BTRIM({a}pixel_data->>'decloud_quality'), ''), ''))"
    cloud_source = f"LOWER(COALESCE(NULLIF(BTRIM({a}parcel_cloud_source), ''), NULLIF(BTRIM({a}pixel_data->>'parcel_cloud_source'), ''), ''))"
    effective = f"""(
        CASE
          WHEN {a}parcel_cloud_cover_pct IS NOT NULL
               AND {a}cloud_cover IS NOT NULL
               AND {a}parcel_cloud_cover_pct <= {SUSPICIOUS_CLEAR_PARCEL_MAX}
               AND {a}cloud_cover >= {SUSPICIOUS_STAC_OVERCAST_MIN}
          THEN {a}cloud_cover
          WHEN {a}parcel_cloud_cover_pct IS NOT NULL
               AND {a}cloud_cover IS NOT NULL
               AND {a}cloud_cover >= {SUSPICIOUS_STAC_MIN}
               AND ({a}cloud_cover - {a}parcel_cloud_cover_pct)
                   >= {SUSPICIOUS_STAC_OVER_PARCEL_GAP}
          THEN {a}cloud_cover
          WHEN {cloud_source} IN ({trusted})
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
        {source} <> '{DECLOUD_SOURCE}'
        AND NOT ({decloud_scene_id})
        AND (
          NOT ({effective} > :{cloud_param})
          OR ({effective} IS NULL AND {a}cloud_cover_over_30 IS FALSE)
        )
      )
      OR (
        (
          {source} = '{DECLOUD_SOURCE}'
          OR ({decloud_scene_id})
        )
        AND {quality} = '{DECLOUD_QUALITY_GOOD}'
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

    中文业务约束：排除目标日期本身，避免候选产品参与构造自己的参照值；
    优先使用前后窗口内的晴空原始景，窗口为空时才退回同月样本。
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
    """Borderline parcel, or STAC-clear while in-polygon parcel is cloudy.

    中文业务约束：仅对可信地块云量处于边界区间或与STAC明显冲突的原始景做指数参照；
    旧版窗口填充值不能触发这条择优分支。
    """
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
    # 指数距离相同时优先原始景，并以稳定scene_id兜底，避免输入顺序改变导致展示抖动。
    decloud_rank = 0 if _is_raw_scene(scene) else 1
    return (d_ndvi, d_ndmi, decloud_rank, str(scene.get("scene_id") or ""))


def _physiology_sort_key(
    scene: dict[str, Any],
    ndvi_med: float | None,
    ndmi_med: float | None,
    target_date: str,
) -> tuple[int, float, float, int, str]:
    """Lower is better for NDVI/growth pick vs nearby clear phenology.

    中文业务约束：先惩罚偏离邻近晴空基线过大的候选，再比较NDVI/NDMI距离；
    生长季冠层明显发绿时，异常偏低的去云结果不能仅凭云量优势胜出。
    """
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
    """Without neighbors: keep raw if it looks like canopy and decloud does not.

    中文业务约束：没有邻近晴空参照时，只在生长季且原始NDVI符合冠层范围、
    去云NDVI明显偏低时保留原始景，其他情况交由正式景规则兜底。
    """
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

    中文业务约束：该结果进入干旱与正式时序指标；仅允许晴空原始景或``good``去云景，
    边界云量场景再用邻近晴空原始景作参照，避免把普通云污染当作物候变化。
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

    中文业务约束：生长曲线选择器会结合邻近晴空基线和作物物候纠正明显不合理的去云值，
    但干旱与地块正式指标仍使用``pick_official_optical``，避免把展示型择优规则扩大到判级。
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
    """Cloud % + decloud quality description inputs for an NDVI point.

    中文业务约束：前端提示复用官方筛选结果和去云质量原因，区分真实清晰原始景、
    合格去云景及仅保留审计的低质量去云产品。
    """
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
    """计算中位数；偶数样本取中间两值均值，空样本返回None。"""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _percentile_rank(value: float, values: list[float]) -> float | None:
    """用中位秩计算百分位，令并列值各计半份，避免阈值附近偏向某一侧。"""
    if not values:
        return None
    n = len(values)
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (below + 0.5 * equal) / n * 100.0


def _percentile(values: list[float], p: float) -> float | None:
    """按(n-1)位置线性插值求分位数，并将百分位范围限制在0到100。"""
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
    """只用生长季官方影像按自然月建基线；n计NDVI/NDMI齐全景数，NDDI另跳过无效分母。"""
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
    """按NDDI或相对NDVI降幅映射最高等级；是否构成旱情由上游交叉判据决定。"""
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
    """对单景分级；同月样本不足时不算异常百分位，旱情还须同时有水分或绿度下降证据。"""
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

    # 旱情需由 NDDI 绝对/同月异常和水分或绿度下降交叉支持，降低健康冠层的单指标误报。
    if (nddi_abs or nddi_anom) and (ndmi_dry or ndvi_dropped):
        return _drought_severity(nddi, ndvi_drop)
    return "normal"


def classify_drought_series(
    observations: list[OpticalObs],
    *,
    season_months: tuple[int, ...] | list[int] = PHENOLOGY_MONTHS,
) -> list[tuple[str, DroughtClass]]:
    """逐景分级，同月基线使用输入序列的全部官方生长季观测；扩展历史窗口可能更新旧日期等级。"""
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
    """优先读取STAC相对轨道；旧产品缺字段时再按平台偏移解析GRD产品ID。"""
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
    mission = m.group("mission").upper()
    try:
        abs_orbit = int(m.group("absolute_orbit"))
    except (TypeError, ValueError):
        return None
    offset = _S1_ORBIT_OFFSET.get(mission)
    if offset is None:
        return None
    return ((abs_orbit - offset) % 175) + 1


def parse_s1_platform(
    platform: str | None = None, scene_id: str | None = None
) -> str | None:
    """规范化STAC平台名；旧产品缺字段时从Sentinel-1产品ID回退解析。"""
    normalized = re.sub(r"[^A-Z0-9]", "", str(platform or "").upper())
    if normalized in {"S1A", "S1B", "S1C", "S1D"}:
        return normalized
    if normalized in {"SENTINEL1A", "SENTINEL1B", "SENTINEL1C", "SENTINEL1D"}:
        return f"S1{normalized[-1]}"
    match = _S1_ID_RE.match(str(scene_id or "").strip())
    return match.group("mission").upper() if match else None


def _parse_s1_datetime(value: Any) -> datetime | None:
    """把STAC或产品ID时间规范到UTC，保证S1C校准分界前后判断一致。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text or ("T" not in text and " " not in text):
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        # STAC acquisition timestamps and Sentinel-1 product IDs are UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def s1_calibration_epoch(
    platform: str | None,
    acquisition_datetime: str | datetime | None = None,
    *,
    scene_id: str | None = None,
    stac_item_id: str | None = None,
    acquisition_date: str | date_cls | None = None,
) -> str | None:
    """推断S1C AUX_CAL时期以隔离基线；此标签不代表已对历史数据做后向补偿。"""
    platform_code = parse_s1_platform(platform, scene_id or stac_item_id)
    if platform_code != "S1C":
        return None

    parsed = _parse_s1_datetime(acquisition_datetime)
    if parsed is None:
        for item_id in (scene_id, stac_item_id):
            match = _S1_ID_RE.match(str(item_id or "").strip())
            if match:
                parsed = _parse_s1_datetime(match.group("acquisition_datetime"))
                if parsed is not None:
                    break
    if parsed is not None:
        return (
            S1C_CALIBRATION_EPOCH_PRE
            if parsed < _S1C_CALIBRATION_CUTOFF_UTC
            else S1C_CALIBRATION_EPOCH_POST
        )

    date_text = str(acquisition_date or "")[:10]
    try:
        parsed_date = date_cls.fromisoformat(date_text)
    except (TypeError, ValueError):
        return "s1c-auxcal-unknown"
    if parsed_date < _S1C_CALIBRATION_CUTOFF_UTC.date():
        return S1C_CALIBRATION_EPOCH_PRE
    if parsed_date > _S1C_CALIBRATION_CUTOFF_UTC.date():
        return S1C_CALIBRATION_EPOCH_POST
    # 日期级历史记录无法判断变更日15:14 UTC前后的处理配置。
    return S1C_CALIBRATION_EPOCH_UNKNOWN


def orbit_group_key(obs: SarObs) -> str:
    rel = parse_s1_relative_orbit(
        obs.get("scene_id") or obs.get("stac_item_id"), obs.get("relative_orbit")
    )
    if rel is None:
        return "unknown"
    return f"ron{rel}"


def classify_flood_scene(
    vv: float | None,
    vh: float | None,
    baseline_vv: float | None,
    orbit_diff_p40: float | None,
) -> FloodClass | None:
    """按同轨或同定标组基线判断场景；基线不足不报干燥，VV-VH单独也不能确认洪涝。"""
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
    # 没有足够同口径观测时无法证明“干燥”；静态水体候选仍可保留为关注。
    if baseline_vv is None or not _finite(baseline_vv):
        return None
    return "dry"


def _flood_calibration_group_key(obs: SarObs) -> tuple[str, str, str, str, str]:
    """按平台、上游版本、S1C校准时期及数值尺度隔离VV基线。"""
    details = obs.get("radiometric_calibration")
    if not isinstance(details, dict):
        details = {}
    scene_id = obs.get("scene_id")
    stac_item_id = obs.get("stac_item_id")
    platform = parse_s1_platform(
        obs.get("platform") or details.get("platform"), scene_id or stac_item_id
    )
    method = str(
        obs.get("calibration_method") or details.get("method") or "legacy_unknown"
    ).strip()
    scale = _num(
        obs.get("calibration_scale")
        if obs.get("calibration_scale") is not None
        else details.get("fallback_scale")
    )
    raw_epoch = obs.get("calibration_epoch") or details.get("calibration_epoch")
    epoch = str(raw_epoch or "").strip()
    if not epoch:
        inferred_epoch = s1_calibration_epoch(
            platform,
            obs.get("acquisition_datetime") or details.get("acquisition_datetime"),
            scene_id=scene_id,
            stac_item_id=stac_item_id,
            acquisition_date=obs.get("date"),
        )
        epoch = inferred_epoch or "not_applicable"
    processing_version = str(
        obs.get("processing_version") or details.get("processing_version") or ""
    ).strip()
    # 缺少方法的旧记录仍按已存比例拆分；不能把未知定标和不同幅度尺度合并。
    scale_key = repr(scale) if scale is not None else "none"
    return (
        platform or "unknown_platform",
        epoch,
        processing_version or "unknown_processing_version",
        method or "legacy_unknown",
        scale_key,
    )


def _flood_baseline_group_key(
    obs: SarObs,
) -> tuple[str, tuple[str, str, str, str, str]]:
    """同时按相对轨道和物理定标口径分组，只有观测几何与尺度可比时才共享基线。"""
    return orbit_group_key(obs), _flood_calibration_group_key(obs)


def _median_from_sorted(values: list[float]) -> float | None:
    """输入须已升序；直接取中间值，避免逐日期历史前缀反复排序。"""
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2.0


def _percentile_from_sorted(values: list[float], p: float) -> float | None:
    """输入须已升序；用(n-1)位置线性插值，与非增量分类器的分位口径一致。"""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = max(0.0, min(100.0, p)) / 100.0 * (len(values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _classify_flood_series_date_bounded(
    observations: list[SarObs],
) -> list[tuple[str, FloodClass | None]]:
    """按日期前缀累计同轨/同定标样本，供需要回看历史标签的报告使用。"""
    observations_by_date: dict[
        str,
        dict[tuple[str, tuple[str, str, str, str, str]], list[tuple[int, SarObs]]],
    ] = defaultdict(lambda: defaultdict(list))
    for index, obs in enumerate(observations):
        day = str(obs.get("date") or "").strip()
        # 无法确定采集日期的记录不能安全地放入任一历史时间前缀。
        if not day or _num(obs.get("vv")) is None:
            continue
        observations_by_date[day][_flood_baseline_group_key(obs)].append((index, obs))

    orbit_samples: dict[
        tuple[str, tuple[str, str, str, str, str]], tuple[list[float], list[float]]
    ] = defaultdict(lambda: ([], []))
    calibration_samples: dict[
        tuple[str, str, str, str, str], tuple[list[float], list[float]]
    ] = defaultdict(lambda: ([], []))
    classes: dict[int, FloodClass | None] = {}

    for day in sorted(observations_by_date):
        groups = observations_by_date[day]
        # 同一日期的样本先整体加入，再分类，避免同日景数和输入顺序造成口径差异。
        for group_key, rows in groups.items():
            orbit_vv, orbit_diff = orbit_samples[group_key]
            calibration_vv, calibration_diff = calibration_samples[group_key[1]]
            for _, obs in rows:
                vv = _num(obs.get("vv"))
                vh = _num(obs.get("vh"))
                if vv is None:
                    continue
                # 复用有序历史前缀，避免每个日期都重新排序完整样本；数组插入仍有线性搬移成本。
                insort(orbit_vv, vv)
                insort(calibration_vv, vv)
                if vh is not None:
                    difference = vv - vh
                    if _finite(difference):
                        insort(orbit_diff, difference)
                        insort(calibration_diff, difference)

        for group_key, rows in groups.items():
            vv_samples, diff_samples = orbit_samples[group_key]
            # 轨道有效样本不足时仅回退到同定标口径的跨轨样本，不混合不同尺度。
            if len(vv_samples) < MIN_ORBIT_SAMPLES:
                vv_samples, diff_samples = calibration_samples[group_key[1]]
            baseline_vv = (
                _median_from_sorted(vv_samples)
                if len(vv_samples) >= MIN_ORBIT_SAMPLES
                else None
            )
            diff_p40 = (
                _percentile_from_sorted(diff_samples, VV_VH_DIFF_PCTL)
                if len(diff_samples) >= MIN_ORBIT_SAMPLES
                else None
            )
            for index, obs in rows:
                classes[index] = classify_flood_scene(
                    _num(obs.get("vv")),
                    _num(obs.get("vh")),
                    baseline_vv,
                    diff_p40,
                )

    return [
        (str(obs.get("date") or ""), classes.get(index))
        for index, obs in enumerate(observations)
    ]


def classify_flood_series(
    observations: list[SarObs],
    *,
    date_bounded: bool = False,
) -> list[tuple[str, FloodClass | None]]:
    """按轨道和定标口径建VV基线；历史报告可启用逐日期截止，避免未来影像泄漏。"""
    if date_bounded:
        return _classify_flood_series_date_bounded(observations)

    groups: dict[tuple[str, tuple[str, str, str, str, str]], list[SarObs]] = defaultdict(list)
    calibration_groups: dict[tuple[str, str, str, str, str], list[SarObs]] = defaultdict(list)
    for obs in observations:
        if _num(obs.get("vv")) is None:
            continue
        calibration_key = _flood_calibration_group_key(obs)
        groups[_flood_baseline_group_key(obs)].append(obs)
        calibration_groups[calibration_key].append(obs)

    baselines: dict[
        tuple[str, tuple[str, str, str, str, str]], tuple[float | None, float | None]
    ] = {}
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
            baselines[key] = (
                _median(vvs_f),
                _percentile(diffs, VV_VH_DIFF_PCTL)
                if len(diffs) >= MIN_ORBIT_SAMPLES
                else None,
            )
        else:
            # 轨道样本不足时只借同定标口径的其他轨道，避免把传感器尺度差异当作水位突变。
            calibration_key = key[1]
            compatible_rows = calibration_groups.get(calibration_key, [])
            all_vv = [_num(r.get("vv")) for r in compatible_rows]
            all_f = [v for v in all_vv if v is not None]
            all_diff: list[float] = []
            for r in compatible_rows:
                vv = _num(r.get("vv"))
                vh = _num(r.get("vh"))
                if vv is not None and vh is not None:
                    all_diff.append(vv - vh)
            # “不足3景则取全部中位数”会让1-2景也产生确认分级；样本不够时保留未知。
            baselines[key] = (
                _median(all_f) if len(all_f) >= MIN_ORBIT_SAMPLES else None,
                _percentile(all_diff, VV_VH_DIFF_PCTL)
                if len(all_diff) >= MIN_ORBIT_SAMPLES
                else None,
            )

    out: list[tuple[str, FloodClass | None]] = []
    for obs in observations:
        date_str = str(obs.get("date") or "")
        vv = _num(obs.get("vv"))
        vh = _num(obs.get("vh"))
        base, p40 = baselines.get(_flood_baseline_group_key(obs), (None, None))
        out.append((date_str, classify_flood_scene(vv, vh, base, p40)))
    return out


def is_drought_day_class(cls: DroughtClass | str | None) -> bool:
    return cls in ("mild", "moderate", "severe")


def _finite(v: float) -> bool:
    return v == v and v not in (float("inf"), float("-inf"))
