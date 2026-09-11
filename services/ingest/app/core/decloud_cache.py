"""Parcel-window catalog for UnCRtainTS (scratch, not full scenes).

Stdlib catalog keyed by (land_id, date, sensor). Optional numpy arrays live
next to the JSON sidecar so a later batch task can reuse windows without a
fresh STAC scrape. Missing arrays are fine: hrefs are enough to re-window.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.decloud import decloud_lookback_days, neighbor_window

_SENSORS = frozenset({"S2", "S1"})


def cache_root() -> Path:
    explicit = (os.environ.get("DECLOUD_CACHE_DIR") or "").strip()
    if explicit:
        root = Path(explicit)
    else:
        scratch = (
            os.environ.get("OPENFARM_SCRATCH_DIR") or "/data/scratch"
        ).strip() or "/data/scratch"
        root = Path(scratch) / "decloud_windows"
    root.mkdir(parents=True, exist_ok=True)
    return root


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


def _safe_land_id(land_id: str) -> str:
    text = str(land_id).strip()
    if not text:
        raise ValueError("land_id required")
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in text)


def _parcel_dir(land_id: str) -> Path:
    path = cache_root() / _safe_land_id(land_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def meta_path(land_id: str, date_str: date | str, sensor: str) -> Path:
    sensor_u = str(sensor).upper()
    if sensor_u not in _SENSORS:
        raise ValueError(f"unsupported sensor {sensor}")
    return _parcel_dir(land_id) / f"{_iso(date_str)}_{sensor_u}.json"


def array_path(land_id: str, date_str: date | str, sensor: str) -> Path:
    return meta_path(land_id, date_str, sensor).with_suffix(".npz")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def put_window_meta(
    *,
    land_id: str,
    date_str: date | str,
    sensor: str,
    cloud_cover: float | None = None,
    stac_id: str | None = None,
    band_hrefs: dict[str, str] | None = None,
    has_array: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or merge a window catalog row. Never deletes raw lonlat products."""
    sensor_u = str(sensor).upper()
    iso = _iso(date_str)
    path = meta_path(land_id, iso, sensor_u)
    current: dict[str, Any] = {}
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
    npz = array_path(land_id, iso, sensor_u)
    payload = {
        **current,
        "land_id": str(land_id),
        "date": iso,
        "sensor": sensor_u,
    }
    if cloud_cover is not None:
        payload["cloud_cover"] = cloud_cover
    if stac_id is not None:
        payload["stac_id"] = stac_id
    if band_hrefs is not None:
        payload["band_hrefs"] = {str(k): str(v) for k, v in band_hrefs.items() if v}
    if extra:
        payload.update(extra)
    if has_array is None:
        payload["has_array"] = bool(current.get("has_array")) or npz.is_file()
    else:
        payload["has_array"] = bool(has_array)
    _write_json(path, payload)
    return payload


def get_window_meta(
    land_id: str, date_str: date | str, sensor: str
) -> dict[str, Any] | None:
    path = meta_path(land_id, date_str, sensor)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    npz = array_path(land_id, date_str, sensor)
    data["has_array"] = bool(data.get("has_array")) or npz.is_file()
    return data


def list_cached_windows(
    land_id: str,
    sensor: str,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
) -> list[dict[str, Any]]:
    """Catalog rows that can be re-windowed (hrefs) or reused (npz)."""
    folder = _parcel_dir(land_id)
    sensor_u = str(sensor).upper()
    d0 = _as_date(date_from) if date_from is not None else None
    d1 = _as_date(date_to) if date_to is not None else None
    out: list[dict[str, Any]] = []
    for path in sorted(folder.glob(f"*_{sensor_u}.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            item_date = _as_date(data.get("date") or path.name.split("_", 1)[0])
        except (TypeError, ValueError):
            continue
        if d0 is not None and item_date < d0:
            continue
        if d1 is not None and item_date > d1:
            continue
        hrefs = data.get("band_hrefs") or {}
        npz = array_path(land_id, item_date, sensor_u)
        has_array = bool(data.get("has_array")) or npz.is_file()
        if not hrefs and not has_array:
            continue
        row = dict(data)
        row["date"] = item_date
        row["has_array"] = has_array
        row["array_path"] = str(npz) if has_array else None
        out.append(row)
    out.sort(key=lambda r: r["date"])
    return out


def list_cached_s2(
    land_id: str,
    date_from: date | str | None = None,
    date_to: date | str | None = None,
) -> list[dict[str, Any]]:
    return list_cached_windows(land_id, "S2", date_from, date_to)


def usable_s2_count(
    land_id: str,
    around: date | str,
    lookback_days: int | None = None,
) -> int:
    days = decloud_lookback_days() if lookback_days is None else max(1, int(lookback_days))
    d0, d1 = neighbor_window(_as_date(around), days)
    return len(list_cached_s2(land_id, d0, d1))


def neighbor_counts_for_dates(
    land_id: str,
    dates: list[date | str],
    lookback_days: int | None = None,
) -> dict[str, int]:
    return {
        _iso(d): usable_s2_count(land_id, d, lookback_days=lookback_days) for d in dates
    }


def write_window_array(
    land_id: str,
    date_str: date | str,
    sensor: str,
    **arrays: Any,
) -> Path | None:
    """Best-effort npz write. Returns None when numpy is unavailable."""
    try:
        import numpy as np
    except ImportError:
        return None
    path = array_path(land_id, date_str, sensor)
    path.parent.mkdir(parents=True, exist_ok=True)
    packed = {k: np.asarray(v) for k, v in arrays.items() if v is not None}
    if not packed:
        return None
    tmp = path.with_suffix(path.suffix + ".tmp")
    np.savez_compressed(tmp, **packed)
    tmp.replace(path)
    put_window_meta(
        land_id=land_id,
        date_str=date_str,
        sensor=sensor,
        has_array=True,
    )
    return path


def read_window_array(
    land_id: str, date_str: date | str, sensor: str
) -> dict[str, Any] | None:
    path = array_path(land_id, date_str, sensor)
    if not path.is_file():
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        with np.load(path) as data:
            return {k: data[k] for k in data.files}
    except OSError:
        return None


__all__ = [
    "array_path",
    "cache_root",
    "get_window_meta",
    "list_cached_s2",
    "list_cached_windows",
    "meta_path",
    "neighbor_counts_for_dates",
    "put_window_meta",
    "read_window_array",
    "usable_s2_count",
    "write_window_array",
]
