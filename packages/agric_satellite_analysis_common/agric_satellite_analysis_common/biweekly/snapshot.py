"""Validate compact adapter output, freeze facts, and map planned crop periods.

No BTU/DB/weather adapter is implied here. Numerical quality limits below are
versioned OFFLINE trial policy, not agronomic calibration or publication rules.
"""

from __future__ import annotations

import math
import re
from datetime import date

from .models import ReportError, Snapshot, canonical, digest

POLICY_VERSION = "biweekly-crop-policy-offline-draft-v1"
MAPPING_VERSION = "planned-three-phase-draft-v1"
STAGES = {
    "winter_wheat": {
        "seedling": "S1",
        "tillering": "S1",
        "winter": "S1",
        "green_up": "S1",
        "rising": "S1",
        "jointing": "S2",
        "booting": "S2",
        "heading": "S3",
        "flowering": "S3",
        "filling": "S3",
        "maturity": "S3",
    },
    "maize": {
        "seedling": "S1",
        "jointing": "S1",
        "small_trumpet": "S2",
        "large_trumpet": "S2",
        "tasseling": "S3",
        "silking": "S3",
        "filling": "S3",
        "maturity": "S3",
    },
}
METRICS = {
    "ndvi": ("NDVI均值", "1", -1, 1),
    "ndre": ("NDRE均值", "1", -1, 1),
    "ndmi": ("NDMI均值", "1", -1, 1),
    "fvc": ("FVC", "1", 0, 1),
    "lai": ("LAI", "m2/m2", 0, 20),
    "rain_mm": ("本期降水", "mm", 0, 100000),
    "temp_mean_c": ("本期平均气温", "C", -100, 100),
}


def _require(condition, code="SOURCE_INVALID"):
    if not condition:
        raise ReportError(code)


def _text(value, limit=128):
    _require(
        isinstance(value, str)
        and 0 < len(value) <= limit
        and not any(ord(c) < 32 for c in value)
    )
    return value


def _day(value):
    try:
        _require(
            isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
        )
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ReportError("DATE_INVALID") from None


def validate_report_window(start, end):
    """拒绝非法/非双周范围，避免调用源适配器后才验证。"""
    start_day, end_day = _day(start), _day(end)
    _require((end_day - start_day).days == 14, "WINDOW_NOT_BIWEEKLY")
    return start_day, end_day


def _number(value, low, high):
    _require(
        type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
        "VALUE_INVALID",
    )
    return value


