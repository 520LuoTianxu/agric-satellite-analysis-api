"""Internal HTTP client for download-host → API control/data plane (D2/D3).

When ``API_BASE_URL`` (and ``INTERNAL_API_TOKEN``) are set, workers can read
field resolve / jobs / agri scene dates via HTTP instead of SyncSession.
If unset, callers should fall back to legacy DATABASE_URL reads.

Paths are ``{API_PATH_PREFIX}/internal/...`` (default prefix ``/v1``). Set
``API_PATH_PREFIX`` empty when pointing at the public ``.../satellite-api``
gateway, which exposes ``/internal`` without ``/v1``.

D3 writes: prefer HTTP when ``http_writes_enabled()`` (see that helper).
Keep SyncSession PG writes until ``INGEST_PG_WRITES=0`` or claim+HTTP.
"""

from __future__ import annotations

import atexit
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from numbers import Integral, Real
from typing import Any

import httpx

logger = logging.getLogger(__name__)

__all__ = [
    "InternalApiError",
    "agri_land_meta",
    "agri_scene_dates",
    "agri_scenes_summary",
    "api_base_url",
    "api_path_prefix",
    "internal_api_path",
    "apply_results",
    "cache_results",
    "close_cached_result_client",
    "assessment_bundle",
    "agri_satellite_batch_inputs",
    "create_decloud_schedule",
    "claim_decloud_schedules",
    "complete_decloud_schedule",
    "fail_decloud_schedule",
    "complete_work",
    "data_readiness",
    "daily_satellite_prepare",
    "daily_satellite_finalize",
    "evaluate_land_alerts",
    "ensure_admin_task_run",
    "finalize_overview_stats",
    "fail_work",
    "land_geom",
    "get_job",
    "http_writes_enabled",
    "ingest_pg_reads_allowed",
    "ingest_pg_writes_enabled",
    "internal_api_enabled",
    "internal_api_token",
    "internal_client",
    "patch_job",
    "progress_work",
    "refresh_overview_stats",
    "update_admin_task_run_status",
    "resolve_land",
    "season_growth_inputs",
    "weather_land_ids",
    "weekly_index_prepare",
    "satellite_history_backfill",
    "http_timeout",
    "http_retries",
    "http_retry_backoff_sec",
    "is_transient_http_error",
    "request_with_retry",
]


