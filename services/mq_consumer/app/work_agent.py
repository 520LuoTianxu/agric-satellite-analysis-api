"""HTTP claim work agent for direct canonical land-parcel tasks.

Claim payloads contain one identity only: land_id. Report and data workers
therefore receive the same value without a field/parcel translation step.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Any

import httpx
from agric_satellite_analysis_common.celery_app import (
    CPU_COMPUTE_QUEUE,
    celery_client,
    task_queue_for,
)
from agric_satellite_analysis_common.mq_schemas import TaskMessage
from agric_satellite_analysis_common.trace import (
    attach_trace_header,
    bind_trace_from_mapping,
    clear_trace_id,
    current_trace_id,
    get_or_create_trace_id,
)

logger = logging.getLogger("work_agent")

DEFAULT_TYPES = [
    "assessment_report",
    "season_growth_report",
    "land_bootstrap",
    "satellite_analysis",
    "satellite_batch",
    "agri_bridge",
    "weather_backfill",
    "soil_fetch",
    "admin_task",
]

# 管理员页面只允许触发这组预先登记的任务；下载机再次校验，避免 API
# 被错误配置或恶意 payload 利用成任意 Celery 任务执行器。
ADMIN_TASK_NAMES = frozenset(
    {
        "app.tasks.weather.schedule_daily_weather_fetch",
        "app.tasks.overview_preagg.refresh_daily_satellite",
        "app.tasks.overview_preagg.refresh_overview_stats",
    }
)

COMPLETE_ON_DISPATCH_TYPES = frozenset(
    {
        "land_bootstrap",
        "satellite_analysis",
        "satellite_batch",
        "agri_bridge",
        "weather_backfill",
        "soil_fetch",
        "admin_task",
    }
)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def work_queue_mode() -> str:
    mode = _env("WORK_QUEUE_MODE", "legacy").lower()
    return mode if mode in ("legacy", "claim", "dual") else "legacy"


def should_run_claim_agent() -> bool:
    return work_queue_mode() == "claim"


def claim_types() -> list[str]:
    raw = _env("WORK_CLAIM_TYPES")
    return (
        [t.strip() for t in raw.split(",") if t.strip()] if raw else list(DEFAULT_TYPES)
    )


def worker_name() -> str:
    """返回管理员页面展示的稳定机器名，优先读取 WORKER_NAME。"""
    return _env("WORKER_NAME") or _env("WORKER_ID") or socket.gethostname()


def worker_id() -> str:
    """兼容旧代码：租约 owner 与管理员机器名使用同一身份值。"""
    return worker_name()


def api_base_url() -> str:
    return _env("API_BASE_URL").rstrip("/")


def claim_interval_sec() -> float:
    try:
        return max(1.0, float(_env("WORK_CLAIM_INTERVAL_SEC", "4")))
    except ValueError:
        return 4.0


def queue_name() -> str:
    """返回本机主 Celery 队列名，默认汇报 CPU/编排队列。"""
    return _env("CELERY_QUEUE_NAME", CPU_COMPUTE_QUEUE) or CPU_COMPUTE_QUEUE


def queue_names() -> list[str]:
    """返回需要汇报的本机 Celery 队列，可用环境变量覆盖。"""
    raw = _env("CELERY_QUEUE_NAMES") or _env("CELERY_QUEUE_NAME")
    raw = raw or "satellite_download,cpu_compute,ingest,decloud,storage"
    names = [item.strip() for item in raw.split(",") if item.strip()]
    return list(dict.fromkeys(names)) or [CPU_COMPUTE_QUEUE]


def queue_depths() -> dict[str, int] | None:
    """读取本机 Redis 各 ready list 长度；失败时返回 None，不能伪报为 0。"""
    try:
        import redis

        client = redis.Redis.from_url(
            _env("REDIS_URL", "redis://127.0.0.1:6379/0"),
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        try:
            # Celery Redis transport 将指定 queue 的待消费消息存为同名 list。
            return {
                name: max(0, int(client.llen(name))) for name in queue_names()
            }
        finally:
            client.close()
    except Exception as exc:
        logger.warning(
            "claim_queue_probe_failed queues=%s error=%s", queue_names(), exc
        )
        return None


def pending_queue_count() -> int | None:
    """返回本机所有已配置队列的 ready 任务总数。"""
    depths = queue_depths()
    return sum(depths.values()) if depths is not None else None


def lease_seconds() -> int:
    try:
        return max(30, int(_env("WORK_LEASE_SECONDS", "600")))
    except ValueError:
        return 600


def _headers() -> dict[str, str]:
    token = _env("INTERNAL_API_TOKEN")
    if not token:
        raise RuntimeError("INTERNAL_API_TOKEN is required for claim mode")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _client() -> httpx.Client:
    base = api_base_url()
    if not base:
        raise RuntimeError("API_BASE_URL is required for claim mode")
    return httpx.Client(
        base_url=base,
        timeout=30.0,
        headers=_headers(),
        event_hooks={"request": [attach_trace_header]},
    )


def claim_batch(
    client: httpx.Client,
    *,
    types: list[str] | None = None,
    limit: int = 1,
) -> list[dict[str, Any]]:
    depths = queue_depths()
    pending = sum(depths.values()) if depths is not None else None
    body = {
        "worker_name": worker_name(),
        "worker_id": worker_id(),
        "types": types or claim_types(),
        "limit": limit,
        "lease_seconds": lease_seconds(),
        "interval_seconds": claim_interval_sec(),
        "queue_name": queue_name(),
        "pending_queue_count": pending,
        "queue_depths": depths or {},
    }
    response = client.post("/v1/internal/work/claim", json=body)
    response.raise_for_status()
    return list(response.json().get("items") or [])


def complete(client: httpx.Client, work_id: str, result: dict[str, Any]) -> None:
    response = client.post(
        f"/v1/internal/work/{work_id}/complete",
        json={"worker_id": worker_id(), "result": result},
    )
    response.raise_for_status()


def fail(
    client: httpx.Client, work_id: str, error: str, *, retry: bool = False
) -> None:
    response = client.post(
        f"/v1/internal/work/{work_id}/fail",
        json={"worker_id": worker_id(), "error": error, "retry": retry},
    )
    response.raise_for_status()


def heartbeat(client: httpx.Client, work_id: str) -> None:
    response = client.post(
        f"/v1/internal/work/{work_id}/heartbeat",
        json={"worker_id": worker_id(), "lease_seconds": lease_seconds()},
    )
    response.raise_for_status()


def progress(client: httpx.Client, work_id: str, progress_body: dict[str, Any]) -> None:
    response = client.post(
        f"/v1/internal/work/{work_id}/progress",
        json={"worker_id": worker_id(), "progress": progress_body},
    )
    response.raise_for_status()


def report_admin_task_status(
    client: httpx.Client,
    run_id: str,
    celery_task_id: str,
    status: str,
    *,
    result: Any | None = None,
    error: str | None = None,
) -> None:
    """将下载机本地 Celery 的真实状态回传给 API 管理页面。"""
    response = client.post(
        f"/v1/internal/admin/task-runs/{run_id}/status",
        json={
            "worker_name": worker_name(),
            "celery_task_id": celery_task_id,
            "status": status,
            "result": result,
            "error": error,
        },
    )
    response.raise_for_status()


def _json_safe_result(value: Any) -> Any:
    """把 Celery 返回值限制为可安全写入 API JSON 的值。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return {"repr": repr(value)}


