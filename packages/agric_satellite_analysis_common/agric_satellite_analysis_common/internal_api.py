"""Internal HTTP client for download-host → API control/data plane (D2/D3).

When ``API_BASE_URL`` (and ``INTERNAL_API_TOKEN``) are set, workers can read
field resolve / jobs / agri scene dates via HTTP instead of SyncSession.
If unset, callers should fall back to legacy DATABASE_URL reads.

D3 writes: prefer HTTP when ``http_writes_enabled()`` (see that helper).
Keep SyncSession PG writes until ``INGEST_PG_WRITES=0`` or claim+HTTP.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from contextlib import contextmanager
from numbers import Integral, Real
from typing import Any

import httpx

__all__ = [
    "InternalApiError",
    "agri_land_meta",
    "agri_scene_dates",
    "agri_scenes_summary",
    "api_base_url",
    "apply_results",
    "cache_results",
    "assessment_bundle",
    "complete_work",
    "data_readiness",
    "daily_satellite_prepare",
    "daily_satellite_finalize",
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
]


class InternalApiError(RuntimeError):
    """Raised when an internal HTTP call fails (non-2xx or transport)."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def api_base_url() -> str:
    return _env("API_BASE_URL").rstrip("/")


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
def internal_client(*, timeout: float = 30.0) -> Iterator[httpx.Client]:
    base = api_base_url()
    if not base:
        raise InternalApiError("API_BASE_URL is required for internal HTTP")
    from agric_satellite_analysis_common.trace import attach_trace_header

    with httpx.Client(
        base_url=base,
        timeout=timeout,
        headers=_headers(),
        event_hooks={"request": [attach_trace_header]},
    ) as client:
        yield client


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
        r = c.post("/v1/internal/admin/task-runs/ensure", json=body)
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
        r = c.post(f"/v1/internal/admin/task-runs/{run_id}/status", json=body)
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
    """GET /v1/internal/lands/resolve → canonical land metadata."""
    params: dict[str, str] = {}
    if land_id:
        params["land_id"] = str(land_id)

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = c.get("/v1/internal/lands/resolve", params=params)
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
        r = c.get(f"/v1/internal/jobs/{job_id}")
        _raise_for_status(r, context="jobs/get")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("jobs/get returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def patch_job(
    job_id: str,
    body: dict[str, Any],
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """PATCH /v1/internal/jobs/{id} (status / progress / error)."""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = c.patch(f"/v1/internal/jobs/{job_id}", json=body)
        _raise_for_status(r, context="jobs/patch")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("jobs/patch returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def agri_land_meta(
    land_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /v1/internal/agri/lands/{land_id} → land_id, tile_id, land_name, …"""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = c.get(f"/v1/internal/agri/lands/{land_id}")
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
    """GET /v1/internal/agri/lands/{id}/scenes/dates — ISO dates for skip-existing."""

    def _do(c: httpx.Client) -> list[str]:
        r = c.get(
            f"/v1/internal/agri/lands/{land_id}/scenes/dates",
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


def agri_scenes_summary(
    land_id: str,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /v1/internal/agri/lands/{id}/scenes/summary."""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = c.get(f"/v1/internal/agri/lands/{land_id}/scenes/summary")
        _raise_for_status(r, context="agri/scenes-summary")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("agri/scenes-summary returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client() as c:
        return _do(c)


def land_geom(
    land_id: str,
    *,
    include_geojson: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """GET /v1/internal/lands/{id}/geom."""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = c.get(
            f"/v1/internal/lands/{land_id}/geom",
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
        r = c.post(
            "/v1/internal/results/apply",
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
    """POST /v1/internal/results/cache — enqueue a result in API Redis."""

    def _do(c: httpx.Client) -> dict[str, Any]:
        r = c.post("/v1/internal/results/cache", json={"result": result})
        _raise_for_status(r, context="results/cache")
        data = r.json()
        if not isinstance(data, dict):
            raise InternalApiError("results/cache returned non-object")
        return data

    if client is not None:
        return _do(client)
    with internal_client(timeout=timeout) as c:
        return _do(c)


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
        r = c.post(f"/v1/internal/work/{work_id}/complete", json=body)
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
        r = c.post(f"/v1/internal/work/{work_id}/fail", json=body)
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
        r = c.post(f"/v1/internal/work/{work_id}/progress", json=body)
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
        r = c.get(
            f"/v1/internal/lands/{land_id}/assessment-bundle",
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
        r = c.get(
            f"/v1/internal/lands/{land_id}/season-growth-inputs",
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
        "/v1/internal/schedule/daily-satellite",
        params={"as_of": as_of} if as_of else None,
        client=client,
    )


def daily_satellite_finalize(
    run_id: str, *, client: httpx.Client | None = None
) -> dict[str, Any]:
    """API确认结果入库后保存快照；未完成时返回阶段供Celery延时重试。"""
    return _daily_satellite_request(
        f"/v1/internal/schedule/daily-satellite/{run_id}/finalize", client=client
    )


def _daily_satellite_request(
    path: str,
    *,
    params: dict[str, str] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    def request(c: httpx.Client) -> dict[str, Any]:
        response = c.post(path, params=params)
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
        r = c.post("/v1/internal/schedule/weekly-index")
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
        r = c.get("/v1/internal/schedule/weather-lands")
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
        r = c.post("/v1/internal/schedule/overview-refresh", params=params)
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
        r = c.post(
            "/v1/internal/schedule/overview-refresh/finalize",
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
        r = c.get(
            f"/v1/internal/lands/{land_id}/data-readiness",
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