def build_snapshot(raw: dict) -> Snapshot:
    _require(isinstance(raw, dict))
    try:
        # 仅接受紧凑契约；不把像元数组、任意附件或未知字段塞给模型。
        allowed = {
            "tenant_id",
            "field_id",
            "season_id",
            "crop",
            "data_kind",
            "source_revision",
            "window",
            "plan_version",
            "asset_ids",
            "stages",
            "sampling",
            "evidence",
            "geometry_fingerprint",
            "processing_versions",
        }
        _require(set(raw) == allowed)
        scope = {
            k: _text(raw[k])
            for k in (
                "tenant_id",
                "field_id",
                "season_id",
                "source_revision",
                "plan_version",
                "geometry_fingerprint",
            )
        }
        versions = raw["processing_versions"]
        _require(
            isinstance(versions, dict)
            and set(versions) == {"mask", "grid", "aggregation", "calibration"}
        )
        versions = {k: _text(v) for k, v in versions.items()}
        crop = raw["crop"]
        _require(crop in STAGES, "CROP_POLICY_UNAVAILABLE")
        _require(raw["data_kind"] in {"mock", "real"})
        start, end = validate_report_window(
            raw["window"]["start"], raw["window"]["end"]
        )
        assets = raw["asset_ids"]
        _require(isinstance(assets, list) and len(assets) <= 128)
        assets = sorted({_text(x, 256) for x in assets})
        periods = []
        _require(isinstance(raw["stages"], list) and len(raw["stages"]) <= 64)
        for item in raw["stages"]:
            _require(set(item) == {"period_id", "stage_key", "name", "start", "end"})
            key = item["stage_key"]
            _require(key in STAGES[crop], "STAGE_MAPPING_UNAVAILABLE")
            a, b = _day(item["start"]), _day(item["end"])
            _require(a < b, "STAGE_WINDOW_INVALID")
            periods.append(
                {
                    "period_id": _text(item["period_id"]),
                    "name": _text(item["name"]),
                    "stage_key": key,
                    "phase": STAGES[crop][key],
                    "start": str(a),
                    "end": str(b),
                }
            )
        periods.sort(key=lambda x: (x["start"], x["period_id"]))
        _require(
            len({p["period_id"] for p in periods}) == len(periods), "DUPLICATE_PERIOD"
        )
        for previous, current in zip(periods, periods[1:]):
            _require(previous["end"] <= current["start"], "OVERLAPPING_STAGES")
        covered = []
        primary = None
        for period in periods:
            a, b = max(start, _day(period["start"])), min(end, _day(period["end"]))
            if a < b:
                covered.append(
                    {
                        **period,
                        "covered_start": str(a),
                        "covered_end": str(b),
                        "days": (b - a).days,
                    }
                )
            if _day(period["start"]) <= start < _day(period["end"]):
                primary = period["period_id"]
        sampling = raw["sampling"]
        _require(set(sampling) == {"valid_days", "valid_fraction", "max_gap_days"})
        _require(
            type(sampling["valid_days"]) is int and 0 <= sampling["valid_days"] <= 14
        )
        _require(
            type(sampling["max_gap_days"]) is int
            and 0 <= sampling["max_gap_days"] <= 14
        )
        _number(sampling["valid_fraction"], 0, 1)
        rs_ok = (
            sampling["valid_days"] >= 2
            and sampling["valid_fraction"] >= 0.5
            and sampling["max_gap_days"] <= 10
        )
        flags = {
            "FVC_UNCALIBRATED",
            "LAI_MODEL_UNAVAILABLE",
            "DISASTER_EVIDENCE_INCOMPLETE",
            "PLANNED_PHENOLOGY_NOT_OBSERVED",
            "OFFLINE_DRAFT_POLICY",
        }
        if not periods:
            flags.add("STAGE_PLAN_MISSING")
        if sum(p["days"] for p in covered) != 14 or primary is None:
            flags.add("STAGE_COVERAGE_INCOMPLETE")
        if not rs_ok:
            flags.add("LOW_RS_COVERAGE")
        evidence, facts, ids, seen_metrics = [], {}, set(), set()
        _require(isinstance(raw["evidence"], list) and len(raw["evidence"]) <= 32)
        for item in raw["evidence"]:
            _require(
                set(item)
                == {
                    "id",
                    "metric",
                    "value",
                    "unit",
                    "source_id",
                    "status",
                    "method_version",
                    "start",
                    "end",
                }
            )
            eid, metric = _text(item["id"]), item["metric"]
            _require(bool(re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", eid)))
            _require(
                eid not in ids and metric not in seen_metrics, "DUPLICATE_EVIDENCE"
            )
            _require(metric in METRICS, "METRIC_POLICY_UNAVAILABLE")
            ids.add(eid)
            seen_metrics.add(metric)
            label, unit, low, high = METRICS[metric]
            _require(item["unit"] == unit, "UNIT_MISMATCH")
            _require(
                _day(item["start"]) == start and _day(item["end"]) == end,
                "EVIDENCE_WINDOW_MISMATCH",
            )
            _require(item["status"] in {"observed", "estimated", "missing"})
            _text(item["method_version"])
            missing = item["status"] == "missing"
            if missing:
                _require(item["value"] is None, "MISSING_MUST_BE_NULL")
            else:
                _number(item["value"], low, high)
                _require(item["source_id"] in assets, "SOURCE_ASSET_UNTRACKED")
            usable = (
                not missing
                and item["status"] == "observed"
                and metric not in {"fvc", "lai"}
                and (not metric.startswith("nd") or rs_ok)
            )
            availability = (
                "available"
                if usable
                else (
                    "not_validated"
                    if metric in {"fvc", "lai"}
                    else "missing_or_low_quality"
                )
            )
            evidence.append({**item, "label": label, "availability": availability})
            if usable:
                facts[eid] = {
                    "metric": metric,
                    "label": label,
                    "value": item["value"],
                    "unit": unit,
                    "source_id": item["source_id"],
                }
            else:
                flags.add("UNAVAILABLE_" + metric.upper())
        required = {"ndvi", "rain_mm"}
        if any(p["phase"] in {"S2", "S3"} for p in covered):
            required.add("ndre")
        if any(p["phase"] == "S3" for p in covered):
            required.add("ndmi")
        available = {f["metric"] for f in facts.values()}
        flags.update("MISSING_" + m.upper() for m in required - available)
        data = {
            **scope,
            "schema_version": "biweekly-snapshot-v1",
            "crop": crop,
            "data_kind": raw["data_kind"],
            "timezone": "Asia/Shanghai",
            "window": {"start": str(start), "end": str(end)},
            "asset_ids": assets,
            "source_payload_sha256": digest(canonical(raw)),
            "policy_version": POLICY_VERSION,
            "mapping_version": MAPPING_VERSION,
            "phenology_basis": "planned_btu_not_observed",
            "primary_period_id": primary,
            "covered_periods": covered,
            "sampling": dict(sampling),
            "processing_versions": versions,
            "evidence": sorted(evidence, key=lambda x: x["id"]),
            "facts": facts,
            "quality_flags": sorted(flags),
        }
        encoded = canonical(data)
        _require(len(encoded.encode()) <= 64000, "SNAPSHOT_TOO_LARGE")
        return Snapshot(encoded, digest(encoded))
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ReportError("SOURCE_INVALID") from None
