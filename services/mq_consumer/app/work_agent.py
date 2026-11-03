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
    SATELLITE_DOWNLOAD_QUEUE,
    celery_client,
    task_queue_for,
)
from agric_satellite_analysis_common.mq_schemas import TaskMessage
from agric_satellite_analysis_common.task_priority import (
    BACKGROUND_TASK_PRIORITY,
    celery_priority_for,
    normalize_task_priority,
)
from agric_satellite_analysis_common.trace import (
    attach_trace_header,
    bind_trace_from_mapping,
    clear_trace_id,
    current_trace_id,
    get_or_create_trace_id,
)
from agric_satellite_analysis_common.weather_daily_limit import (
    WEATHER_WORK_TYPE,
    weather_daily_limit_reached,
)
from agric_satellite_analysis_common.internal_api import internal_api_path as _ipath

logger = logging.getLogger("work_agent")

# Base data first so claim prefers downloads over report waiters.
BASE_DATA_TYPES = [
    "satellite_batch",
    "land_bootstrap",
    "satellite_analysis",
    "agri_bridge",
    WEATHER_WORK_TYPE,
    "soil_fetch",
    "admin_task",
    "project_task",
]
REPORT_TYPES = [
    "assessment_report",
    "season_growth_report",
]
BASE_DATA_TYPE_SET = frozenset(BASE_DATA_TYPES)
REPORT_TYPE_SET = frozenset(REPORT_TYPES)
DEFAULT_TYPES = list(BASE_DATA_TYPES) + list(REPORT_TYPES)

# 管理员页面只允许触发这组预先登记的任务；下载机再次校验，避免 API
# 被错误配置或恶意 payload 利用成任意 Celery 任务执行器。
ADMIN_TASK_NAMES = frozenset(
    {
        "app.tasks.weather.schedule_daily_weather_fetch",
        "app.tasks.overview_preagg.refresh_daily_satellite",
        "app.tasks.overview_preagg.refresh_overview_stats",
    }
)

# 项目级任务（COG 补数、收获分析）白名单；与 API services/project_tasks.py 保持一致。
PROJECT_TASK_NAMES = frozenset(
    {
        "app.tasks.project_cogs.build_project_cogs",
        "app.tasks.project_harvest.run_project_harvest",
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
        "project_task",
    }
)

# 派发到下载机本地Celery失败时最多尝试三次；API侧会在第三次失败时收敛关联Job。
MAX_WORK_ITEM_ATTEMPTS = 3


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def work_queue_mode() -> str:
    mode = _env("WORK_QUEUE_MODE", "legacy").lower()
    return mode if mode in ("legacy", "claim", "dual") else "legacy"


def should_run_claim_agent() -> bool:
    return work_queue_mode() == "claim"


def claim_types() -> list[str]:
    raw = _env("WORK_CLAIM_TYPES")
    configured = (
        [t.strip() for t in raw.split(",") if t.strip()] if raw else list(DEFAULT_TYPES)
    )
    if not weather_daily_limit_reached():
        return configured
    # 天气 API 达到当天配额后只暂停天气类型，其他下载和计算任务继续领取。
    return [task_type for task_type in configured if task_type != WEATHER_WORK_TYPE]



def reports_after_base_enabled() -> bool:
    """WORK_CLAIM_REPORTS_AFTER_BASE (default on): only claim reports when no base-data item is pending."""
    raw = _env("WORK_CLAIM_REPORTS_AFTER_BASE", "1").lower()
    return raw not in {"0", "false", "no", "off"}