class InternalApiError(RuntimeError):
    """Raised when an internal HTTP call fails (non-2xx or transport)."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


_result_client_lock = threading.Lock()
_result_client: httpx.Client | None = None
_result_client_pid: int | None = None


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def api_base_url() -> str:
    return _env("API_BASE_URL").rstrip("/")


def api_path_prefix() -> str:
    """Path prefix before ``/internal/...``.

    Defaults to ``/v1`` (direct API / compose). Set ``API_PATH_PREFIX`` empty
    (or ``-`` / ``none``) when ``API_BASE_URL`` is the public gateway
    ``.../satellite-api``, which already serves ``/internal`` without ``/v1``.
    """
    if "API_PATH_PREFIX" not in os.environ:
        return "/v1"
    raw = (os.environ.get("API_PATH_PREFIX") or "").strip()
    if raw in ("", "-", "none"):
        return ""
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/")


def _ipath(path: str) -> str:
    """Build an Internal HTTP path under the configured ``API_PATH_PREFIX``."""
    if not path.startswith("/"):
        path = "/" + path
    prefix = api_path_prefix()
    if not prefix:
        return path
    return prefix + path


internal_api_path = _ipath


def internal_api_token() -> str:
    return _env("INTERNAL_API_TOKEN")


def internal_api_enabled() -> bool:
    """True when download-side HTTP reads should be preferred over SyncSession."""
    return bool(api_base_url() and internal_api_token())


def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def _falsey(val: str) -> bool:
    return val.strip().lower() in ("0", "false", "no", "off")


def ingest_pg_writes_enabled() -> bool:
    """Legacy SyncSession PG writes on download host (default on).

    Set ``INGEST_PG_WRITES=0`` to disable direct PG writes once HTTP path is ready.
    """
    raw = _env("INGEST_PG_WRITES", "1")
    if _falsey(raw):
        return False
    return True


def ingest_pg_reads_allowed() -> bool:
    """Whether download workers may use SyncSession for reads.

    Defaults to the same as ``INGEST_PG_WRITES``. Override with ``INGEST_PG_READS``
    (``1`` keep PG reads while writes are HTTP-only; ``0`` forbid PG reads).
    """
    raw = _env("INGEST_PG_READS", "")
    if raw:
        return not _falsey(raw)
    return ingest_pg_writes_enabled()


def http_writes_enabled() -> bool:
    """True when ingest should write job/domain results via internal HTTP.

    Enabled when internal API is configured AND either:
    - ``INGEST_PG_WRITES=0`` (force HTTP writes), or
    - ``WORK_QUEUE_MODE=claim`` (claim path implies HTTP result delivery), or
    - ``INGEST_HTTP_WRITES=1`` (explicit dual-run / canary)
    """
    if not internal_api_enabled():
        return False
    if not ingest_pg_writes_enabled():
        return True
    mode = _env("WORK_QUEUE_MODE", "legacy").lower()
    if mode == "claim":
        return True
    return _truthy(_env("INGEST_HTTP_WRITES", "0"))


def _headers() -> dict[str, str]:
    from agric_satellite_analysis_common.trace import TRACE_HEADER, current_trace_id

    token = internal_api_token()
    if not token:
        raise InternalApiError("INTERNAL_API_TOKEN is required for internal HTTP")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    tid = current_trace_id()
    if tid:
        headers[TRACE_HEADER] = tid
    return headers




_TRANSIENT_STATUS = frozenset({502, 503, 504})


def http_retries() -> int:
    """瞬时 TLS/超时/协议错误时的额外重试次数（不含首次）。"""
    try:
        return max(0, int(_env("INTERNAL_API_HTTP_RETRIES", "3")))
    except ValueError:
        return 3


def http_retry_backoff_sec() -> float:
    try:
        return max(0.05, float(_env("INTERNAL_API_HTTP_RETRY_BACKOFF_SEC", "0.5")))
    except ValueError:
        return 0.5


def http_timeout(total: float | None = None) -> httpx.Timeout:
    """Internal API 出站超时：read/write 用 total，connect/pool 单独收紧。"""
    if total is None:
        try:
            total_v = max(5.0, float(_env("INTERNAL_API_HTTP_TIMEOUT_SEC", "30")))
        except ValueError:
            total_v = 30.0
    else:
        total_v = max(1.0, float(total))
    try:
        connect = max(1.0, float(_env("INTERNAL_API_HTTP_CONNECT_TIMEOUT_SEC", "5")))
    except ValueError:
        connect = 5.0
    connect = min(connect, total_v)
    pool = min(connect, 5.0)
    return httpx.Timeout(
        total_v, connect=connect, read=total_v, write=total_v, pool=pool
    )


def _as_timeout(timeout: float | httpx.Timeout) -> httpx.Timeout:
    if isinstance(timeout, httpx.Timeout):
        return timeout
    return http_timeout(float(timeout))


def is_transient_http_error(exc: BaseException) -> bool:
    """网关 TLS/超时/协议抖动与 502–504 视为可重试。"""
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
    msg = str(exc).upper()
    markers = (
        "UNEXPECTED_EOF",
        "EOF OCCURRED IN VIOLATION OF PROTOCOL",
        "HANDSHAKE OPERATION TIMED OUT",
        "SERVER DISCONNECTED",
        "CONNECTION RESET",
        "SSL: ",
        "SSLEOF",
    )
    if any(marker in msg for marker in markers):
        return isinstance(exc, (OSError, httpx.HTTPError, ConnectionError, TimeoutError))
    return False


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int | None = None,
    backoff: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_transient: Callable[[BaseException, int], None] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """对 Internal API 出站请求做短退避重试；非瞬时错误立即抛出。"""
    extra = http_retries() if retries is None else max(0, int(retries))
    delay = (
        http_retry_backoff_sec() if backoff is None else max(0.0, float(backoff))
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
                    "internal_api_http_retry status=%s attempt=%s/%s method=%s url=%s",
                    response.status_code,
                    i + 1,
                    attempts,
                    method.upper(),
                    url,
                )
                if on_transient is not None:
                    on_transient(
                        httpx.HTTPStatusError(
                            f"HTTP {response.status_code}",
                            request=response.request,
                            response=response,
                        ),
                        i,
                    )
                sleep(delay * (2**i))
                continue
            return response
        except Exception as exc:
            last_exc = exc
            if not is_transient_http_error(exc) or i + 1 >= attempts:
                raise
            logger.warning(
                "internal_api_http_retry error=%s attempt=%s/%s method=%s url=%s",
                exc,
                i + 1,
                attempts,
                method.upper(),
                url,
            )
            if on_transient is not None:
                on_transient(exc, i)
            sleep(delay * (2**i))
    assert last_exc is not None
    raise last_exc


def _json_safe_payload(value: Any) -> Any:
    """Recursively normalize values before encoding an internal API payload."""
    if isinstance(value, dict):
        return {key: _json_safe_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_payload(item) for item in value]
    if isinstance(value, Real) and not isinstance(value, Integral):
        normalized = float(value)
        # 所有 results/apply 调用统一清洗非有限浮点，避免 HTTP JSON 编码直接失败。
        return normalized if math.isfinite(normalized) else None
    return value


@contextmanager
def internal_client(*, timeout: float | httpx.Timeout = 30.0) -> Iterator[httpx.Client]:
    base = api_base_url()
    if not base:
        raise InternalApiError("API_BASE_URL is required for internal HTTP")
    from agric_satellite_analysis_common.trace import attach_trace_header

    with httpx.Client(
        base_url=base,
        timeout=_as_timeout(timeout),
        headers=_headers(),
        event_hooks={"request": [attach_trace_header]},
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    ) as client:
        yield client


def _cached_result_client(*, timeout: float | httpx.Timeout = 30.0) -> httpx.Client:
    """复用下载worker的回执HTTP连接池，避免每景结果都重新握手。"""
    global _result_client, _result_client_pid

    base = api_base_url()
    if not base:
        raise InternalApiError("API_BASE_URL is required for internal HTTP")
    token = internal_api_token()
    if not token:
        raise InternalApiError("INTERNAL_API_TOKEN is required for internal HTTP")

    pid = os.getpid()
    with _result_client_lock:
        if (
            _result_client is not None
            and _result_client_pid == pid
            and not _result_client.is_closed
        ):
            return _result_client

        # Celery prefork子进程不可沿用父进程的socket；进程号变化时重建连接池。
        if _result_client is not None:
            _result_client.close()

        from agric_satellite_analysis_common.trace import (
            TRACE_HEADER,
            attach_trace_header,
        )

        headers = _headers()
        # trace由request hook按每次调用注入，不能固化首次建池线程的trace值。
        headers.pop(TRACE_HEADER, None)
        _result_client = httpx.Client(
            base_url=base,
            timeout=_as_timeout(timeout),
            headers=headers,
            event_hooks={"request": [attach_trace_header]},
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
        _result_client_pid = pid
        return _result_client


def close_cached_result_client(*_args: Any, **_kwargs: Any) -> None:
    """worker退出时释放共享回执连接池；重复关闭保持安全。"""
    global _result_client, _result_client_pid

    with _result_client_lock:
        client = _result_client
        _result_client = None
        _result_client_pid = None
    if client is not None:
        client.close()


def _call_with_cached_client(
    do: Callable[[httpx.Client], Any],
    *,
    timeout: float | httpx.Timeout = 30.0,
) -> Any:
    """高频 Internal API 走进程级连接池；TLS/EOF 半开时整池重建再试一轮。"""
    try:
        return do(_cached_result_client(timeout=timeout))
    except Exception as exc:
        if not is_transient_http_error(exc):
            raise
        close_cached_result_client()
        return do(_cached_result_client(timeout=timeout))


atexit.register(close_cached_result_client)


def _raise_for_status(r: httpx.Response, *, context: str) -> None:
    if r.is_success:
        return
    detail = ""
    try:
        body = r.json()
        if isinstance(body, dict):
            detail = str(body.get("detail") or body)[:500]
    except Exception:
        detail = (r.text or "")[:500]
    raise InternalApiError(
        f"{context} failed status={r.status_code} detail={detail}",
        status_code=r.status_code,
    )


def ensure_admin_task_run(
    task_key: str,
    task_name: str,
    execution_key: str,
    *,
    params: dict[str, Any] | None = None,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """让 Beat 任务通过 Internal HTTP 幂等创建管理员运行记录。"""
    body = {
        "task_key": str(task_key),
        "task_name": str(task_name),
        "execution_key": str(execution_key),
        "params": dict(params or {}),
    }

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", _ipath("/internal/admin/task-runs/ensure"), json=body)
        _raise_for_status(r, context="admin/task-runs/ensure")
        data = r.json()
        if not isinstance(data, dict) or not data.get("run_id"):
            raise InternalApiError("admin/task-runs/ensure returned invalid data")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def update_admin_task_run_status(
    run_id: str,
    status: str,
    *,
    celery_task_id: str | None = None,
    result: Any | None = None,
    error: str | None = None,
    worker_name: str = "scheduled-task",
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """通过 Internal HTTP 回报 Beat/Celery 任务的真实状态。"""
    body: dict[str, Any] = {
        "worker_name": worker_name,
        "status": str(status),
    }
    if celery_task_id:
        body["celery_task_id"] = str(celery_task_id)
    if result is not None:
        body["result"] = result
    if error is not None:
        body["error"] = str(error)[:4000]

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", _ipath(f"/internal/admin/task-runs/{run_id}/status"), json=body)
        _raise_for_status(r, context="admin/task-runs/status")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("admin/task-runs/status returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def resolve_land(
    *,
    land_id: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """读取单个规范地块元数据；下载机只能通过此 Internal HTTP 边界取数。"""
    params: dict[str, str] = {}
    if land_id:
        params["land_id"] = str(land_id)

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", _ipath("/internal/lands/resolve"), params=params)
        _raise_for_status(r, context="lands/resolve")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("lands/resolve returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def get_job(
    job_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /v1/internal/jobs/{id}."""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", _ipath(f"/internal/jobs/{job_id}"))
        _raise_for_status(r, context="jobs/get")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("jobs/get returned non-object")
        return data

    if client is not None:
        return _do(client)
    return _call_with_cached_client(_do)


