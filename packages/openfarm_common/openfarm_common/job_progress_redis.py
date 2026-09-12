"""Redis hot-path job progress (HINCRBY/HSET) with graceful degrade.

Scene ThreadPool workers update a per-job Redis Hash instead of contending on
``jobs.progress_json`` Postgres refresh+commit. Callers periodically
``flush_to_job`` (ingest) or ``merge_progress`` (API) to surface a compact
summary.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger()

KEY_TMPL = "openfarm:job:{job_id}:progress"
# Keep hot-path keys around long enough for UI polling after a large job.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

_client_lock = threading.Lock()
_client: Any = None
_client_failed = False
_warned_unavailable = False
_retry_after = 0.0
_RETRY_SECONDS = 30.0


def progress_key(job_id: str | Any) -> str:
    return KEY_TMPL.format(job_id=str(job_id))


def _redis_url() -> str:
    from openfarm_common.settings import settings

    return settings.redis_url


def _get_client():
    """Lazy Redis client; None when Redis is unavailable (degraded mode)."""
    global _client, _client_failed, _warned_unavailable, _retry_after
    if _client_failed and time.monotonic() < _retry_after:
        return None
    if _client is not None:
        return _client
    with _client_lock:
        if _client_failed and time.monotonic() < _retry_after:
            return None
        if _client is not None:
            return _client
        try:
            import redis

            client = redis.Redis.from_url(
                _redis_url(),
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
            client.ping()
            _client = client
            _client_failed = False
            _warned_unavailable = False
            return _client
        except Exception as exc:
            _client_failed = True
            # 连接失败只降级一个冷却窗口，避免一次抖动导致永久失去实时进度。
            _retry_after = time.monotonic() + _RETRY_SECONDS
            if not _warned_unavailable:
                _warned_unavailable = True
                logger.warning(
                    "job_progress_redis_unavailable",
                    error=str(exc),
                )
            return None


def _connection_failed(client, event: str, exc: Exception) -> None:
    """同一轮连接故障只记录一次，并阻止场景线程在 Redis 故障期间持续重试。"""
    global _client, _client_failed, _retry_after, _warned_unavailable
    with _client_lock:
        if _client is not client:
            return
        _client = None
        _client_failed = True
        _warned_unavailable = True
        _retry_after = time.monotonic() + _RETRY_SECONDS
    logger.warning(event, error=str(exc))


def reset_client_for_tests() -> None:
    """Clear cached client / failure flag (unit tests only)."""
    global _client, _client_failed, _warned_unavailable, _retry_after
    with _client_lock:
        _client = None
        _client_failed = False
        _warned_unavailable = False
        _retry_after = 0.0


def _touch_ttl(client, key: str, ttl: int = DEFAULT_TTL_SECONDS) -> None:
    try:
        client.expire(key, ttl)
    except Exception:
        pass


def set_total(
    job_id: str | Any,
    total: int,
    *,
    workers: int | None = None,
    current_step: str = "process_scenes",
) -> bool:
    """Initialize / overwrite total (+ optional workers) for a job."""
    client = _get_client()
    if client is None:
        return False
    key = progress_key(job_id)
    try:
        mapping: dict[str, str] = {
            "total": str(int(total)),
            "done": "0",
            "failed": "0",
            "current_step": current_step,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if workers is not None:
            mapping["workers"] = str(int(workers))
        client.hset(key, mapping=mapping)
        _touch_ttl(client, key)
        return True
    except Exception as exc:
        _connection_failed(client, "job_progress_redis_set_total_failed", exc)
        return False


def mark_scene_progress(
    job_id: str | Any,
    step: str,
    *,
    scene: int | None = None,
    total_scenes: int | None = None,
    scene_id: str | None = None,
    **extra: Any,
) -> bool:
    """Record the current per-scene step in Redis (no Postgres)."""
    client = _get_client()
    if client is None:
        return False
    key = progress_key(job_id)
    try:
        mapping: dict[str, str] = {
            "current_step": step,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if scene is not None:
            mapping["current_scene"] = str(int(scene))
        if total_scenes is not None:
            mapping["total"] = str(int(total_scenes))
        if scene_id is not None:
            mapping["scene_id"] = str(scene_id)
        for k, v in extra.items():
            if v is None:
                continue
            mapping[str(k)] = str(v)
        client.hset(key, mapping=mapping)
        _touch_ttl(client, key)
        return True
    except Exception as exc:
        _connection_failed(client, "job_progress_redis_mark_failed", exc)
        return False


def incr_done(job_id: str | Any, *, failed: bool = False) -> int | None:
    """Atomically increment done (and optionally failed). Returns new done count."""
    client = _get_client()
    if client is None:
        return None
    key = progress_key(job_id)
    try:
        pipe = client.pipeline()
        pipe.hincrby(key, "done", 1)
        if failed:
            pipe.hincrby(key, "failed", 1)
        pipe.hset(key, "updated_at", datetime.now(timezone.utc).isoformat())
        pipe.expire(key, DEFAULT_TTL_SECONDS)
        results = pipe.execute()
        return int(results[0])
    except Exception as exc:
        _connection_failed(client, "job_progress_redis_incr_failed", exc)
        return None


def read_progress(job_id: str | Any) -> dict[str, Any] | None:
    """Return Redis hash as a typed dict, or None if missing / Redis down."""
    client = _get_client()
    if client is None:
        return None
    key = progress_key(job_id)
    try:
        raw = client.hgetall(key)
        if not raw:
            return None
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if k in ("total", "done", "failed", "workers", "current_scene"):
                try:
                    out[k] = int(v)
                except (TypeError, ValueError):
                    out[k] = v
            else:
                out[k] = v
        return out
    except Exception as exc:
        _connection_failed(client, "job_progress_redis_read_failed", exc)
        return None


def clear_progress(job_id: str | Any) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        client.delete(progress_key(job_id))
        return True
    except Exception as exc:
        _connection_failed(client, "job_progress_redis_clear_failed", exc)
        return False


def apply_redis_to_progress(
    progress: dict[str, Any] | None,
    snap: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge a Redis snapshot into a progress_json-compatible dict (pure)."""
    out = dict(progress or {})
    # Redis 快照可能晚于最终落库，不能把已结束任务的阶段回退为下载中。
    if not snap or out.get("current_step") in {"complete", "completed", "failed", "cancelled"}:
        return out

    total = snap.get("total")
    done = snap.get("done")
    failed = snap.get("failed")
    workers = snap.get("workers")
    if total is not None:
        out["total_scenes"] = total
    if done is not None:
        out["scenes_done"] = done
    if failed is not None:
        out["scenes_failed"] = failed
    if workers is not None:
        out["workers"] = workers
        out["scene_workers"] = workers

    step = snap.get("current_step")
    if step:
        out["current_step"] = step

    steps = dict(out.get("steps") or {})
    entry = dict(steps.get("process_scenes") or {})
    entry["status"] = entry.get("status") or "running"
    if total is not None:
        entry["total_scenes"] = total
    if done is not None:
        entry["scenes_done"] = done
        entry["scene"] = done  # rough cursor for UIs that read ``scene``
    if workers is not None:
        entry["workers"] = workers
    if snap.get("current_scene") is not None:
        entry["scene"] = snap["current_scene"]
    if snap.get("scene_id"):
        entry["scene_id"] = snap["scene_id"]
    if snap.get("updated_at"):
        entry["updated_at"] = snap["updated_at"]
    steps["process_scenes"] = entry
    out["steps"] = steps
    out["progress_source"] = "redis"
    return out


def merge_progress_for_api(
    job_id: str | Any,
    progress_json: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Prefer live Redis counters when present; else return progress_json."""
    snap = read_progress(job_id)
    if not snap:
        return progress_json
    return apply_redis_to_progress(progress_json, snap)
