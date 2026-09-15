"""HTTP claim work agent for direct canonical land-parcel tasks.

Claim payloads contain one identity only: land_id. Report and data workers
therefore receive the same value without a field/parcel translation step.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any

import httpx
from openfarm_common.celery_app import celery_client
from openfarm_common.mq_schemas import TaskMessage
from openfarm_common.trace import (
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
    "agri_bridge",
    "weather_backfill",
    "soil_fetch",
]

COMPLETE_ON_DISPATCH_TYPES = frozenset(
    {
        "land_bootstrap",
        "satellite_analysis",
        "agri_bridge",
        "weather_backfill",
        "soil_fetch",
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


def worker_id() -> str:
    return _env("WORKER_ID") or socket.gethostname()


def api_base_url() -> str:
    return _env("API_BASE_URL").rstrip("/")


def claim_interval_sec() -> float:
    try:
        return max(1.0, float(_env("WORK_CLAIM_INTERVAL_SEC", "4")))
    except ValueError:
        return 4.0


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
    body = {
        "worker_id": worker_id(),
        "types": types or claim_types(),
        "limit": limit,
        "lease_seconds": lease_seconds(),
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
    async_result = celery_client.send_task(task_name, kwargs=kwargs, queue="ingest")
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
        progress(
            client,
            work_id,
            {
                "stage": "dispatched",
                "celery_id": result.get("celery_id"),
                "celery_ids": result.get("celery_ids"),
                "dispatched": result.get("dispatched"),
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
    "DEFAULT_TYPES",
    "claim_batch",
    "claim_types",
    "process_item",
    "run_forever",
    "should_run_claim_agent",
    "work_queue_mode",
]
