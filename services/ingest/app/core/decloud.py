"""Optional UnCRtainTS decloud feature flag, trigger, and quality gate.

Stdlib-only so CI ingest tests can import this without torch/numpy/rasterio.

Product rules:
- Additive: never overwrite raw S2 lonlat rows.
- Trigger when scene/parcel cloud > DECLOUD_CLOUD_MIN_PCT (default 30).
- STAC search/ingest cap is DECLOUD_STAC_CLOUD_MAX_PCT (default 90).
- When enabled, default ``DECLOUD_MODE=batch``: buffer many parcel windows,
  then decloud; do not publish the official cloudy-date product as soon as
  one raw scene finishes.
- Store as sensor=S2 with scene_id suffix ``_decloud`` and
  pixel_data.source = ``uncrtaints_decloud``.
- Store fair/bad reconstructions too (audit). Official drought / land
  metrics still accept only quality ``good``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

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
DecloudMode = Literal["batch", "per_scene"]

DEFAULT_CLOUD_MIN_PCT = CLOUD_MAX_PCT
DEFAULT_STAC_CLOUD_MAX_PCT = 90.0
DEFAULT_INPUT_T = 3
DEFAULT_CHECKPOINT_NAME = "diagonal_1"
DEFAULT_MODE: DecloudMode = "batch"
DEFAULT_LOOKBACK_DAYS = 45

# Extra L2A assets agri optical indices do not already pull (B10 is zeros).
# Keys match UnCRtainTS S2_L2A_ASSET_MAP / Element84 sentinel-2-l2a.
DECLOUD_EXTRA_S2_ASSETS: dict[str, tuple[str, ...]] = {
    "B01": ("coastal", "B01"),
    "B06": ("rededge2", "B06"),
    "B8A": ("nir08", "B8A"),
    "B09": ("nir09", "B09"),
}

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


def decloud_mode() -> DecloudMode:
    """When enabled, default is batch-after-buffer (not per-scene STAC scrape)."""
    raw = (os.environ.get("DECLOUD_MODE") or DEFAULT_MODE).strip().lower()
    if raw in ("per_scene", "per-scene", "scene", "immediate"):
        return "per_scene"
    return "batch"


def decloud_lookback_days() -> int:
    return max(1, _parse_int_env("DECLOUD_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS))


def decloud_s2_extra_assets() -> dict[str, tuple[str, ...]]:
    """STAC extras so the optical job can cache a full UnCRtainTS L2A window."""
    return dict(DECLOUD_EXTRA_S2_ASSETS)


def batch_neighbors_ready(usable_s2: int, input_t: int | None = None) -> bool:
    """True when the local window cache has enough S2 dates for ``input_t``."""
    need = decloud_input_t() if input_t is None else max(1, int(input_t))
    return int(usable_s2) >= need


def should_enqueue_per_scene_decloud(
    *,
    mode: str | None = None,
    cached_neighbor_count: int,
    input_t: int | None = None,
) -> bool:
    """Per-scene Celery hop only in per_scene mode when neighbors are already cached."""
    resolved = decloud_mode() if mode is None else str(mode).strip().lower()
    if resolved in ("per-scene", "scene", "immediate"):
        resolved = "per_scene"
    if resolved != "per_scene":
        return False
    return batch_neighbors_ready(cached_neighbor_count, input_t)


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if text else None


def cloudy_targets_from_raw(
    raw_results: list[dict[str, Any]] | None,
    *,
    cloud_min_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Cloudy raw lonlat rows that still need an additive decloud attempt."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw_results or []:
        if not row:
            continue
        date_str = _iso_date(row.get("date"))
        if not date_str or date_str in seen:
            continue
        if not should_trigger_decloud(
            cloud_cover_over_30=row.get("cloud_cover_over_30"),
            parcel_cloud_cover_pct=row.get("parcel_cloud_cover_pct"),
            cloud_cover=row.get("cloud_cover"),
            cloud_min_pct=cloud_min_pct,
        ):
            continue
        seen.add(date_str)
        out.append(
            {
                "date": date_str,
                "raw_scene_id": row.get("scene_id") or row.get("raw_scene_id"),
                "stac_cloud": row.get("cloud_cover", row.get("stac_cloud")),
                "parcel_cloud": row.get(
                    "parcel_cloud_cover_pct", row.get("parcel_cloud")
                ),
                "cloud_over_30": row.get("cloud_cover_over_30", row.get("cloud_over_30")),
            }
        )
    return out


@dataclass(frozen=True)
class DecloudPlan:
    """What to do after raw lonlat is stored for a job.

    Raw is always kept. Decloud OSS/MQ is held until a batch (or a per-scene
    hop that already has cached neighbors) can run.
    """

    store_raw: bool
    per_scene_dates: tuple[str, ...]
    batch: bool
    batch_targets: tuple[dict[str, Any], ...]
    hold_decloud_dates: tuple[str, ...]


def plan_decloud_after_raw(
    *,
    enabled: bool,
    mode: str | None = None,
    raw_results: list[dict[str, Any]] | None,
    cached_neighbor_counts: dict[str, int] | None = None,
    input_t: int | None = None,
    cloud_min_pct: float | None = None,
) -> DecloudPlan:
    """Decide per-scene vs batch decloud after raw products are stored.

    Batch (default): never enqueue per-scene; one job-level batch after the
    buffer has enough temporal context.
    Per-scene: immediate hop only when ``input_t`` neighbors are already
    cached; otherwise those dates still go to batch.
    """
    targets = (
        cloudy_targets_from_raw(raw_results, cloud_min_pct=cloud_min_pct)
        if enabled
        else []
    )
    if not enabled or not targets:
        return DecloudPlan(True, (), False, (), ())

    resolved = (mode or decloud_mode()).strip().lower()
    if resolved in ("per-scene", "scene", "immediate"):
        resolved = "per_scene"
    counts = cached_neighbor_counts or {}
    need = decloud_input_t() if input_t is None else max(1, int(input_t))

    if resolved != "per_scene":
        hold = tuple(t["date"] for t in targets)
        return DecloudPlan(True, (), True, tuple(targets), hold)

    ready: list[str] = []
    delayed: list[dict[str, Any]] = []
    for target in targets:
        date_str = target["date"]
        if should_enqueue_per_scene_decloud(
            mode="per_scene",
            cached_neighbor_count=int(counts.get(date_str, 0) or 0),
            input_t=need,
        ):
            ready.append(date_str)
        else:
            delayed.append(target)
    hold = tuple(t["date"] for t in delayed)
    return DecloudPlan(
        True,
        tuple(ready),
        bool(delayed),
        tuple(delayed),
        hold,
    )


def pick_temporal_scenes(
    scenes: list[dict[str, Any]],
    target: date,
    input_t: int,
) -> list[dict[str, Any]]:
    """Target plus nearest other S2 dates, length ``input_t`` (repeat if needed)."""
    if not scenes:
        return []
    need = max(1, int(input_t))
    target_scene = None
    others: list[dict[str, Any]] = []
    for sc in scenes:
        sc_date = sc.get("date")
        if isinstance(sc_date, str):
            sc_date = date.fromisoformat(sc_date[:10])
        if sc_date == target:
            target_scene = sc
        else:
            others.append(sc)
    if target_scene is None:
        target_scene = min(
            scenes,
            key=lambda s: abs((_scene_date(s) - target).days),
        )
        others = [s for s in scenes if s is not target_scene]
    others.sort(key=lambda s: abs((_scene_date(s) - target).days))
    neighbors = others[: max(0, need - 1)]
    neighbors.sort(key=lambda s: _scene_date(s))
    # Reconstruct the last timestep (UnCRtainTS mean[0, -1] and dummy backend).
    ordered = neighbors + [target_scene]
    while len(ordered) < need:
        ordered.insert(0, ordered[0])
    return ordered[:need]


def _scene_date(scene: dict[str, Any]) -> date:
    value = scene.get("date")
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def neighbor_window(target: date, lookback_days: int | None = None) -> tuple[date, date]:
    days = decloud_lookback_days() if lookback_days is None else max(1, int(lookback_days))
    delta = timedelta(days=days)
    return target - delta, target + delta


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


def should_persist_decloud_product(
    *,
    has_reconstruction: bool,
    pixel_count: int = 0,
    has_finite_index: bool = False,
) -> bool:
    """Whether OSS/MQ should receive a ``_decloud`` row.

    Persist whenever UnCRtainTS produced a reconstruction, including fair/bad
    and sparse/weak pixels. Empty lonlat after a harsh quality path is not a
    skip if zonal means or any pixel exist. Skip only when there is nothing
    to store (no reconstruct array at all).
    """
    return bool(has_reconstruction) or pixel_count > 0 or has_finite_index


def decloud_drought_exclusion_flags(
    is_official: bool,
) -> tuple[bool, float | None]:
    """``(cloud_cover_over_30, parcel_cloud_cover_pct)`` fallback for a decloud row.

    Fair/bad stay out of drought SQL (quality != good) and the legacy
    cloud>30 filter (over_30 True, parcel 100). Good rows keep over_30 False
    for that legacy filter but must not invent a 0% parcel cloud; callers
    persist the raw parcel metric or NULL (tooltip then uses STAC).
    """
    if is_official:
        # Drought SQL includes good decloud via quality, not a fake 0% parcel.
        # Persist NULL so tooltips fall back to STAC instead of inventing 0.
        return False, None
    return True, 100.0


def decloud_pixel_payload(
    *,
    quality: str,
    score: float,
    reasons: list[str] | None,
    raw_scene_id: str | None,
    pixels: list[dict[str, Any]],
) -> dict[str, Any]:
    """lonlat_v1 object stored on the decloud product (quality always present)."""
    return {
        "format": "lonlat_v1",
        "source": DECLOUD_SOURCE,
        "decloud_quality": quality,
        "decloud_score": score,
        "decloud_reasons": list(reasons or []),
        "raw_scene_id": raw_scene_id,
        "pixels": pixels,
    }


def geojson_ring_centroid(
    geom: dict[str, Any] | None,
) -> tuple[float, float] | None:
    """Average vertex of the outer ring. Stdlib; good enough for a stub pixel."""
    if not isinstance(geom, dict):
        return None
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    rings: list[Any] = []
    if gtype == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        try:
            return float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            return None
    if gtype == "Polygon" and coords:
        rings = [coords[0]] if coords else []
    elif gtype == "MultiPolygon" and coords:
        rings = [part[0] for part in coords if part]
    xs: list[float] = []
    ys: list[float] = []
    for ring in rings:
        if not isinstance(ring, (list, tuple)):
            continue
        for pt in ring:
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                continue
            try:
                xs.append(float(pt[0]))
                ys.append(float(pt[1]))
            except (TypeError, ValueError):
                continue
    if not xs:
        return None
    return sum(xs) / len(xs), sum(ys) / len(ys)


def fallback_lonlat_pixels(
    *,
    pixels: list[dict[str, Any]] | None,
    index_avgs: dict[str, float | None],
    lon: float | None,
    lat: float | None,
    allow_zero_stub: bool = False,
) -> list[dict[str, Any]]:
    """Keep sampled pixels; otherwise one centroid pixel from zonal means."""
    if pixels:
        return list(pixels)
    if lon is None or lat is None:
        return []
    finite = {
        key: value
        for key, value in index_avgs.items()
        if value is not None and value == value
    }
    if not finite:
        if not allow_zero_stub:
            return []
        finite = {"NDVI": 0.0}
    pix: dict[str, Any] = {
        "lon": round(float(lon), 6),
        "lat": round(float(lat), 6),
        "clear": 0,
    }
    pix.update(finite)
    if "NDVI" not in pix:
        pix["NDVI"] = 0.0
    return [pix]


def window_array_tmp_path(path: Path) -> Path:
    """Sibling temp path that still ends in ``.npz``.

    ``numpy.savez_compressed`` appends ``.npz`` when the name does not already
    end with that suffix, so ``foo.npz.tmp`` is written as ``foo.npz.tmp.npz``
    and the subsequent replace misses the file. Include the pid so two
    workers writing the same date do not share a temp name.
    """
    return path.with_name(f"{path.stem}.{os.getpid()}.writing.npz")


__all__ = [
    "DECLOUD_EXTRA_S2_ASSETS",
    "DECLOUD_QUALITY_GOOD",
    "DECLOUD_SCENE_ID_SUFFIX",
    "DECLOUD_SOURCE",
    "DecloudMode",
    "DecloudPlan",
    "DecloudQuality",
    "DecloudQualityInputs",
    "DecloudQualityResult",
    "batch_neighbors_ready",
    "cloudy_targets_from_raw",
    "decloud_backend",
    "decloud_cloud_min_pct",
    "decloud_enabled",
    "decloud_input_t",
    "decloud_lookback_days",
    "decloud_mode",
    "decloud_oss_sensor",
    "decloud_s2_extra_assets",
    "decloud_scene_id",
    "decloud_stac_cloud_max_pct",
    "decloud_use_sar",
    "is_decloud_product",
    "is_official_optical_product",
    "neighbor_window",
    "official_s2_sql",
    "pick_temporal_scenes",
    "plan_decloud_after_raw",
    "score_decloud",
    "should_enqueue_per_scene_decloud",
    "should_persist_decloud_product",
    "should_trigger_decloud",
    "decloud_drought_exclusion_flags",
    "decloud_pixel_payload",
    "fallback_lonlat_pixels",
    "geojson_ring_centroid",
    "window_array_tmp_path",
    "uncrtaints_checkpoint_dir",
    "uncrtaints_checkpoint_name",
    "uncrtaints_home",
]
