"""Winter-wheat S1/S2/S3 display policy aligned with the v8 report and design.md.

This is a pending agronomy-review pilot (wheat-smart-pilot-1). It is not
observed phenology and is not an approved mapping for other crops.

Official keys are the Smart wheat stage names used by
agriculture_standard_job.job_name and crop_remote_phase_mapping.stage_name.
Observed agriculture_plant_task.task_name values and older fixture codes are
aliases onto those official names only. 起身期 aliases to S2 (via 拔节孕穗期),
never S1. There is no official 孕穗 or booting row.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .models import ReportError

MAPPING_VERSION = "wheat-smart-pilot-1"
POLICY_VERSION = "biweekly-wheat-display-v8-1"
CROP_ID = 4
CROP_NAME = "小麦"
STAGE_MODEL = "smart_agric wheat job_name / plant_task.task_name"

# Official Smart dictionary / mapping-table names only. No invented 孕穗 row.
WHEAT_STAGES: dict[str, dict[str, Any]] = {
    "播种前准备": {
        "name": "播种前准备",
        "status": "NOT_APPLICABLE",
        "phase_code": None,
        "phase_label": None,
        "tags": (),
    },
    "播种期": {
        "name": "播种期",
        "status": "NOT_APPLICABLE",
        "phase_code": None,
        "phase_label": None,
        "tags": (),
    },
    "出苗期": {
        "name": "出苗期",
        "status": "MAPPED",
        "phase_code": "VEG",
        "phase_label": "S1",
        "tags": (),
    },
    "分蘖期": {
        "name": "分蘖期",
        "status": "MAPPED",
        "phase_code": "VEG",
        "phase_label": "S1",
        "tags": (),
    },
    "越冬期": {
        "name": "越冬期",
        "status": "MAPPED",
        "phase_code": "VEG",
        "phase_label": "S1",
        "tags": ("DORMANCY",),
    },
    "返青期": {
        "name": "返青期",
        "status": "MAPPED",
        "phase_code": "VEG",
        "phase_label": "S1",
        "tags": (),
    },
    "拔节孕穗期": {
        "name": "拔节孕穗期",
        "status": "MAPPED",
        "phase_code": "TRANS",
        "phase_label": "S2",
        "tags": (),
    },
    "抽穗期": {
        "name": "抽穗期",
        "status": "MAPPED",
        "phase_code": "REPRO",
        "phase_label": "S3",
        "tags": (),
    },
    "灌浆期": {
        "name": "灌浆期",
        "status": "MAPPED",
        "phase_code": "REPRO",
        "phase_label": "S3",
        "tags": (),
    },
    "成熟期": {
        "name": "成熟期",
        "status": "MAPPED",
        "phase_code": "REPRO",
        "phase_label": "S3",
        "tags": (),
    },
}

# Read-time aliases onto official names. These are not extra mapping-table rows.
# Plant-task names below were observed on live Smart wheat batches (crop_id=4).
WHEAT_ALIASES = {
    # Observed agriculture_plant_task.task_name values
    "整地播种期": "播种期",
    "出苗至三叶期": "出苗期",
    "冬前分蘖期": "分蘖期",
    "起身期": "拔节孕穗期",
    "拔节期": "拔节孕穗期",
    "抽穗至开花期": "抽穗期",
    # Older offline fixtures / explicit 15933 reference intervals
    "SG": "播种期",
    "E3": "出苗期",
    "PT": "分蘖期",
    "OW": "越冬期",
    "GR": "返青期",
    "RS": "拔节孕穗期",
    "JT": "拔节孕穗期",
    "HA": "抽穗期",
    "GF": "灌浆期",
    "MT": "成熟期",
    "sowing": "播种期",
    "seedling": "出苗期",
    "tillering": "分蘖期",
    "winter": "越冬期",
    "green": "返青期",
    "green_up": "返青期",
    "rising": "拔节孕穗期",
    "jointing": "拔节孕穗期",
    "heading": "抽穗期",
    "flowering": "抽穗期",
    "filling": "灌浆期",
    "maturity": "成熟期",
}

PLAIN_NAMES = {
    "ndvi": "绿度",
    "evi": "增强绿度",
    "ndre": "叶片营养",
    "ndmi": "植被水分",
}

PHASE_METRICS = {
    "S1": {"primary": ("ndvi",), "supporting": ("evi", "ndmi")},
    "S2": {"primary": ("ndre", "evi"), "supporting": ("ndvi", "ndmi")},
    "S3": {"primary": ("ndre", "ndmi"), "supporting": ("ndvi", "evi")},
}

FORBIDDEN_REPORT_METRICS = frozenset({"fvc", "lai", "psri", "savi", "osavi"})
PHASE_LABELS = {"S1": "营养生长", "S2": "营养/生殖并进", "S3": "生殖生长"}


def canonical_stage_code(key: str) -> str:
    if key in WHEAT_STAGES:
        return key
    if key in WHEAT_ALIASES:
        return WHEAT_ALIASES[key]
    raise ReportError("STAGE_MAPPING_UNAVAILABLE")


def _blank_to_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def apply_mapping_rows(rows: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """Index crop_remote_phase_mapping rows by official or source stage name."""
    indexed: dict[str, dict[str, Any]] = {}
    for raw in rows or []:
        name = str(raw.get("stage_name") or raw.get("stage_code") or "").strip()
        if not name:
            continue
        status = str(raw.get("mapping_status") or raw.get("status") or "UNMAPPED")
        tags = raw.get("context_tags") or raw.get("tags") or ()
        if isinstance(tags, str):
            tags = tuple(item for item in tags.split(",") if item)
        indexed[name] = {
            "stage_code": name,
            "name": name,
            "status": status,
            "phase_code": _blank_to_none(raw.get("phase_code")),
            "phase_label": _blank_to_none(raw.get("phase_label") or raw.get("phase")),
            "tags": list(tags),
            "mapping_version": raw.get("mapping_version") or MAPPING_VERSION,
            "review_status": raw.get("review_status") or "PENDING_AGRONOMY_REVIEW",
        }
    return indexed


def resolve_wheat_stage(key: str, mapping_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    name = str(key or "").strip()
    if not name:
        raise ReportError("STAGE_MAPPING_UNAVAILABLE")
    indexed = apply_mapping_rows(mapping_rows)
    if name in indexed:
        return indexed[name]
    official = WHEAT_ALIASES.get(name, name if name in WHEAT_STAGES else None)
    if official and official in indexed:
        row = dict(indexed[official])
        row["source_stage_name"] = name
        return row
    code = canonical_stage_code(name)
    row = WHEAT_STAGES[code]
    return {
        "stage_code": code,
        "name": row["name"],
        "status": row["status"],
        "phase_code": row["phase_code"],
        "phase_label": row["phase_label"],
        "tags": list(row["tags"]),
        "mapping_version": MAPPING_VERSION,
        "review_status": "PENDING_AGRONOMY_REVIEW",
        "source_stage_name": name if name != code else None,
    }


def wheat_snapshot_phases() -> dict[str, str | None]:
    """stage_key → S1/S2/S3 or None (NOT_APPLICABLE). Includes aliases."""
    out: dict[str, str | None] = {
        code: row["phase_label"] for code, row in WHEAT_STAGES.items()
    }
    for alias, code in WHEAT_ALIASES.items():
        out[alias] = WHEAT_STAGES[code]["phase_label"]
    return out


def metrics_for_phase(phase_label: str | None) -> dict[str, tuple[str, ...]]:
    if phase_label not in PHASE_METRICS:
        return {"primary": (), "supporting": ()}
    return dict(PHASE_METRICS[phase_label])


def _as_day(value: str) -> date:
    return date.fromisoformat(value)


def interval_covers(start: str, end_exclusive: str, day: str) -> bool:
    return _as_day(start) <= _as_day(day) < _as_day(end_exclusive)


def display_phase(
    *,
    planned_intervals: list[dict],
    observation_dates: list[str],
    window_start: str,
    end_exclusive: str,
) -> dict[str, Any]:
    """Follow in-period valid observations; disclose planned transitions.

    planned_intervals items: {stage_code, start, end_exclusive}
    Dates are Asia/Shanghai calendar days. end is exclusive.
    """
    window_days = []
    cursor = _as_day(window_start)
    last = _as_day(end_exclusive)
    while cursor < last:
        window_days.append(cursor.isoformat())
        cursor += timedelta(days=1)

    def stage_on(day: str) -> dict | None:
        hits = [item for item in planned_intervals if interval_covers(item["start"], item["end_exclusive"], day)]
        if len(hits) > 1:
            raise ReportError("DATE_CONFLICT")
        return hits[0] if hits else None

    planned = []
    for day in window_days:
        hit = stage_on(day)
        if not hit:
            planned.append({"date": day, "stage_code": None, "phase_label": None})
            continue
        resolved = resolve_wheat_stage(hit["stage_code"])
        planned.append(
            {
                "date": day,
                "stage_code": resolved["stage_code"],
                "phase_label": resolved["phase_label"],
                "status": resolved["status"],
                "tags": resolved["tags"],
            }
        )

    obs = []
    for day in observation_dates:
        if not (window_start <= day < end_exclusive):
            continue
        hit = stage_on(day)
        resolved = resolve_wheat_stage(hit["stage_code"]) if hit else None
        obs.append(
            {
                "date": day,
                "stage_code": resolved["stage_code"] if resolved else None,
                "phase_label": resolved["phase_label"] if resolved else None,
                "tags": resolved["tags"] if resolved else [],
            }
        )

    obs_phases = [row["phase_label"] for row in obs if row["phase_label"]]
    unique = list(dict.fromkeys(obs_phases))
    if unique:
        display = unique[0] if len(unique) == 1 else unique
        source = "in_period_observations"
    else:
        planned_phases = [row["phase_label"] for row in planned if row["phase_label"]]
        unique_plan = list(dict.fromkeys(planned_phases))
        display = unique_plan[0] if len(unique_plan) == 1 else unique_plan or None
        source = "planned_window_only_no_optics"

    metrics = metrics_for_phase(display if isinstance(display, str) else None)
    if isinstance(display, list):
        # Observations straddle two stages: do not average into one phase.
        metrics = {"primary": (), "supporting": ()}

    transitions = []
    for previous, current in zip(planned, planned[1:]):
        if (previous["stage_code"], previous["phase_label"]) != (
            current["stage_code"],
            current["phase_label"],
        ):
            transitions.append(
                {
                    "on": current["date"],
                    "from_stage": previous["stage_code"],
                    "from_phase": previous["phase_label"],
                    "to_stage": current["stage_code"],
                    "to_phase": current["phase_label"],
                }
            )

    next_start = end_exclusive
    next_end = ( _as_day(end_exclusive) + timedelta(days=14) ).isoformat()
    next_hit = stage_on(next_start) if planned_intervals else None
    # stage_on only sees intervals; next week may be covered by an interval that starts at end_exclusive
    if next_hit is None:
        for item in planned_intervals:
            if interval_covers(item["start"], item["end_exclusive"], next_start):
                next_hit = item
                break
    next_resolved = resolve_wheat_stage(next_hit["stage_code"]) if next_hit else None

    return {
        "policy_version": POLICY_VERSION,
        "mapping_version": MAPPING_VERSION,
        "display_phase": display if isinstance(display, str) else None,
        "display_phases_if_split": display if isinstance(display, list) else None,
        "display_source": source,
        "phase_name": PHASE_LABELS.get(display) if isinstance(display, str) else None,
        "primary_metrics": list(metrics["primary"]),
        "supporting_metrics": list(metrics["supporting"]),
        "plain_names": dict(PLAIN_NAMES),
        "observation_stages": obs,
        "planned_days": planned,
        "transitions": transitions,
        "next_week": {
            "start": next_start,
            "end_exclusive": next_end,
            "stage_code": next_resolved["stage_code"] if next_resolved else None,
            "phase_label": next_resolved["phase_label"] if next_resolved else None,
            "primary_metrics": list(
                metrics_for_phase(
                    next_resolved["phase_label"] if next_resolved else None
                )["primary"]
            ),
        },
    }


def compare_to_prior(
    current: list[dict[str, Any]],
    prior: list[dict[str, Any]],
    metrics: list[str],
) -> dict[str, Any]:
    """Prove 好转 only against the previous RS period, same in-field means.

    Each item: {date, <metric>_avg or metric keys}. Missing metrics are omitted,
    never invented. A single current observation cannot be called 持续改善.
    """

    def values(rows: list[dict], metric: str) -> list[tuple[str, float]]:
        out = []
        for row in rows:
            key = f"{metric}_avg" if f"{metric}_avg" in row else metric
            value = row.get(key)
            if value is None:
                continue
            out.append((row["date"], float(value)))
        return out

    by_metric = {}
    higher = 0
    compared = 0
    for metric in metrics:
        if metric in FORBIDDEN_REPORT_METRICS:
            continue
        now = values(current, metric)
        then = values(prior, metric)
        if not now or not then:
            by_metric[metric] = {"status": "incomparable_missing_values"}
            continue
        now_mean = sum(v for _, v in now) / len(now)
        then_mean = sum(v for _, v in then) / len(then)
        delta = now_mean - then_mean
        compared += 1
        if delta > 0:
            higher += 1
        by_metric[metric] = {
            "status": "compared",
            "plain_name": PLAIN_NAMES.get(metric, metric),
            "current_dates": [d for d, _ in now],
            "prior_dates": [d for d, _ in then],
            "current_mean": now_mean,
            "prior_mean": then_mean,
            "delta": delta,
            "higher": delta > 0,
        }
    sustained = len(current) >= 2
    return {
        "compared_metrics": compared,
        "higher_count": higher,
        "all_compared_higher": compared > 0 and higher == compared,
        "can_claim_sustained_improvement": sustained and compared > 0 and higher == compared,
        "metrics": by_metric,
    }
