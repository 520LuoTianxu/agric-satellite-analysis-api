"""Optional UnCRtainTS decloud feature flag, trigger, and quality gate.

Stdlib-only so CI ingest tests can import this without torch/numpy/rasterio.

Product rules:
- Additive: never overwrite raw S2 lonlat rows.
- Trigger when scene/parcel cloud > DECLOUD_CLOUD_MIN_PCT (default 30).
- STAC search/ingest cap is DECLOUD_STAC_CLOUD_MAX_PCT (default 90).
- Store as sensor=S2 with scene_id suffix ``_decloud`` and
  pixel_data.source = ``uncrtaints_decloud``.
- Official drought / timeseries / land metrics accept only quality ``good``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from app.core.agri_classify import (
    CLOUD_MAX_PCT,
    DECLOUD_QUALITY_GOOD,
    DECLOUD_SCENE_ID_SUFFIX,
    DECLOUD_SOURCE,
    is_decloud_product,
    is_official_optical_product,
    official_s2_sql,
)

DecloudQuality = Literal["good", "fair", "bad"]
DecloudBackend = Literal["uncrtaints", "dummy"]

DEFAULT_CLOUD_MIN_PCT = CLOUD_MAX_PCT
DEFAULT_STAC_CLOUD_MAX_PCT = 90.0
DEFAULT_INPUT_T = 3
DEFAULT_CHECKPOINT_NAME = "diagonal_1"

# Quality heuristics (0-1 reflectance units) from the parcel-window pilot.
RGB_BRIGHT_BAD = 0.45
RGB_BRIGHT_FAIR = 0.32
DELTA_RGB_MIN = 0.05
STD_COLLAPSE_RATIO = 0.25
STD_COLLAPSE_ABS = 0.015
NDVI_GAP_FAIR = 0.20
NDVI_GAP_BAD = 0.35

GOOD_SCORE_MIN = 0.70
FAIR_SCORE_MIN = 0.40


def _parse_bool_env(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value == "":
        return None
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def _parse_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _parse_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def decloud_enabled() -> bool:
    """Master switch. Default off so download hosts without GPU/weights are unchanged."""
    explicit = _parse_bool_env("DECLOUD_ENABLED")
    return bool(explicit)


def decloud_cloud_min_pct() -> float:
    return _parse_float_env("DECLOUD_CLOUD_MIN_PCT", DEFAULT_CLOUD_MIN_PCT)


def decloud_stac_cloud_max_pct() -> float:
    """STAC eo:cloud_cover upper bound used only when decloud is enabled."""
    return _parse_float_env("DECLOUD_STAC_CLOUD_MAX_PCT", DEFAULT_STAC_CLOUD_MAX_PCT)


def decloud_backend() -> DecloudBackend:
    raw = (os.environ.get("DECLOUD_BACKEND") or "uncrtaints").strip().lower()
    if raw in ("dummy", "smoke", "mock"):
        return "dummy"
    return "uncrtaints"


def decloud_input_t() -> int:
    return max(1, _parse_int_env("DECLOUD_INPUT_T", DEFAULT_INPUT_T))


def decloud_use_sar() -> bool:
    explicit = _parse_bool_env("DECLOUD_USE_SAR")
    return True if explicit is None else explicit


def uncrtaints_checkpoint_dir() -> str:
    return (os.environ.get("UNCRTAINTS_CHECKPOINT_DIR") or "").strip()


def uncrtaints_home() -> str:
    return (os.environ.get("UNCRTAINTS_HOME") or "").strip()


def uncrtaints_checkpoint_name() -> str:
    raw = (os.environ.get("UNCRTAINTS_CHECKPOINT_NAME") or "").strip()
    return raw or DEFAULT_CHECKPOINT_NAME


def decloud_scene_id(date_str: str) -> str:
    """Stable additive scene_id; does not collide with raw stac_bridge_*_S2."""
    return f"stac_bridge_{date_str}_S2{DECLOUD_SCENE_ID_SUFFIX}"


def decloud_oss_sensor() -> str:
    """OSS / MQ label suffix so {date}_S2.json stays the raw product."""
    return "S2_decloud"


def should_trigger_decloud(
    *,
    cloud_cover_over_30: bool | None = None,
    parcel_cloud_cover_pct: float | None = None,
    cloud_cover: float | None = None,
    cloud_min_pct: float | None = None,
    cloud_max_pct: float | None = None,
) -> bool:
    """True when parcel or STAC cloud is above min and STAC is within the ingest max.

    Trigger if in-polygon parcel cloud > min (default 30) **or** STAC
    ``eo:cloud_cover`` > min, up to ``DECLOUD_STAC_CLOUD_MAX_PCT`` (default 90).
    Scenes above the max should not have been ingested.
    """
    lo = DEFAULT_CLOUD_MIN_PCT if cloud_min_pct is None else float(cloud_min_pct)
    hi = (
        decloud_stac_cloud_max_pct()
        if cloud_max_pct is None
        else float(cloud_max_pct)
    )

    def _f(v: float | None) -> float | None:
        if v is None:
            return None
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        if x != x or x in (float("inf"), float("-inf")):
            return None
        return x

    stac = _f(cloud_cover)
    parcel = _f(parcel_cloud_cover_pct)
    if stac is not None and stac > hi:
        return False
    parcel_hi = parcel is not None and parcel > lo
    stac_hi = stac is not None and stac > lo
    if parcel is not None or stac is not None:
        return bool(parcel_hi or stac_hi)
    return cloud_cover_over_30 is True


@dataclass(frozen=True)
class DecloudQualityInputs:
    """Scalar summaries (0-1 reflectance) used by the quality gate."""

    rgb_mean: float
    rgb_mean_raw: float
    rgb_std: float
    rgb_std_raw: float
    ndvi_mean: float
    neighbor_ndvi_mean: float | None = None


@dataclass
class DecloudQualityResult:
    quality: DecloudQuality
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def is_official(self) -> bool:
        return self.quality == DECLOUD_QUALITY_GOOD


def _finite(v: float) -> bool:
    return v == v and v not in (float("inf"), float("-inf"))


def score_decloud(inp: DecloudQualityInputs) -> DecloudQualityResult:
    """Score a reconstructed parcel window as good / fair / bad.

    Pilot heuristics that mapped to fair/bad:
    - RGB mean still too bright after decloud
    - tiny delta vs the cloudy raw RGB
    - spatial std collapse (over-smoothed)
    - NDVI far below clear-month neighbors
    """
    reasons: list[str] = []
    fatal = False
    score = 1.0

    rgb = float(inp.rgb_mean)
    rgb_raw = float(inp.rgb_mean_raw)
    std = float(inp.rgb_std)
    std_raw = float(inp.rgb_std_raw)
    ndvi = float(inp.ndvi_mean)

    if not _finite(rgb) or not _finite(ndvi):
        return DecloudQualityResult("bad", 0.0, ["non_finite_reconstruction"])

    if rgb >= RGB_BRIGHT_BAD:
        reasons.append("rgb_still_bright")
        score -= 0.45
        fatal = True
    elif rgb >= RGB_BRIGHT_FAIR:
        reasons.append("rgb_bright")
        score -= 0.20

    if _finite(rgb_raw) and rgb_raw > 1e-6:
        delta = abs(rgb - rgb_raw) / rgb_raw
        if delta < DELTA_RGB_MIN:
            reasons.append("tiny_rgb_delta")
            score -= 0.30
            if delta < DELTA_RGB_MIN / 2:
                fatal = True

    if _finite(std) and _finite(std_raw) and std_raw > STD_COLLAPSE_ABS:
        ratio = std / std_raw if std_raw > 1e-9 else 1.0
        if std < STD_COLLAPSE_ABS and ratio < STD_COLLAPSE_RATIO:
            reasons.append("spatial_std_collapse")
            score -= 0.35
            fatal = True
        elif ratio < STD_COLLAPSE_RATIO:
            reasons.append("spatial_std_low")
            score -= 0.15

    neighbor = inp.neighbor_ndvi_mean
    if neighbor is not None and _finite(float(neighbor)):
        gap = float(neighbor) - ndvi
        if gap >= NDVI_GAP_BAD:
            reasons.append("ndvi_far_below_neighbors")
            score -= 0.40
            fatal = True
        elif gap >= NDVI_GAP_FAIR:
            reasons.append("ndvi_below_neighbors")
            score -= 0.20

    if score < 0:
        score = 0.0
    if score > 1:
        score = 1.0

    if fatal or score < FAIR_SCORE_MIN:
        quality: DecloudQuality = "bad"
    elif score < GOOD_SCORE_MIN or reasons:
        quality = "fair"
    else:
        quality = "good"

    return DecloudQualityResult(quality, round(score, 4), reasons)


__all__ = [
    "DECLOUD_QUALITY_GOOD",
    "DECLOUD_SCENE_ID_SUFFIX",
    "DECLOUD_SOURCE",
    "DecloudQuality",
    "DecloudQualityInputs",
    "DecloudQualityResult",
    "decloud_backend",
    "decloud_cloud_min_pct",
    "decloud_enabled",
    "decloud_input_t",
    "decloud_oss_sensor",
    "decloud_scene_id",
    "decloud_stac_cloud_max_pct",
    "decloud_use_sar",
    "is_decloud_product",
    "is_official_optical_product",
    "official_s2_sql",
    "score_decloud",
    "should_trigger_decloud",
    "uncrtaints_checkpoint_dir",
    "uncrtaints_checkpoint_name",
    "uncrtaints_home",
]
