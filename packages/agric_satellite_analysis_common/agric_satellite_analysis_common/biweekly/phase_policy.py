"""Winter-wheat S1/S2/S3 display policy aligned with the v8 report and design.md.

This is a pending agronomy-review pilot (wheat-qxt88-pilot-1). It is not
observed phenology and is not an approved mapping for other crops.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .models import ReportError

MAPPING_VERSION = "wheat-qxt88-pilot-1"
POLICY_VERSION = "biweekly-wheat-display-v8-1"
SPECIES_CODE = "2000000005002"
STAGE_MODEL = "QX/T 88-2008"

# Official QX/T 88-2008 codes only. There is no 孕穗 / booting row in the dictionary.
WHEAT_STAGES: dict[str, dict[str, Any]] = {
    "SG": {
        "name": "整地播种期",
        "status": "NOT_APPLICABLE",
        "phase_code": None,
        "phase_label": None,
        "tags": (),
    },
    "E3": {
        "name": "出苗至三叶期",
        "status": "MAPPED",
        "phase_code": "VEG",
        "phase_label": "S1",
        "tags": (),
    },
    "PT": {
        "name": "冬前分蘖期",
        "status": "MAPPED",
        "phase_code": "VEG",
        "phase_label": "S1",
        "tags": (),
    },
    "OW": {
        "name": "越冬期",
        "status": "MAPPED",
        "phase_code": "VEG",
        "phase_label": "S1",
        "tags": ("DORMANCY",),
    },
    "GR": {
        "name": "返青期",
        "status": "MAPPED",
        "phase_code": "VEG",
        "phase_label": "S1",
        "tags": (),
    },
    "RS": {
        "name": "起身期",
        "status": "MAPPED",
        "phase_code": "TRANS",
        "phase_label": "S2",
        "tags": (),
    },
    "JT": {
        "name": "拔节期",
        "status": "MAPPED",
        "phase_code": "TRANS",
        "phase_label": "S2",
        "tags": (),
    },
    "HA": {
        "name": "抽穗至开花期",
        "status": "MAPPED",
        "phase_code": "REPRO",
        "phase_label": "S3",
        "tags": (),
    },
    "GF": {
        "name": "灌浆期",
        "status": "MAPPED",
        "phase_code": "REPRO",
        "phase_label": "S3",
        "tags": (),
    },
    "MT": {
        "name": "成熟期",
        "status": "MAPPED",
        "phase_code": "REPRO",
        "phase_label": "S3",
        "tags": (),
    },
}

# Friendly aliases used by older offline fixtures. "booting" is intentionally absent.
WHEAT_ALIASES = {
    "sowing": "SG",
    "seedling": "E3",
    "tillering": "PT",
    "winter": "OW",
    "green": "GR",
    "green_up": "GR",
    "rising": "RS",
    "jointing": "JT",
    "heading": "HA",
    "flowering": "HA",
    "filling": "GF",
    "maturity": "MT",
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


def resolve_wheat_stage(key: str) -> dict[str, Any]:
    code = canonical_stage_code(key)
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
