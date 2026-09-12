"""Observation-only harvest day detection from official NDVI series.

Never interpolates dates between scenes. Soft-fail → uncertain / no_growth.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from statistics import median
from typing import Any


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class HarvestThresholds:
    grow_min: float = 0.35
    drop_frac: float = 0.35
    lookback_k: int = 3
    confirm_m: int = 1
    min_clear_points: int = 4

    @classmethod
    def from_env(cls) -> "HarvestThresholds":
        return cls(
            grow_min=_env_float("HARVEST_GROW_MIN_NDVI", 0.35),
            drop_frac=_env_float("HARVEST_DROP_FRAC", 0.35),
            lookback_k=max(1, _env_int("HARVEST_LOOKBACK_K", 3)),
            confirm_m=max(0, _env_int("HARVEST_CONFIRM_M", 1)),
            min_clear_points=max(2, _env_int("HARVEST_MIN_CLEAR_POINTS", 4)),
        )


@dataclass
class HarvestDetectResult:
    status: str  # detected | uncertain | no_growth
    harvest_date: str | None = None
    confidence: str | None = None  # high | medium | low
    evidence: dict[str, Any] = field(default_factory=dict)
    scene_id: str | None = None
    alternates: list[dict[str, Any]] = field(default_factory=list)
    window: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _is_official_point(pt: dict[str, Any]) -> bool:
    """Keep only official / good optical points (no fair/bad decloud)."""
    if pt.get("official") is False:
        return False
    quality = str(pt.get("decloud_quality") or pt.get("quality") or "").strip().lower()
    if quality in {"fair", "bad", "poor"}:
        return False
    # Explicit good / official flags
    if pt.get("official") is True:
        return True
    if quality in {"good", "official", ""}:
        # empty quality + has ndvi treated as usable if caller filtered
        return True
    if pt.get("is_official") is True:
        return True
    source = str(pt.get("source") or "").strip().lower()
    if "decloud" in source and quality not in {"good", ""}:
        return False
    return quality in {"good", "official"} or pt.get("official") is True


def _ndvi_of(pt: dict[str, Any]) -> float | None:
    for key in ("ndvi_avg", "ndvi", "value"):
        if pt.get(key) is None:
            continue
        try:
            return float(pt[key])
        except (TypeError, ValueError):
            continue
    return None


def filter_official_ndvi_points(
    points: list[dict[str, Any]],
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    """Sort and filter to dated official points with NDVI inside optional window."""
    out: list[dict[str, Any]] = []
    for pt in points or []:
        if not isinstance(pt, dict):
            continue
        d = _parse_date(pt.get("date") or pt.get("scene_date"))
        if d is None:
            continue
        if start and d < start:
            continue
        if end and d > end:
            continue
        if not _is_official_point(pt):
            continue
        ndvi = _ndvi_of(pt)
        if ndvi is None:
            continue
        enriched = dict(pt)
        enriched["_date"] = d
        enriched["_ndvi"] = ndvi
        out.append(enriched)
    out.sort(key=lambda p: (p["_date"], str(p.get("scene_id") or "")))
    return out


def detect_harvest(
    official_ndvi_points: list[dict[str, Any]],
    window: dict[str, Any] | None = None,
    thresholds: HarvestThresholds | None = None,
) -> HarvestDetectResult:
    """Detect earliest harvest-like NDVI drop on real scene dates only."""
    thr = thresholds or HarvestThresholds.from_env()
    window = dict(window or {})
    start = _parse_date(window.get("start_date"))
    end = _parse_date(window.get("end_date"))
    points = filter_official_ndvi_points(
        official_ndvi_points, start=start, end=end
    )
    base_evidence = {
        "thresholds": {
            "grow_min": thr.grow_min,
            "drop_frac": thr.drop_frac,
            "lookback_k": thr.lookback_k,
            "confirm_m": thr.confirm_m,
            "min_clear_points": thr.min_clear_points,
        },
        "clear_point_count": len(points),
        "note": "observation_only_no_interpolation",
    }
    win_meta = {
        k: window.get(k)
        for k in ("start_date", "end_date", "label", "crops", "crop")
        if window.get(k) is not None
    }

    if len(points) < thr.min_clear_points:
        return HarvestDetectResult(
            status="uncertain",
            confidence="low",
            evidence={**base_evidence, "reason": "too_few_clear_points"},
            window=win_meta,
        )

    grew = any(p["_ndvi"] >= thr.grow_min for p in points)
    if not grew:
        return HarvestDetectResult(
            status="no_growth",
            confidence="low",
            evidence={**base_evidence, "reason": "ndvi_never_reached_grow_min"},
            window=win_meta,
        )

    candidates: list[dict[str, Any]] = []
    for i, pt in enumerate(points):
        if i < thr.lookback_k:
            continue
        lookback = points[i - thr.lookback_k : i]
        baseline = median([p["_ndvi"] for p in lookback])
        if baseline <= 0:
            continue
        drop = (baseline - pt["_ndvi"]) / baseline
        if drop < thr.drop_frac:
            continue
        # Post-harvest sustain: following clear points stay below grow_min
        following = points[i + 1 : i + 1 + thr.confirm_m] if thr.confirm_m else []
        if thr.confirm_m > 0 and len(following) >= thr.confirm_m:
            if any(p["_ndvi"] >= thr.grow_min for p in following):
                continue
            confidence = "high" if drop >= thr.drop_frac + 0.1 else "medium"
        elif thr.confirm_m > 0 and len(following) == 0:
            confidence = "low"  # window end, drop only
        else:
            # partial follow-up
            if following and any(p["_ndvi"] >= thr.grow_min for p in following):
                continue
            confidence = "medium" if following else "low"

        candidates.append(
            {
                "harvest_date": pt["_date"].isoformat(),
                "scene_id": pt.get("scene_id"),
                "ndvi": pt["_ndvi"],
                "baseline_ndvi": baseline,
                "drop_frac": round(drop, 4),
                "confidence": confidence,
                "lookback_dates": [p["_date"].isoformat() for p in lookback],
                "followup_dates": [p["_date"].isoformat() for p in following],
            }
        )

    if not candidates:
        return HarvestDetectResult(
            status="uncertain",
            confidence="low",
            evidence={**base_evidence, "reason": "no_drop_pattern"},
            window=win_meta,
        )

    # Earliest satisfying candidate is primary
    primary = candidates[0]
    return HarvestDetectResult(
        status="detected",
        harvest_date=primary["harvest_date"],
        confidence=primary["confidence"],
        scene_id=primary.get("scene_id"),
        evidence={**base_evidence, **primary},
        alternates=candidates[1:],
        window=win_meta,
    )
