"""Versioned local cache for small, already-reprojected satellite windows.

This cache is independent from the UnCRtainTS parcel catalog. It stores the
shared scene window used by ``process_satellite_batch`` so retries and reruns do
not re-open remote COGs. Keys include source paths, target grid and resampling
mode; signed query strings are deliberately excluded because SAS tokens rotate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import numpy as np

logger = logging.getLogger("openfarm.ingest.band_window_cache")

CACHE_SCHEMA_VERSION = 2
_META_KEY = "__window_cache_meta__"
_prune_lock = threading.Lock()
_last_prune_monotonic = 0.0


def window_cache_enabled() -> bool:
    """独立开关；关闭时不创建目录，也不进行任何磁盘读写。"""
    return str(os.environ.get("BAND_WINDOW_CACHE", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def window_cache_root() -> Path:
    explicit = str(os.environ.get("BAND_WINDOW_CACHE_DIR", "")).strip()
    if explicit:
        root = Path(explicit)
    else:
        scratch = str(
            os.environ.get("OPENFARM_SCRATCH_DIR", "/data/scratch")
        ).strip() or "/data/scratch"
        root = Path(scratch) / "band_windows" / "v2"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _source_identity(href: str) -> str:
    """保留稳定的源路径、移除会轮换且可能含敏感信息的查询参数。"""
    text = str(href)
    if text.startswith("/vsis3/"):
        return text
    parsed = urlsplit(text)
    if parsed.scheme:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return text


def _resampling_name(value: object) -> str:
    name = getattr(value, "name", None)
    return str(name or value)


def _grid_values(transform: object) -> list[float]:
    try:
        return [round(float(value), 14) for value in transform]  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("target transform is not iterable") from exc


def _cache_meta(
    *,
    scene_id: str,
    sensor: str,
    target_shape: tuple[int, int],
    target_transform: object,
    band_hrefs: Mapping[str, str],
    resampling_by_band: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    sources = {
        str(key): _source_identity(value)
        for key, value in sorted(band_hrefs.items())
        if value
    }
    resampling = {
        key: _resampling_name((resampling_by_band or {}).get(key, "bilinear"))
        for key in sources
    }
    identity = {
        "schema": CACHE_SCHEMA_VERSION,
        "scene_id": str(scene_id),
        "sensor": str(sensor).upper(),
        "crs": "EPSG:4326",
        "shape": [int(target_shape[0]), int(target_shape[1])],
        "transform": _grid_values(target_transform),
        "sources": sources,
        "resampling": resampling,
    }
    encoded = json.dumps(
        identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {**identity, "fingerprint": hashlib.sha256(encoded).hexdigest()}


def _cache_path(meta: Mapping[str, Any]) -> Path:
    digest = str(meta["fingerprint"])
    sensor = str(meta["sensor"]).lower()
    folder = window_cache_root() / sensor / digest[:2]
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{digest}.npz"


def read_scene_window(
    *,
    scene_id: str,
    sensor: str,
    target_shape: tuple[int, int],
    target_transform: object,
    band_hrefs: Mapping[str, str],
    resampling_by_band: Mapping[str, object] | None = None,
) -> dict[str, np.ndarray] | None:
    """读取完全匹配源与目标网格的 v2 窗口；损坏缓存自动失效。"""
    if not window_cache_enabled() or not band_hrefs:
        return None
    meta = _cache_meta(
        scene_id=scene_id,
        sensor=sensor,
        target_shape=target_shape,
        target_transform=target_transform,
        band_hrefs=band_hrefs,
        resampling_by_band=resampling_by_band,
    )
    path = _cache_path(meta)
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            if _META_KEY not in data.files:
                raise ValueError("cache metadata missing")
            stored = json.loads(str(data[_META_KEY].item()))
            if stored.get("fingerprint") != meta["fingerprint"]:
                raise ValueError("cache fingerprint mismatch")
            arrays = {
                band: np.asarray(data[band])
                for band in meta["sources"]
                if band in data.files
            }
        if set(arrays) != set(meta["sources"]):
            raise ValueError("cache band set incomplete")
        if any(tuple(array.shape) != tuple(target_shape) for array in arrays.values()):
            raise ValueError("cache array shape mismatch")
        # mtime 即 LRU 访问时间；命中后触碰文件，淘汰时优先保留热点窗口。
        try:
            os.utime(path, None)
        except OSError:
            # 并发 LRU 恰好删除文件时，本次已加载数组仍然有效，无需回退远端读取。
            pass
        return arrays
    except Exception as exc:
        logger.warning("band_window_cache_invalid path=%s error=%s", path, exc)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def write_scene_window(
    *,
    scene_id: str,
    sensor: str,
    target_shape: tuple[int, int],
    target_transform: object,
    band_hrefs: Mapping[str, str],
    arrays: Mapping[str, np.ndarray],
    resampling_by_band: Mapping[str, object] | None = None,
) -> Path | None:
    """原子写入一个共享景窗口；缓存失败不应影响主卫星产品流程。"""
    if not window_cache_enabled() or not band_hrefs:
        return None
    meta = _cache_meta(
        scene_id=scene_id,
        sensor=sensor,
        target_shape=target_shape,
        target_transform=target_transform,
        band_hrefs=band_hrefs,
        resampling_by_band=resampling_by_band,
    )
    expected = set(meta["sources"])
    packed = {
        band: np.asarray(arrays[band]) for band in sorted(expected) if band in arrays
    }
    if set(packed) != expected:
        missing = sorted(expected - set(packed))
        raise ValueError(f"window cache arrays incomplete: {missing}")
    if any(tuple(array.shape) != tuple(target_shape) for array in packed.values()):
        raise ValueError("window cache array shape mismatch")

    # 写入前先回收旧窗口，为低剩余空间场景预留落盘空间；写后再次调用会受节流保护。
    prune_window_cache()
    path = _cache_path(meta)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    payload = {
        **packed,
        _META_KEY: np.asarray(
            json.dumps(meta, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        ),
    }
    try:
        # 使用文件句柄可阻止 numpy 自动追加 .npz，确保 replace 的临时路径准确。
        with tmp.open("wb") as handle:
            np.savez_compressed(handle, **payload)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    prune_window_cache()
    return path


def _float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def prune_window_cache(*, force: bool = False) -> dict[str, int]:
    """按文件 mtime 执行 LRU 淘汰，并同时守住容量与磁盘剩余空间。"""
    global _last_prune_monotonic
    if not window_cache_enabled():
        return {"files": 0, "bytes": 0, "removed": 0, "removed_bytes": 0}
    interval = _float_env("BAND_WINDOW_CACHE_PRUNE_INTERVAL_SEC", 60.0)
    now = time.monotonic()
    with _prune_lock:
        if not force and now - _last_prune_monotonic < interval:
            return {"files": 0, "bytes": 0, "removed": 0, "removed_bytes": 0}
        _last_prune_monotonic = now
        root = window_cache_root()
        files: list[tuple[float, int, Path]] = []
        for path in root.rglob("*.npz"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((stat.st_mtime, stat.st_size, path))

        total = sum(size for _, size, _ in files)
        max_bytes = int(
            _float_env("BAND_WINDOW_CACHE_MAX_GB", 20.0) * 1024 * 1024 * 1024
        )
        min_free = int(
            _float_env("BAND_WINDOW_CACHE_MIN_FREE_GB", 2.0) * 1024 * 1024 * 1024
        )
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            free = min_free
        target_bytes = int(max_bytes * 0.9) if max_bytes else total
        if (not max_bytes or total <= max_bytes) and free >= min_free:
            return {
                "files": len(files),
                "bytes": total,
                "removed": 0,
                "removed_bytes": 0,
            }

        removed = 0
        removed_bytes = 0
        for _, size, path in sorted(files, key=lambda row: row[0]):
            enough_capacity = not max_bytes or total - removed_bytes <= target_bytes
            enough_free = free + removed_bytes >= min_free
            if enough_capacity and enough_free:
                break
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            removed += 1
            removed_bytes += size
        if removed:
            logger.info(
                "band_window_cache_pruned files=%s bytes=%s", removed, removed_bytes
            )
        return {
            "files": len(files),
            "bytes": total,
            "removed": removed,
            "removed_bytes": removed_bytes,
        }


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "prune_window_cache",
    "read_scene_window",
    "window_cache_enabled",
    "window_cache_root",
    "write_scene_window",
]