def patch_job(
    job_id: str,
    body: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    include_progress: bool = True,
) -> dict[str, Any]:
    """PATCH /v1/internal/jobs/{id} (status / progress / error)."""

    def _do(c: httpx.Client) -> dict[str, Any]:
        if include_progress:
            r = request_with_retry(c, "PATCH", _ipath(f"/internal/jobs/{job_id}"), json=body)
        else:
            # 高频增量进度只需确认写入状态，省去往返不断增长的产品历史列表。
            r = request_with_retry(c, "PATCH", 
                _ipath(f"/internal/jobs/{job_id}"),
                json=body,
                params={"include_progress": "false"},
            )
        _raise_for_status(r, context="jobs/patch")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("jobs/patch returned non-object")
        return data

    if client is not None:
        return _do(client)
    return _call_with_cached_client(_do)


def create_decloud_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    """把已发布 S2 的去云排程意图持久化到 API 数据库 Outbox。"""
    with internal_client(timeout=30.0) as client:
        response = request_with_retry(client, "POST", 
            _ipath("/internal/decloud-schedules"),
            json=_json_safe_payload(payload),
        )
        _raise_for_status(response, context="decloud-schedules/create")
        data = response.json()
        if not isinstance(data, dict) or not data.get("schedule_key"):
            raise InternalApiError("decloud-schedules/create returned invalid response")
        return data