def monitor_admin_task(
    client: httpx.Client,
    run_id: str,
    celery_task_id: str,
) -> None:
    """后台轮询本机 Redis 中的 Celery 结果，回报成功或失败终态。"""
    while True:
        try:
            async_result = celery_client.AsyncResult(celery_task_id)
            state = str(async_result.state or "PENDING").upper()
            if state == "SUCCESS":
                report_admin_task_status(
                    client,
                    run_id,
                    celery_task_id,
                    "success",
                    result=_json_safe_result(async_result.result),
                )
                return
            if state in {"FAILURE", "REVOKED"}:
                error = str(async_result.result or f"Celery task state: {state}")
                report_admin_task_status(
                    client,
                    run_id,
                    celery_task_id,
                    "failed",
                    error=error,
                )
                return
        except Exception:
            # 结果后端或 API 短暂不可用时继续重试，避免页面永久停在 running。
            logger.exception(
                "admin_task_status_monitor_retrying run_id=%s celery_task_id=%s",
                run_id,
                celery_task_id,
            )
        time.sleep(5.0)


def start_admin_task_monitor(
    client: httpx.Client,
    run_id: str,
    celery_task_id: str,
) -> None:
    """为管理员任务启动守护线程，避免阻塞短轮询 claim。"""
    thread = threading.Thread(
        target=monitor_admin_task,
        args=(client, run_id, celery_task_id),
        name=f"admin-task-monitor-{run_id[:8]}",
        daemon=True,
    )
    thread.start()