def split_claim_types(types: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Split configured claim types into (base_or_other, reports)."""
    configured = list(claim_types() if types is None else types)
    base: list[str] = []
    reports: list[str] = []
    for task_type in configured:
        if task_type in REPORT_TYPE_SET:
            reports.append(task_type)
        else:
            base.append(task_type)
    return base, reports


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


DEFAULT_MAX_LOCAL_BACKLOG = 100


def max_local_backlog() -> int:
    """WORK_CLAIM_MAX_LOCAL_BACKLOG：本机积压（就绪+在途）达到该值即暂停领取；0 表示不限制。"""
    raw = _env("WORK_CLAIM_MAX_LOCAL_BACKLOG", str(DEFAULT_MAX_LOCAL_BACKLOG))
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_LOCAL_BACKLOG


def backlog_queue_names() -> list[str]:
    """WORK_CLAIM_BACKLOG_QUEUES：领取上限只统计这些队列（默认仅 satellite_download）。

    与 CELERY_QUEUE_NAMES（上报队列深度用）分开配置，decloud 等下游队列不再阻塞领取。
    """
    raw = _env("WORK_CLAIM_BACKLOG_QUEUES", "satellite_download")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    return names or ["satellite_download"]


def _unacked_queue(raw: Any) -> str | None:
    """解析 Celery Redis transport 的 unacked 值 [payload, exchange, routing_key]，返回 routing_key。"""
    try:
        value = json.loads(raw)
        if isinstance(value, list) and len(value) >= 3:
            routing_key = value[2]
            if isinstance(routing_key, bytes):
                routing_key = routing_key.decode()
            if isinstance(routing_key, str) and routing_key:
                return routing_key
            payload = value[0] if isinstance(value[0], dict) else {}
            delivery = (payload.get("properties") or {}).get("delivery_info") or {}
            return delivery.get("routing_key") or None
    except Exception:
        return None
    return None


def local_backlog() -> int | None:
    """领取上限用的本机积压：WORK_CLAIM_BACKLOG_QUEUES 中各队列就绪消息（含优先级子队列）
    + 路由到这些队列的 unacked（已投递未确认，含 ETA/在途）。无法解析的 unacked 条目跳过。

    读取失败或超过探测时限返回 None（调用方按不限流处理，保持原有领取行为）。
    使用 HSCAN 而非 HVALS，并设墙钟上限，避免超大 unacked 把 claim 环堵死。
    """
    try:
        import redis

        try:
            budget = max(0.5, float(_env("WORK_CLAIM_BACKLOG_PROBE_SEC", "3")))
        except ValueError:
            budget = 3.0
        deadline = time.monotonic() + budget
        client = redis.Redis.from_url(
            _env("REDIS_URL", "redis://127.0.0.1:6379/0"),
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        try:
            wanted = set(backlog_queue_names())
            total = 0
            for name in wanted:
                if time.monotonic() > deadline:
                    raise TimeoutError("backlog probe budget exceeded during llen")
                keys = {name}
                # Celery Redis 优先级队列以 "<queue>\x06\x16<n>" 形式另存为 list
                for key in client.scan_iter(match=f"{name}\x06\x16*", count=100):
                    if time.monotonic() > deadline:
                        raise TimeoutError("backlog probe budget exceeded during scan")
                    keys.add(key.decode() if isinstance(key, bytes) else key)
                total += sum(int(client.llen(k)) for k in keys)
            cursor: int | bytes = 0
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("backlog probe budget exceeded during hscan")
                cursor, pairs = client.hscan("unacked", cursor=cursor, count=200)
                for _field, raw in pairs.items():
                    if _unacked_queue(raw) in wanted:
                        total += 1
                if cursor == 0 or cursor == b"0":
                    break
            return total
        finally:
            client.close()
    except Exception as exc:
        logger.warning("claim_backlog_probe_failed error=%s", exc)
        return None


def claim_paused_for_backlog() -> tuple[bool, int | None, int]:
    cap = max_local_backlog()
    if cap <= 0:
        return False, None, cap
    backlog = local_backlog()
    return backlog is not None and backlog >= cap, backlog, cap


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


def api_http_timeout() -> httpx.Timeout:
    """Claim 环出站 HTTP 硬超时，避免 progress/complete/claim 无限挂起。

    connect 单独收紧（默认 5s），降低网关 TLS 半开时长时间卡在握手的概率；
    pool 同样短，避免从坏连接池里干等。
    """
    try:
        total = max(5.0, float(_env("WORK_CLAIM_HTTP_TIMEOUT_SEC", "20")))
    except ValueError:
        total = 20.0
    try:
        connect = max(1.0, float(_env("WORK_CLAIM_HTTP_CONNECT_TIMEOUT_SEC", "5")))
    except ValueError:
        connect = 5.0
    connect = min(connect, total)
    pool = min(connect, 5.0)
    return httpx.Timeout(total, connect=connect, read=total, write=total, pool=pool)


def claim_http_retries() -> int:
    """瞬时 TLS/超时/协议错误时的额外重试次数（不含首次）。"""
    try:
        return max(0, int(_env("WORK_CLAIM_HTTP_RETRIES", "3")))
    except ValueError:
        return 3


def claim_http_retry_backoff_sec() -> float:
    try:
        return max(0.05, float(_env("WORK_CLAIM_HTTP_RETRY_BACKOFF_SEC", "0.5")))
    except ValueError:
        return 0.5


def claim_watchdog_sec() -> float:
    """无成功 claim 环进度超过该秒数则重建 httpx 客户端；0 关闭看门狗。"""
    try:
        raw = float(_env("WORK_CLAIM_WATCHDOG_SEC", "90"))
    except ValueError:
        return 90.0
    if raw <= 0:
        return 0.0
    return max(15.0, raw)


_TRANSIENT_STATUS = frozenset({502, 503, 504})


def is_transient_http_error(exc: BaseException) -> bool:
    """网关 TLS/超时/协议抖动视为可重试。"""
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.LocalProtocolError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response is not None and exc.response.status_code in _TRANSIENT_STATUS
    return False


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int | None = None,
    backoff: float | None = None,
    sleep: Any = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """对 Internal API 出站请求做短退避重试；成功或非瞬时错误立即返回/抛出。"""
    extra = claim_http_retries() if retries is None else max(0, int(retries))
    delay = (
        claim_http_retry_backoff_sec() if backoff is None else max(0.0, float(backoff))
    )
    attempts = extra + 1
    op = getattr(client, method.lower())
    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            response = op(url, **kwargs)
            if (
                getattr(response, "status_code", None) in _TRANSIENT_STATUS
                and i + 1 < attempts
            ):
                logger.warning(
                    "work_claim_http_retry status=%s attempt=%s/%s method=%s url=%s",
                    response.status_code,
                    i + 1,
                    attempts,
                    method.upper(),
                    url,
                )
                sleep(delay * (2**i))
                continue
            return response
        except Exception as exc:
            last_exc = exc
            if not is_transient_http_error(exc) or i + 1 >= attempts:
                raise
            logger.warning(
                "work_claim_http_retry error=%s attempt=%s/%s method=%s url=%s",
                exc,
                i + 1,
                attempts,
                method.upper(),
                url,
            )
            sleep(delay * (2**i))
    assert last_exc is not None
    raise last_exc


def _client() -> httpx.Client:
    base = api_base_url()
    if not base:
        raise RuntimeError("API_BASE_URL is required for claim mode")
    return httpx.Client(
        base_url=base,
        timeout=api_http_timeout(),
        headers=_headers(),
        event_hooks={"request": [attach_trace_header]},
        # 网关半开时不要无限复用坏连接；短保活 + 小池便于看门狗重建。
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
    )


def claim_batch(
    client: httpx.Client,
    *,
    types: list[str] | None = None,
    limit: int = 1,
) -> list[dict[str, Any]]:
    requested_types = list(claim_types() if types is None else types)
    if types is not None and weather_daily_limit_reached():
        requested_types = [
            task_type
            for task_type in requested_types
            if task_type != WEATHER_WORK_TYPE
        ]
    if not requested_types:
        return []
    depths = queue_depths()
    pending = sum(depths.values()) if depths is not None else None
    body = {
        "worker_name": worker_name(),
        "worker_id": worker_id(),
        "types": requested_types,
        "limit": limit,
        "lease_seconds": lease_seconds(),
        "interval_seconds": claim_interval_sec(),
        "queue_name": queue_name(),
        "pending_queue_count": pending,
        "queue_depths": depths or {},
    }
    response = request_with_retry(
        client, "POST", _ipath("/internal/work/claim"), json=body
    )
    response.raise_for_status()
    return list(response.json().get("items") or [])


# 旧版 API 没有 /worker-heartbeat 时的兜底：用一个不存在的任务类型调用 claim，
# 服务端按类型过滤后不会返回任何任务，但仍会刷新下载机心跳。
HEARTBEAT_ONLY_TYPE = "__heartbeat_only__"
_heartbeat_endpoint_missing = False


def paused_heartbeat_interval_sec() -> float:
    try:
        return max(1.0, float(_env("WORK_CLAIM_PAUSED_HEARTBEAT_SEC", "30")))
    except ValueError:
        return 30.0


def worker_heartbeat(client: httpx.Client) -> str:
    """暂停领取期间向生产上报存活（不领取任务）。返回所用方式：endpoint / claim_fallback。"""
    global _heartbeat_endpoint_missing
    depths = queue_depths()
    body = {
        "worker_name": worker_name(),
        "worker_id": worker_id(),
        "types": list(claim_types()),
        "limit": 1,
        "interval_seconds": claim_interval_sec(),
        "queue_name": queue_name(),
        "pending_queue_count": sum(depths.values()) if depths is not None else None,
        "queue_depths": depths or {},
    }
    if not _heartbeat_endpoint_missing:
        response = request_with_retry(
            client, "POST", _ipath("/internal/work/worker-heartbeat"), json=body
        )
        if response.status_code not in (404, 405):
            response.raise_for_status()
            return "endpoint"
        _heartbeat_endpoint_missing = True
        logger.info("worker_heartbeat endpoint missing; using zero-match claim")
    body["types"] = [HEARTBEAT_ONLY_TYPE]
    response = request_with_retry(
        client, "POST", _ipath("/internal/work/claim"), json=body
    )
    response.raise_for_status()
    items = list(response.json().get("items") or [])
    if items:  # 理论上不可能；绝不静默吞掉任务
        logger.error("heartbeat claim unexpectedly returned %d items", len(items))
        raise RuntimeError("heartbeat-only claim returned work items")
    return "claim_fallback"


def complete(client: httpx.Client, work_id: str, result: dict[str, Any]) -> None:
    response = request_with_retry(
        client,
        "POST",
        _ipath(f"/internal/work/{work_id}/complete"),
        json={"worker_id": worker_id(), "result": result},
    )
    response.raise_for_status()


def fail(
    client: httpx.Client, work_id: str, error: str, *, retry: bool = False
) -> None:
    response = request_with_retry(
        client,
        "POST",
        _ipath(f"/internal/work/{work_id}/fail"),
        json={"worker_id": worker_id(), "error": error, "retry": retry},
    )
    response.raise_for_status()


def heartbeat(client: httpx.Client, work_id: str) -> None:
    response = request_with_retry(
        client,
        "POST",
        _ipath(f"/internal/work/{work_id}/heartbeat"),
        json={"worker_id": worker_id(), "lease_seconds": lease_seconds()},
    )
    response.raise_for_status()


def progress(client: httpx.Client, work_id: str, progress_body: dict[str, Any]) -> None:
    response = request_with_retry(
        client,
        "POST",
        _ipath(f"/internal/work/{work_id}/progress"),
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
    response = request_with_retry(
        client,
        "POST",
        _ipath(f"/internal/admin/task-runs/{run_id}/status"),
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
    priority: int | None = None,
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
    options: dict[str, Any] = {
        "kwargs": kwargs,
        "queue": task_queue_for(task_name, requested_queue=CPU_COMPUTE_QUEUE),
    }
    if priority is not None and priority > BACKGROUND_TASK_PRIORITY:
        options["priority"] = celery_priority_for(priority)
    async_result = celery_client.send_task(task_name, **options)
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
    priority: int | None = None,
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
        priority=normalize_task_priority(priority),
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
    priority = normalize_task_priority(item.get("priority"))
    if wtype == "admin_task":
        payload = dict(item.get("payload_json") or {})
        task_name = str(payload.get("task_name") or "")
        if task_name not in ADMIN_TASK_NAMES:
            raise ValueError(f"unsupported admin task: {task_name}")
        kwargs = dict(payload.get("kwargs") or {})
        options: dict[str, Any] = {
            "kwargs": kwargs,
            # 管理员任务属于 CPU/编排侧；下载机只负责按任务名做最终路由。
            "queue": task_queue_for(task_name, requested_queue=CPU_COMPUTE_QUEUE),
        }
        if priority > BACKGROUND_TASK_PRIORITY:
            options["priority"] = celery_priority_for(priority)
        async_result = celery_client.send_task(task_name, **options)
        return {
            "dispatched": [task_name],
            "celery_id": async_result.id,
            "admin_task_run_id": payload.get("admin_task_run_id"),
        }

    if wtype == "project_task":
        payload = dict(item.get("payload_json") or {})
        task_name = str(payload.get("task_name") or "")
        if task_name not in PROJECT_TASK_NAMES:
            raise ValueError(f"unsupported project task: {task_name}")
        options = {
            "kwargs": dict(payload.get("kwargs") or {}),
            "queue": task_queue_for(task_name, requested_queue=SATELLITE_DOWNLOAD_QUEUE),
        }
        if priority > BACKGROUND_TASK_PRIORITY:
            options["priority"] = celery_priority_for(priority)
        async_result = celery_client.send_task(task_name, **options)
        return {"dispatched": [task_name], "celery_id": async_result.id}

    land_id, extras = _payload_parts(item)
    if not land_id:
        raise ValueError("work item missing land_id")

    payload = dict(item.get("payload_json") or {})
    task_id = payload.get("task_id") or extras.get("task_id")
    if wtype in ("assessment_report", "season_growth_report"):
        return _dispatch_report(wtype, work_id, land_id, extras, priority)
    if wtype in COMPLETE_ON_DISPATCH_TYPES or wtype in DEFAULT_TYPES:
        return _dispatch_via_handler(
            wtype, work_id, land_id, extras, task_id, priority
        )
    raise ValueError(f"unsupported work type for claim agent: {wtype}")


def process_item(client: httpx.Client, item: dict[str, Any]) -> None:
    """Dispatch Celery and complete the lease for fan-out data tasks.

    Celery 一旦 send_task 成功，COMPLETE_ON_DISPATCH 类型必须尽力 complete，
    不能因 progress/complete HTTP 失败再走 fail+retry（否则会双派发）；
    派发前打 dispatch_start，便于区分“卡在 handler”与“卡在回报”。
    """
    work_id = str(item["id"])
    wtype = item.get("type") or ""
    bind_trace_from_mapping(item.get("payload_json") or {})
    get_or_create_trace_id()
    dispatched_ok = False
    result: dict[str, Any] | None = None
    try:
        land_hint = None
        try:
            land_hint, _ = _payload_parts(item)
        except Exception:
            land_hint = None
        logger.info(
            "work_item_dispatch_start id=%s type=%s land_id=%s attempt=%s",
            work_id,
            wtype,
            land_hint,
            item.get("attempts"),
        )
        result = _dispatch_celery(item)
        dispatched_ok = True
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
        progress_body = {
            "stage": "dispatched",
            "celery_id": result.get("celery_id"),
            "celery_ids": result.get("celery_ids"),
            "dispatched": result.get("dispatched"),
            "admin_task_run_id": result.get("admin_task_run_id"),
            "job_id": result.get("job_id"),
            "land_id": result.get("land_id"),
        }
        try:
            progress(client, work_id, progress_body)
        except Exception:
            logger.exception("work_item_progress_failed id=%s type=%s", work_id, wtype)
        logger.info(
            "work_item_dispatched id=%s type=%s land_id=%s celery=%s",
            work_id,
            wtype,
            result.get("land_id"),
            result.get("celery_id") or result.get("celery_ids"),
        )
        if wtype in COMPLETE_ON_DISPATCH_TYPES:
            try:
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
            except Exception:
                logger.exception(
                    "work_item_complete_failed id=%s type=%s", work_id, wtype
                )
    except Exception as exc:
        if dispatched_ok and wtype in COMPLETE_ON_DISPATCH_TYPES:
            # 派发已成功：不再 fail/retry，避免双派发；租约依赖上面的 complete 或到期回收。
            logger.exception(
                "work_item_post_dispatch_error_ignored id=%s type=%s err=%s",
                work_id,
                wtype,
                exc,
            )
            return
        try:
            attempts = max(1, int(item.get("attempts") or 1))
        except (TypeError, ValueError):
            attempts = 1
        retry = attempts < MAX_WORK_ITEM_ATTEMPTS
        logger.exception(
            "work_item_dispatch_failed id=%s attempt=%s/%s retry=%s",
            work_id,
            attempts,
            MAX_WORK_ITEM_ATTEMPTS,
            retry,
        )
        try:
            fail(client, work_id, str(exc), retry=retry)
        except Exception:
            logger.exception("work_item_fail_report_failed id=%s", work_id)
    finally:
        clear_trace_id()


def run_forever() -> None:
    """Short-poll claim loop; only WORK_QUEUE_MODE=claim may start it.

    独立看门狗线程：若超过 WORK_CLAIM_WATCHDOG_SEC 无环进度，关闭并重建
    httpx 客户端，打断半开 TLS / 卡死连接池，避免数十分钟零领取。
    """
    if not should_run_claim_agent():
        raise RuntimeError(
            f"claim agent refused: WORK_QUEUE_MODE={work_queue_mode()!r} "
            "(only 'claim' is allowed; dual would double-dispatch with MQ)"
        )
    watchdog_sec = claim_watchdog_sec()
    logger.info(
        "work_agent starting worker_id=%s api=%s interval=%s types=%s "
        "http_timeout=%s watchdog_sec=%s",
        worker_id(),
        api_base_url(),
        claim_interval_sec(),
        claim_types(),
        api_http_timeout(),
        watchdog_sec,
    )
    state: dict[str, Any] = {
        "client": _client(),
        "last_ok": time.monotonic(),
        "stop": False,
        "resets": 0,
    }
    client_lock = threading.Lock()

    def mark_loop_progress() -> None:
        state["last_ok"] = time.monotonic()

    def reset_client(reason: str) -> None:
        with client_lock:
            old = state["client"]
            state["resets"] += 1
            logger.warning(
                "work_claim_watchdog_reset reason=%s resets=%s stale_sec=%.1f",
                reason,
                state["resets"],
                max(0.0, time.monotonic() - float(state["last_ok"])),
            )
            try:
                old.close()
            except Exception:
                logger.exception("work_claim_watchdog_close_failed")
            state["client"] = _client()
            state["last_ok"] = time.monotonic()

    def watchdog_loop() -> None:
        # 同线程无法打断卡住的 HTTP；独立线程 close 客户端以唤醒主环。
        while not state["stop"]:
            time.sleep(min(5.0, max(1.0, watchdog_sec / 6.0)))
            if state["stop"] or watchdog_sec <= 0:
                continue
            stale = time.monotonic() - float(state["last_ok"])
            if stale >= watchdog_sec:
                reset_client(f"no_loop_progress_for_{stale:.0f}s")

    watchdog_thread: threading.Thread | None = None
    if watchdog_sec > 0:
        watchdog_thread = threading.Thread(
            target=watchdog_loop,
            name="work-claim-watchdog",
            daemon=True,
        )
        watchdog_thread.start()

    try:
        paused_logged = False
        last_paused_hb = float("-inf")
        while True:
            client = state["client"]
            try:
                paused, backlog, cap = claim_paused_for_backlog()
                if paused:
                    if not paused_logged:
                        logger.info(
                            "work_claim_paused local_backlog=%s cap=%s", backlog, cap
                        )
                        paused_logged = True
                    now = time.monotonic()
                    if now - last_paused_hb >= paused_heartbeat_interval_sec():
                        last_paused_hb = now
                        try:
                            how = worker_heartbeat(client)
                            logger.info(
                                "work_claim_paused_heartbeat ok via=%s local_backlog=%s",
                                how,
                                backlog,
                            )
                        except Exception as exc:
                            logger.warning("work_claim_paused_heartbeat failed: %s", exc)
                    mark_loop_progress()
                    time.sleep(claim_interval_sec())
                    continue
                if paused_logged:
                    logger.info(
                        "work_claim_resumed local_backlog=%s cap=%s", backlog, cap
                    )
                    paused_logged = False
                # Prefer base-data / download work; only claim reports when none left
                # so assessment waiting_for_data does not starve satellite_batch.
                if reports_after_base_enabled():
                    base_types, report_types = split_claim_types()
                    items = (
                        claim_batch(client, types=base_types, limit=1)
                        if base_types
                        else []
                    )
                    if not items and report_types:
                        items = claim_batch(client, types=report_types, limit=1)
                else:
                    items = claim_batch(client, limit=1)
                # 空领取也算一次成功 poll，刷新看门狗。
                mark_loop_progress()
                if not items:
                    time.sleep(claim_interval_sec())
                else:
                    for item in items:
                        process_item(state["client"], item)
                        mark_loop_progress()
            except Exception:
                logger.exception("work_agent_loop_error")
                # 仅在真实 httpx 客户端已关闭时重建；MagicMock.is_closed 不能当 bool。
                closed = getattr(client, "is_closed", False) is True
                if closed and state["client"] is client:
                    reset_client("loop_saw_closed_client")
                time.sleep(claim_interval_sec())
    finally:
        state["stop"] = True
        if watchdog_thread is not None:
            watchdog_thread.join(timeout=2.0)
        try:
            state["client"].close()
        except Exception:
            logger.exception("work_agent_client_close_failed")


__all__ = [
    "COMPLETE_ON_DISPATCH_TYPES",
    "DEFAULT_MAX_LOCAL_BACKLOG",
    "BASE_DATA_TYPES",
    "BASE_DATA_TYPE_SET",
    "REPORT_TYPES",
    "REPORT_TYPE_SET",
    "backlog_queue_names",
    "HEARTBEAT_ONLY_TYPE",
    "paused_heartbeat_interval_sec",
    "worker_heartbeat",
    "claim_paused_for_backlog",
    "local_backlog",
    "max_local_backlog",
    "MAX_WORK_ITEM_ATTEMPTS",
    "ADMIN_TASK_NAMES",
    "PROJECT_TASK_NAMES",
    "DEFAULT_TYPES",
    "reports_after_base_enabled",
    "split_claim_types",
    "api_http_timeout",
    "claim_http_retries",
    "claim_http_retry_backoff_sec",
    "claim_watchdog_sec",
    "is_transient_http_error",
    "request_with_retry",
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