def claim_decloud_schedules(
    *,
    worker_id: str,
    schedule_key: str | None = None,
    limit: int = 25,
    lease_seconds: int = 900,
) -> list[dict[str, Any]]:
    """用数据库租约领取待排程项；多 dispatcher 并发时不会同时领取同一行。"""
    body = {
        "worker_id": worker_id,
        "schedule_key": schedule_key,
        "limit": limit,
        "lease_seconds": lease_seconds,
    }
    with internal_client(timeout=30.0) as client:
        response = request_with_retry(client, "POST", 
            _ipath("/internal/decloud-schedules/claim"),
            json=body,
        )
        _raise_for_status(response, context="decloud-schedules/claim")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise InternalApiError("decloud-schedules/claim returned invalid response")
        return [item for item in data["items"] if isinstance(item, dict)]


def complete_decloud_schedule(*, schedule_key: str, worker_id: str) -> None:
    """确认去云计划已经成功交给下游 Celery 队列。"""
    with internal_client() as client:
        response = request_with_retry(client, "POST", 
            _ipath(f"/internal/decloud-schedules/{schedule_key}/complete"),
            json={"worker_id": worker_id},
        )
        _raise_for_status(response, context="decloud-schedules/complete")


def fail_decloud_schedule(
    *, schedule_key: str, worker_id: str, error: str
) -> dict[str, Any]:
    """保留排程失败原因并按 API 端退避时间重新开放领取。"""
    with internal_client() as client:
        response = request_with_retry(client, "POST", 
            _ipath(f"/internal/decloud-schedules/{schedule_key}/fail"),
            json={"worker_id": worker_id, "error": str(error)[:2000]},
        )
        _raise_for_status(response, context="decloud-schedules/fail")
        data = response.json()
        if not isinstance(data, dict):
            raise InternalApiError("decloud-schedules/fail returned invalid response")
        return data


