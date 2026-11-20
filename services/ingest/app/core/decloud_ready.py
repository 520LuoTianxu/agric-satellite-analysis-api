"""Local pending-decloud queue, S1 coverage, and cache-only readiness.

Scratch-only (no Postgres). Safe for HTTP-only download machines that share
``/data/scratch/decloud_windows``. File locks plus atomic JSON replace keep
concurrent satellite / decloud workers from clobbering one land's state.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from app.core.decloud import (
    batch_neighbors_ready,
    decloud_input_t,
    decloud_lookback_days,
    decloud_use_sar,
    neighbor_window,
    pick_temporal_scenes,
)
from app.core.decloud_cache import (
    array_path,
    cache_root,
    get_window_meta,
    list_cached_windows,
    meta_path,
    read_window_array,
)

S1ReadyKind = Literal["cache", "legacy_s2_key", "confirmed_zero", "missing"]

_PENDING_NAME = "pending_decloud.json"
_COVERAGE_NAME = "s1_coverage.json"


def s1_match_days() -> int:
    raw = os.environ.get("S1_MATCH_DAYS")
    if raw is None or not str(raw).strip():
        return 6
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 6


def _iso(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()[:10]


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_land_id(land_id: str) -> str:
    text = str(land_id).strip()
    if not text:
        raise ValueError("land_id required")
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)


def _parcel_dir(land_id: str) -> Path:
    path = cache_root() / _safe_land_id(land_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def pending_path(land_id: str) -> Path:
    return _parcel_dir(land_id) / _PENDING_NAME


def coverage_path(land_id: str) -> Path:
    return _parcel_dir(land_id) / _COVERAGE_NAME


@contextmanager
def _locked_json(path: Path, default: dict[str, Any]) -> Iterator[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data: dict[str, Any] = dict(default)
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    loaded = None
                if isinstance(loaded, dict):
                    data = loaded
            yield data
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(path)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _meta_has_array(land_id: str, item_date: date, sensor: str, row: dict[str, Any]) -> bool:
    if row.get("has_array"):
        return True
    return array_path(land_id, item_date, sensor).is_file()


def list_array_dates(
    land_id: str,
    sensor: str,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
) -> list[dict[str, Any]]:
    """Catalog rows that already have a numpy array, without loading it."""
    rows = []
    for row in list_cached_windows(land_id, sensor, date_from, date_to):
        if not row.get("has_array"):
            continue
        rows.append(row)
    return rows


def peek_window_grid_key(
    land_id: str, item_date: date, sensor: str, row: dict[str, Any] | None = None
) -> str | None:
    """Read grid fingerprint from JSON meta, falling back to npz header only."""
    meta = row or get_window_meta(land_id, item_date, sensor) or {}
    stored = meta.get("grid_key") or meta.get("window_grid_key")
    if stored:
        return str(stored)
    path = array_path(land_id, item_date, sensor)
    if not path.is_file():
        return None
    try:
        import numpy as np

        with np.load(path, mmap_mode="r", allow_pickle=False) as data:
            if "__grid_key__" in data.files:
                return str(data["__grid_key__"].item())
    except (OSError, ValueError, KeyError):
        return None
    return None


def _array_grid_matches(
    land_id: str,
    item_date: date,
    sensor: str,
    expected_grid_key: str | None,
    row: dict[str, Any] | None = None,
) -> bool:
    if not _meta_has_array(land_id, item_date, sensor, row or {}):
        return False
    if not expected_grid_key:
        return True
    stored = peek_window_grid_key(land_id, item_date, sensor, row)
    if stored is None:
        # Legacy file without a stored key: accept for readiness; load-time
        # expected_grid_key still rejects a real mismatch.
        return True
    return str(stored) == str(expected_grid_key)


def find_s1_for_s2_date(
    land_id: str,
    s2_date: date | str,
    *,
    expected_grid_key: str | None = None,
    match_days: int | None = None,
) -> dict[str, Any]:
    """Resolve SAR for one S2 date.

    Preference order:
    1. ``<s1_acq_date>_S1`` within ±match_days whose grid matches (closest first)
    2. legacy ``<s2_date>_S1`` written by older decloud buffering (S2-dated key)
    3. coverage proving the ±match_days window was searched and is empty
    """
    target = _as_date(s2_date)
    window = match_days if match_days is not None else s1_match_days()
    d0 = target - timedelta(days=window)
    d1 = target + timedelta(days=window)
    candidates = list_array_dates(str(land_id), "S1", d0, d1)
    candidates.sort(
        key=lambda row: (abs((_as_date(row["date"]) - target).days), _as_date(row["date"]))
    )
    for row in candidates:
        item_date = _as_date(row["date"])
        if not _array_grid_matches(
            str(land_id), item_date, "S1", expected_grid_key, row
        ):
            continue
        kind: S1ReadyKind = "legacy_s2_key" if item_date == target else "cache"
        # A file named as the S2 date might still be a true S1 pass on that day.
        # ``legacy_s2_key`` is only a hint when the date equals the S2 date and
        # we cannot tell; callers treat both kinds as "has array".
        return {
            "kind": kind,
            "ready": True,
            "s1_date": item_date,
            "delta_days": abs((item_date - target).days),
        }
    if s1_window_confirmed_empty(str(land_id), target, match_days=window):
        return {"kind": "confirmed_zero", "ready": True, "s1_date": None, "delta_days": None}
    return {
        "kind": "missing",
        "ready": False,
        "s1_date": None,
        "delta_days": None,
        "reason": "waiting_s1",
    }


def record_s1_coverage(
    land_id: str,
    date_from: date | str,
    date_to: date | str,
    *,
    failed_dates: list[date | str] | None = None,
    source: str = "satellite_batch",
    job_id: str | None = None,
) -> dict[str, Any]:
    """Register a completed S1 search window for one parcel (no download failures hidden)."""
    start, end = _as_date(date_from), _as_date(date_to)
    if start > end:
        start, end = end, start
    failed = sorted({_iso(d) for d in (failed_dates or [])})
    with _locked_json(
        coverage_path(land_id), {"land_id": str(land_id), "intervals": []}
    ) as payload:
        payload["land_id"] = str(land_id)
        intervals = list(payload.get("intervals") or [])
        intervals.append(
            {
                "date_from": _iso(start),
                "date_to": _iso(end),
                "failed_dates": failed,
                "source": source,
                "job_id": job_id,
                "updated_at": _now_iso(),
            }
        )
        payload["intervals"] = _merge_coverage_intervals(intervals)
        payload["updated_at"] = _now_iso()
        return dict(payload)


def _merge_coverage_intervals(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for raw in intervals:
        try:
            start, end = _as_date(raw["date_from"]), _as_date(raw["date_to"])
        except (KeyError, TypeError, ValueError):
            continue
        if start > end:
            start, end = end, start
        failed = []
        for item in raw.get("failed_dates") or []:
            try:
                failed.append(_iso(item))
            except (TypeError, ValueError):
                continue
        parsed.append(
            {
                "date_from": start,
                "date_to": end,
                "failed_dates": sorted(set(failed)),
                "source": raw.get("source"),
                "job_id": raw.get("job_id"),
                "updated_at": raw.get("updated_at"),
            }
        )
    parsed.sort(key=lambda row: (row["date_from"], row["date_to"]))
    merged: list[dict[str, Any]] = []
    for row in parsed:
        if not merged or row["date_from"] > merged[-1]["date_to"] + timedelta(days=1):
            merged.append(
                {
                    "date_from": row["date_from"],
                    "date_to": row["date_to"],
                    "failed_dates": list(row["failed_dates"]),
                    "source": row.get("source"),
                    "job_id": row.get("job_id"),
                    "updated_at": row.get("updated_at"),
                }
            )
            continue
        last = merged[-1]
        last["date_to"] = max(last["date_to"], row["date_to"])
        last["failed_dates"] = sorted(set(last["failed_dates"]) | set(row["failed_dates"]))
        last["updated_at"] = row.get("updated_at") or last.get("updated_at")
    out = []
    for row in merged:
        out.append(
            {
                "date_from": _iso(row["date_from"]),
                "date_to": _iso(row["date_to"]),
                "failed_dates": row["failed_dates"],
                "source": row.get("source"),
                "job_id": row.get("job_id"),
                "updated_at": row.get("updated_at"),
            }
        )
    return out


def s1_window_confirmed_empty(
    land_id: str, s2_date: date | str, *, match_days: int | None = None
) -> bool:
    """True when [target±match_days] was fully searched and has no failed S1 dates."""
    target = _as_date(s2_date)
    window = match_days if match_days is not None else s1_match_days()
    need0 = target - timedelta(days=window)
    need1 = target + timedelta(days=window)
    path = coverage_path(land_id)
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    intervals = payload.get("intervals") or []
    if not intervals:
        return False
    covered: list[tuple[date, date]] = []
    failed: set[date] = set()
    for raw in intervals:
        try:
            start, end = _as_date(raw["date_from"]), _as_date(raw["date_to"])
        except (KeyError, TypeError, ValueError):
            continue
        covered.append((start, end))
        for item in raw.get("failed_dates") or []:
            try:
                failed.add(_as_date(item))
            except (TypeError, ValueError):
                continue
    covered.sort()
    cursor = need0
    for start, end in covered:
        if end < cursor:
            continue
        if start > cursor:
            return False
        cursor = end + timedelta(days=1)
        if cursor > need1:
            break
    if cursor <= need1:
        return False
    return not any(need0 <= day <= need1 for day in failed)


def upsert_pending_targets(
    land_id: str,
    targets: list[dict[str, Any]],
    *,
    date_from: str,
    date_to: str,
    mq_task_id: str | None = None,
    season_months: list[int] | None = None,
) -> list[str]:
    """Insert or refresh pending cloudy dates. Returns date strings now pending."""
    added: list[str] = []
    with _locked_json(
        pending_path(land_id), {"land_id": str(land_id), "targets": {}}
    ) as payload:
        payload["land_id"] = str(land_id)
        bucket = dict(payload.get("targets") or {})
        for raw in targets:
            if not isinstance(raw, dict) or not raw.get("date"):
                continue
            try:
                iso = _iso(raw["date"])
            except (TypeError, ValueError):
                continue
            current = dict(bucket.get(iso) or {})
            status = str(current.get("status") or "pending")
            if status in {"dispatched", "done"}:
                # Keep the existing in-flight / finished record; refresh metadata only
                # when a new schedule arrives after a failure.
                continue
            item = {
                **current,
                "date": iso,
                "status": "pending",
                "date_from": str(date_from)[:10],
                "date_to": str(date_to)[:10],
                "mq_task_id": mq_task_id,
                "season_months": list(season_months) if season_months is not None else current.get("season_months"),
                "raw_scene_id": raw.get("raw_scene_id") or raw.get("scene_id") or raw.get("stac_id") or current.get("raw_scene_id"),
                "stac_cloud": raw.get("stac_cloud", raw.get("cloud_cover", current.get("stac_cloud"))),
                "parcel_cloud": raw.get("parcel_cloud", raw.get("parcel_cloud_cover_pct", current.get("parcel_cloud"))),
                "updated_at": _now_iso(),
            }
            if status == "failed":
                item["retried_from"] = "failed"
            bucket[iso] = item
            added.append(iso)
        payload["targets"] = bucket
        payload["updated_at"] = _now_iso()
    return added


def mark_pending_status(
    land_id: str, date_str: date | str, status: str, **extra: Any
) -> dict[str, Any] | None:
    iso = _iso(date_str)
    with _locked_json(
        pending_path(land_id), {"land_id": str(land_id), "targets": {}}
    ) as payload:
        bucket = dict(payload.get("targets") or {})
        current = bucket.get(iso)
        if not isinstance(current, dict):
            return None
        current = {**current, "status": status, "updated_at": _now_iso(), **extra}
        bucket[iso] = current
        payload["targets"] = bucket
        payload["updated_at"] = _now_iso()
        return dict(current)


def iter_pending_targets(land_id: str) -> list[dict[str, Any]]:
    path = pending_path(land_id)
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for item in (payload.get("targets") or {}).values():
        if isinstance(item, dict) and item.get("date"):
            out.append(item)
    out.sort(key=lambda row: str(row.get("date")))
    return out


def assess_target_ready(
    land_id: str,
    target: date | str,
    *,
    expected_grid_key: str | None,
    use_sar: bool | None = None,
    input_t: int | None = None,
    lookback_days: int | None = None,
) -> dict[str, Any]:
    """Decide whether one cloudy date can run from local cache alone.

    Only inspects JSON sidecars plus at most a few S1 npz grid keys. Does not
    stack the ±45 day archive into memory.
    """
    target_d = _as_date(target)
    pad = decloud_lookback_days() if lookback_days is None else max(1, int(lookback_days))
    need = decloud_input_t() if input_t is None else max(1, int(input_t))
    d0, d1 = neighbor_window(target_d, pad)
    s2_rows = list_array_dates(str(land_id), "S2", d0, d1)
    if expected_grid_key:
        s2_rows = [
            row
            for row in s2_rows
            if _array_grid_matches(
                str(land_id), _as_date(row["date"]), "S2", expected_grid_key, row
            )
        ]
    if not any(_as_date(row["date"]) == target_d for row in s2_rows):
        return {
            "ready": False,
            "reason": "waiting_s2_target",
            "s2_usable": len(s2_rows),
            "input_t": need,
            "picked": [],
            "s1": [],
        }
    if not batch_neighbors_ready(len(s2_rows), need):
        return {
            "ready": False,
            "reason": "waiting_s2_neighbors",
            "s2_usable": len(s2_rows),
            "input_t": need,
            "picked": [],
            "s1": [],
        }
    picked = pick_temporal_scenes(s2_rows, target_d, need)
    if not picked:
        return {
            "ready": False,
            "reason": "waiting_s2_neighbors",
            "s2_usable": len(s2_rows),
            "input_t": need,
            "picked": [],
            "s1": [],
        }
    picked_dates = [_as_date(row["date"]) for row in picked]
    sar = decloud_use_sar() if use_sar is None else bool(use_sar)
    s1_info: list[dict[str, Any]] = []
    if sar:
        for s2_date in picked_dates:
            match = find_s1_for_s2_date(
                str(land_id), s2_date, expected_grid_key=expected_grid_key
            )
            s1_info.append({"s2_date": _iso(s2_date), **match})
            if not match.get("ready"):
                return {
                    "ready": False,
                    "reason": "waiting_s1",
                    "s2_usable": len(s2_rows),
                    "input_t": need,
                    "picked": [_iso(d) for d in picked_dates],
                    "s1": s1_info,
                }
    return {
        "ready": True,
        "reason": "ready",
        "s2_usable": len(s2_rows),
        "input_t": need,
        "picked": [_iso(d) for d in picked_dates],
        "s1": s1_info,
    }


def pop_ready_pending_targets(
    land_id: str,
    *,
    expected_grid_key: str | None,
    redispatch_after_sec: int = 7200,
) -> list[dict[str, Any]]:
    """Atomically mark newly-ready (or stale dispatched) targets as dispatched."""
    ready: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    with _locked_json(
        pending_path(land_id), {"land_id": str(land_id), "targets": {}}
    ) as payload:
        bucket = dict(payload.get("targets") or {})
        for iso, item in list(bucket.items()):
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "pending")
            if status == "done":
                continue
            if status == "dispatched":
                stamped = str(item.get("dispatched_at") or "")
                stale = False
                if stamped:
                    try:
                        dispatched_at = datetime.fromisoformat(
                            stamped.replace("Z", "+00:00")
                        )
                        stale = (now - dispatched_at).total_seconds() >= redispatch_after_sec
                    except ValueError:
                        stale = True
                else:
                    stale = True
                if not stale:
                    continue
            assessment = assess_target_ready(
                str(land_id), iso, expected_grid_key=expected_grid_key
            )
            item["last_assessment"] = {
                "ready": assessment["ready"],
                "reason": assessment["reason"],
                "picked": assessment.get("picked"),
                "at": _now_iso(),
            }
            if not assessment["ready"]:
                item["status"] = "pending"
                item["held_reason"] = assessment["reason"]
                bucket[iso] = item
                continue
            item["status"] = "dispatched"
            item["dispatched_at"] = _now_iso()
            item["held_reason"] = None
            item["updated_at"] = _now_iso()
            bucket[iso] = item
            ready.append(dict(item))
        payload["targets"] = bucket
        payload["updated_at"] = _now_iso()
    return ready


__all__ = [
    "assess_target_ready",
    "coverage_path",
    "find_s1_for_s2_date",
    "iter_pending_targets",
    "list_array_dates",
    "mark_pending_status",
    "pending_path",
    "pop_ready_pending_targets",
    "record_s1_coverage",
    "s1_match_days",
    "s1_window_confirmed_empty",
    "upsert_pending_targets",
]