def _payload_parts(item: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Read the canonical land_id and task extras from a claimed work item."""
    payload = dict(item.get("payload_json") or {})
    land_id = payload.get("land_id")
    extras = dict(payload.get("extras") or {})
    if not extras and payload.get("job_id"):
        extras = {
            key: value
            for key, value in payload.items()
            if key not in ("land_id", "task_id", "trace_id")
        }
    return (str(land_id) if land_id else None, extras)


def _dispatch_report(
    wtype: str,
    work_id: str,
    land_id: str,
    extras: dict[str, Any],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "land_id": land_id,
        "work_item_id": work_id,
    }
    job_id = extras.get("job_id")
    if job_id:
        kwargs["job_id"] = str(job_id)

    keys = (
        ("crop_type", "crop_name_zh", "date_from", "date_to", "years")
        if wtype == "assessment_report"
        else ("start_date", "end_date", "crops", "label", "material_keys")
    )
    for key in keys:
        if extras.get(key) is not None:
            kwargs[key] = extras[key]
    if extras.get("pull_data") is not None:
        kwargs["pull_data"] = bool(extras["pull_data"])
    if extras.get("wait_celery_ids"):
        kwargs["wait_celery_ids"] = list(extras["wait_celery_ids"])

    task_name = (
        "app.tasks.assessment_report.generate_assessment_report"
        if wtype == "assessment_report"
        else "app.tasks.season_growth_report.generate_season_growth_report"
    )
    async_result = celery_client.send_task(
        task_name,
        kwargs=kwargs,
        queue=task_queue_for(task_name, requested_queue=CPU_COMPUTE_QUEUE),
    )
    return {
        "dispatched": [task_name],
        "celery_id": async_result.id,
        "job_id": str(job_id) if job_id else None,
        "land_id": land_id,
    }


def _dispatch_via_handler(
    wtype: str,
    work_id: str,
    land_id: str,
    extras: dict[str, Any],
    task_id: str | None,
) -> dict[str, Any]:
    """Reuse the MQ dispatch functions without introducing an identity mapper."""
    from app.handler import (
        _dispatch_land_bootstrap,
        _dispatch_satellite_analysis,
        _dispatch_satellite_batch,
        _dispatch_soil_fetch,
        _dispatch_weather_backfill,
    )

    task = TaskMessage(
        task_id=str(task_id or work_id),
        type=wtype,
        land_id=land_id,
        extras=dict(extras),
        trace_id=current_trace_id(),
    )
    if wtype == "agri_bridge":
        task.extras = {**task.extras, "mode": "bridge_only"}

    if wtype in ("satellite_analysis", "agri_bridge"):
        info = _dispatch_satellite_analysis(task, land_id)
    elif wtype == "satellite_batch":
        info = _dispatch_satellite_batch(task, land_id)
    elif wtype == "weather_backfill":
        info = _dispatch_weather_backfill(task, land_id)
    elif wtype == "soil_fetch":
        info = _dispatch_soil_fetch(task, land_id)
    elif wtype == "land_bootstrap":
        info = _dispatch_land_bootstrap(task, land_id)
    else:
        raise ValueError(f"unsupported work type for claim agent: {wtype}")

    result = dict(info or {})
    result["land_id"] = land_id
    result["work_item_id"] = work_id
    return result


def _dispatch_celery(item: dict[str, Any]) -> dict[str, Any]:
    wtype = item.get("type") or ""
    work_id = str(item.get("id"))
    if wtype == "admin_task":
        payload = dict(item.get("payload_json") or {})
        task_name = str(payload.get("task_name") or "")
        if task_name not in ADMIN_TASK_NAMES:
            raise ValueError(f"unsupported admin task: {task_name}")
        kwargs = dict(payload.get("kwargs") or {})
        async_result = celery_client.send_task(
            task_name,
            kwargs=kwargs,
            # 管理员任务属于 CPU/编排侧；下载机只负责按任务名做最终路由。
            queue=task_queue_for(task_name, requested_queue=CPU_COMPUTE_QUEUE),
        )
        return {
            "dispatched": [task_name],
            "celery_id": async_result.id,
            "admin_task_run_id": payload.get("admin_task_run_id"),
        }

    land_id, extras = _payload_parts(item)
    if not land_id:
        raise ValueError("work item missing land_id")

    payload = dict(item.get("payload_json") or {})
    task_id = payload.get("task_id") or extras.get("task_id")
    if wtype in ("assessment_report", "season_growth_report"):
        return _dispatch_report(wtype, work_id, land_id, extras)
    if wtype in COMPLETE_ON_DISPATCH_TYPES or wtype in DEFAULT_TYPES:
        return _dispatch_via_handler(wtype, work_id, land_id, extras, task_id)
    raise ValueError(f"unsupported work type for claim agent: {wtype}")


def process_item(client: httpx.Client, item: dict[str, Any]) -> None:
    """Dispatch Celery and complete the lease for fan-out data tasks."""
    work_id = str(item["id"])
    wtype = item.get("type") or ""
    bind_trace_from_mapping(item.get("payload_json") or {})
    get_or_create_trace_id()
    try:
        result = _dispatch_celery(item)
        if wtype == "admin_task":
            run_id = str(result.get("admin_task_run_id") or "")
            celery_task_id = str(result.get("celery_id") or "")
            if not run_id or not celery_task_id:
                raise ValueError("admin task dispatch missing run id or celery task id")
            try:
                report_admin_task_status(
                    client,
                    run_id,
                    celery_task_id,
                    "running",
                )
            except Exception:
                # 任务已经发到本机队列，回报失败时不能让 work item 被重复派发。
                logger.exception(
                    "admin_task_running_report_failed run_id=%s celery_task_id=%s",
                    run_id,
                    celery_task_id,
                )
            start_admin_task_monitor(client, run_id, celery_task_id)
        progress(
            client,
            work_id,
            {
                "stage": "dispatched",
                "celery_id": result.get("celery_id"),
                "celery_ids": result.get("celery_ids"),
                "dispatched": result.get("dispatched"),
                "admin_task_run_id": result.get("admin_task_run_id"),
                "job_id": result.get("job_id"),
                "land_id": result.get("land_id"),
            },
        )
        logger.info(
            "work_item_dispatched id=%s type=%s land_id=%s celery=%s",
            work_id,
            wtype,
            result.get("land_id"),
            result.get("celery_id") or result.get("celery_ids"),
        )
        if wtype in COMPLETE_ON_DISPATCH_TYPES:
            complete(
                client,
                work_id,
                {
                    "phase": "dispatched",
                    "type": wtype,
                    "dispatched": result.get("dispatched"),
                    "celery_ids": result.get("celery_ids")
                    or ([result["celery_id"]] if result.get("celery_id") else []),
                    "land_id": result.get("land_id"),
                    "job_id": result.get("job_id"),
                    "admin_task_run_id": result.get("admin_task_run_id"),
                },
            )
    except Exception as exc:
        logger.exception("work_item_dispatch_failed id=%s", work_id)
        try:
            fail(client, work_id, str(exc), retry=False)
        except Exception:
            logger.exception("work_item_fail_report_failed id=%s", work_id)
    finally:
        clear_trace_id()


def run_forever() -> None:
    """Short-poll claim loop; only WORK_QUEUE_MODE=claim may start it."""
    if not should_run_claim_agent():
        raise RuntimeError(
            f"claim agent refused: WORK_QUEUE_MODE={work_queue_mode()!r} "
            "(only 'claim' is allowed; dual would double-dispatch with MQ)"
        )
    logger.info(
        "work_agent starting worker_id=%s api=%s interval=%s types=%s",
        worker_id(),
        api_base_url(),
        claim_interval_sec(),
        claim_types(),
    )
    with _client() as client:
        while True:
            try:
                items = claim_batch(client, limit=1)
                if not items:
                    time.sleep(claim_interval_sec())
                else:
                    for item in items:
                        process_item(client, item)
            except Exception:
                logger.exception("work_agent_loop_error")
                time.sleep(claim_interval_sec())


__all__ = [
    "COMPLETE_ON_DISPATCH_TYPES",
    "ADMIN_TASK_NAMES",
    "DEFAULT_TYPES",
    "claim_batch",
    "claim_types",
    "pending_queue_count",
    "process_item",
    "monitor_admin_task",
    "report_admin_task_status",
    "start_admin_task_monitor",
    "queue_name",
    "queue_names",
    "queue_depths",
    "run_forever",
    "should_run_claim_agent",
    "worker_name",
    "work_queue_mode",
]