def agri_land_meta(
    land_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /v1/internal/agri/lands/{land_id} → land_id, tile_id, land_name, …"""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", _ipath(f"/internal/agri/lands/{land_id}"))
        _raise_for_status(r, context="agri/land")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("agri/land returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def agri_scene_dates(
    land_id: str,
    *,
    sensor: str,
    client: httpx.Client | None = None,
) -> list[str]:
    """读取单个地块的已入库日期，沿用原始影像过滤口径以安全跳过重复处理。"""

    def _do(c: httpx.Client) -> list[str]:
        r = request_with_retry(c, "GET", 
            _ipath(f"/internal/agri/lands/{land_id}/scenes/dates"),
            params={"sensor": sensor},
        )
        _raise_for_status(r, context="agri/scene-dates")
        data = r.json()
        if isinstance(data, dict):
            dates = data.get("dates") or []
        elif isinstance(data, list):
            dates = data
        else:
            raise InternalApiError("agri/scene-dates returned unexpected shape")
        return [str(d)[:10] for d in dates]

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def agri_satellite_batch_inputs(
    *,
    land_ids: list[str],
    sensor: str,
    include_existing_dates: bool = True,
    date_from: str | None = None,
    date_to: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
    existing_algorithm_version: str | None = None,
) -> list[dict[str, Any]]:
    """批量读取遥感批任务所需地块元数据及已存在日期。

    ``existing_algorithm_version`` 非空时只返回该算法版本的已有日期（旧版本景将被重下）。
    """
    if (date_from is None) != (date_to is None):
        raise ValueError("date_from and date_to must be provided together")
    body = {
        "land_ids": [str(land_id) for land_id in land_ids],
        "sensor": str(sensor),
        "include_existing_dates": bool(include_existing_dates),
    }
    # 日期边界成对传输；旧调用不传时仍按兼容契约读取全部历史日期。
    if date_from is not None and date_to is not None:
        body.update({"date_from": date_from, "date_to": date_to})
    if existing_algorithm_version:
        body["existing_algorithm_version"] = str(existing_algorithm_version)

    def _do(c: httpx.Client) -> list[dict[str, Any]]:
        r = request_with_retry(c, "POST", _ipath("/internal/agri/satellite-batch/inputs"), json=body)
        _raise_for_status(r, context="agri/satellite-batch/inputs")
        data = r.json()
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise InternalApiError("agri/satellite-batch/inputs returned invalid data")
        return items

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def project_harvest_inputs(
    run_id: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """项目收获分析运行输入：去重后地块（裁剪几何）+ 全部地块边界 + 参数。"""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", _ipath(f"/internal/project-harvest/runs/{run_id}/inputs"))
        _raise_for_status(r, context="project-harvest/inputs")
        data = r.json()
        if not isinstance(data, dict) or "parcels" not in data:
            raise InternalApiError("project-harvest/inputs returned invalid data")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def project_cog_inputs(
    *,
    group_id: str | None = None,
    land_ids: list[str] | None = None,
    client: httpx.Client | None = None,
    timeout: float = 60.0,
) -> list[dict[str, Any]]:
    """项目级 COG 输入：按 group_id，或按地块找到所属项目，返回各项目全部有效地块边界。"""
    if not group_id and not land_ids:
        raise ValueError("group_id or land_ids required")
    body: dict[str, Any] = {}
    if group_id:
        body["group_id"] = str(group_id)
    if land_ids:
        body["land_ids"] = [str(x) for x in land_ids]

    def _do(c: httpx.Client) -> list[dict[str, Any]]:
        r = request_with_retry(c, "POST", _ipath("/internal/agri/project-cogs/inputs"), json=body)
        _raise_for_status(r, context="agri/project-cogs/inputs")
        data = r.json()
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, list):
            raise InternalApiError("agri/project-cogs/inputs returned invalid data")
        return groups

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def agri_scenes_summary(
    land_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /v1/internal/agri/lands/{id}/scenes/summary."""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", _ipath(f"/internal/agri/lands/{land_id}/scenes/summary"))
        _raise_for_status(r, context="agri/scenes-summary")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("agri/scenes-summary returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def evaluate_land_alerts(
    land_id: str,
    *,
    replace_open: bool = True,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """POST /v1/internal/lands/{id}/alerts/evaluate; the API owns the database write."""
    body = {"replace_open": bool(replace_open)}

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", _ipath(f"/internal/lands/{land_id}/alerts/evaluate"), json=body)
        _raise_for_status(r, context="lands/alerts/evaluate")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("lands/alerts/evaluate returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def land_geom(
    land_id: str,
    *,
    include_geojson: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /v1/internal/lands/{id}/geom."""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", 
            _ipath(f"/internal/lands/{land_id}/geom"),
            params={"include_geojson": 1 if include_geojson else 0},
        )
        _raise_for_status(r, context="lands/geom")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("lands/geom returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def apply_results(
    result: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """POST /v1/internal/results/apply — domain upserts without a work_item lease."""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", 
            _ipath("/internal/results/apply"),
            json={"result": _json_safe_payload(result)},
        )
        _raise_for_status(r, context="results/apply")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("results/apply returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def cache_results(
    result: dict[str, Any],
    *,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """将单景处理结果回执到 API 队列，默认复用进程级 HTTP 连接池。"""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(
            c,
            "POST",
            _ipath("/internal/results/cache"),
            json={"result": result},
        )
        _raise_for_status(r, context="results/cache")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("results/cache returned non-object")
        return data

    if client is not None:
        return _do(client)
    return _call_with_cached_client(_do, timeout=timeout)


def complete_work(
    work_id: str,
    result: dict[str, Any] | None = None,
    *,
    worker_id: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST /v1/internal/work/{id}/complete (triggers API-side result apply)."""
    body: dict[str, Any] = {"result": dict(result or {})}
    if worker_id:
        body["worker_id"] = worker_id

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", _ipath(f"/internal/work/{work_id}/complete"), json=body)
        _raise_for_status(r, context="work/complete")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("work/complete returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def fail_work(
    work_id: str,
    error: str,
    *,
    worker_id: str | None = None,
    retry: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST /v1/internal/work/{id}/fail."""
    body: dict[str, Any] = {"error": error, "retry": retry}
    if worker_id:
        body["worker_id"] = worker_id

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", _ipath(f"/internal/work/{work_id}/fail"), json=body)
        _raise_for_status(r, context="work/fail")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("work/fail returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def progress_work(
    work_id: str,
    progress: dict[str, Any],
    *,
    worker_id: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST /v1/internal/work/{id}/progress."""
    body: dict[str, Any] = {"progress": dict(progress or {})}
    if worker_id:
        body["worker_id"] = worker_id

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", _ipath(f"/internal/work/{work_id}/progress"), json=body)
        _raise_for_status(r, context="work/progress")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("work/progress returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def assessment_bundle(
    land_id: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """GET /v1/internal/lands/{id}/assessment-bundle — full parcel bundle."""
    params: dict[str, str] = {}
    if date_from:
        params["date_from"] = str(date_from)[:10]
    if date_to:
        params["date_to"] = str(date_to)[:10]

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", 
            _ipath(f"/internal/lands/{land_id}/assessment-bundle"),
            params=params or None,
        )
        _raise_for_status(r, context="lands/assessment-bundle")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("assessment-bundle returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def season_growth_inputs(
    land_id: str,
    *,
    date_from: str,
    date_to: str,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """GET /v1/internal/lands/{id}/season-growth-inputs — parcel + S1/S2 rows."""
    params = {"date_from": str(date_from)[:10], "date_to": str(date_to)[:10]}

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", 
            _ipath(f"/internal/lands/{land_id}/season-growth-inputs"),
            params=params,
        )
        _raise_for_status(r, context="lands/season-growth-inputs")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("season-growth-inputs returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def daily_satellite_prepare(
    *, as_of: str | None = None, client: httpx.Client | None = None
) -> dict[str, Any]:
    """通过API发现并派发每日全国下载，下载机不读取Postgres。"""
    return _daily_satellite_request(
        _ipath("/internal/schedule/daily-satellite"),
        params={"as_of": as_of} if as_of else None,
        client=client,
    )


def daily_satellite_finalize(
    run_id: str, *, client: httpx.Client | None = None
) -> dict[str, Any]:
    """API确认结果入库后保存快照；未完成时返回阶段供Celery延时重试。"""
    return _daily_satellite_request(
        _ipath(f"/internal/schedule/daily-satellite/{run_id}/finalize"), client=client
    )


def satellite_history_backfill(
    *, as_of: str | None = None, client: httpx.Client | None = None
) -> dict[str, Any]:
    """请求 API 机按地块动态规划 10km 窗口并调度历史下载。"""
    params = {"as_of": as_of} if as_of else None

    def _do(c: httpx.Client) -> dict[str, Any]:
        response = request_with_retry(c, "POST", _ipath("/internal/schedule/satellite-history"), params=params)
        _raise_for_status(response, context="schedule/satellite-history")
        data = response.json()
        if not isinstance(data, dict):
            raise InternalApiError("satellite-history returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=120.0) as c:
        return _do(c)


def _daily_satellite_request(
    path: str,
    *,
    params: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    def request(c: httpx.Client) -> dict[str, Any]:
        response = request_with_retry(c, "POST", path, params=params)
        _raise_for_status(response, context="schedule/daily-satellite")
        result = response.json()
        if not isinstance(result, dict):
            raise InternalApiError("daily-satellite returned non-object")
        return result

    if client is not None:
        return request(client)
    with internal_client(timeout=300.0) as connection:
        return request(connection)


def weekly_index_prepare(
    *,
    client: httpx.Client | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """POST /v1/internal/schedule/weekly-index：过期地块 + 在 API 建 Job。"""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", _ipath("/internal/schedule/weekly-index"))
        _raise_for_status(r, context="schedule/weekly-index")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("weekly-index returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def weather_land_ids(
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /v1/internal/schedule/weather-lands：每日天气要拉的 land_id。"""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", _ipath("/internal/schedule/weather-lands"))
        _raise_for_status(r, context="schedule/weather-lands")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("weather-lands returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def refresh_overview_stats(
    *,
    window_days: int = 60,
    crop: str | None = None,
    land_batch_size: int = 10,
    after_land_id: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """POST /v1/internal/schedule/overview-refresh：取一批总览 OSS 输入数据。"""
    params: dict[str, str] = {
        "window_days": str(int(window_days)),
        "land_batch_size": str(int(land_batch_size)),
    }
    if crop:
        params["crop"] = str(crop)
    if after_land_id:
        params["after_land_id"] = str(after_land_id)

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", _ipath("/internal/schedule/overview-refresh"), params=params)
        _raise_for_status(r, context="schedule/overview-refresh")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("overview-refresh returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def finalize_overview_stats(
    result_oss_key: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """POST /v1/internal/schedule/overview-refresh/finalize：提交 OSS 结果包。"""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "POST", 
            _ipath("/internal/schedule/overview-refresh/finalize"),
            json={"result_oss_key": str(result_oss_key)},
        )
        _raise_for_status(r, context="schedule/overview-refresh/finalize")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("overview-refresh/finalize returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


def data_readiness(
    land_id: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /v1/internal/lands/{id}/data-readiness — weather/soil/RS coverage counts."""
    params: dict[str, str] = {}
    if date_from:
        params["date_from"] = str(date_from)[:10]
    if date_to:
        params["date_to"] = str(date_to)[:10]

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = request_with_retry(c, "GET", 
            _ipath(f"/internal/lands/{land_id}/data-readiness"),
            params=params or None,
        )
        _raise_for_status(r, context="lands/data-readiness")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("data-readiness returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)
