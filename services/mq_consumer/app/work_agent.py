"""HTTP claim work agent: poll API /v1/internal/work/claim → Celery dispatch.

Enabled when WORK_QUEUE_MODE=claim only (never dual — avoids double-dispatch with MQ).
Uses API_BASE_URL + INTERNAL_API_TOKEN. Does not require DATABASE_URL or CloudAMQP.

Report types stay leased until the Celery task POSTs complete (D3).
Fan-out types (bootstrap / satellite / weather / soil) complete the lease after
successful Celery dispatch (orchestration ack).
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

logger = logging.getLogger("work_agent")

DEFAULT_TYPES = [
    "assessment_report",
    "season_growth_report",
    "field_bootstrap",
    "satellite_analysis",
    "agri_bridge",
    "weather_backfill",
    "soil_fetch",
]

COMPLETE_ON_DISPATCH_TYPES = frozenset(
    {
        "field_bootstrap",
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
    """Claim poller is claim-mode only (dual would double-run with MQ)."""
    return work_queue_mode() == "claim"


def claim_types() -> list[str]:
    raw = _env("WORK_CLAIM_TYPES")
    if not raw:
        return list(DEFAULT_TYPES)
    return [t.strip() for t in raw.split(",") if t.strip()]


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
    return httpx.Client(base_url=base, timeout=30.0, headers=_headers())


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
    r = client.post("/v1/internal/work/claim", json=body)
    r.raise_for_status()
    data = r.json()
    return list(data.get("items") or [])


def complete(client: httpx.Client, work_id: str, result: dict[str, Any]) -> None:
    r = client.post(
        f"/v1/internal/work/{work_id}/complete",
        json={"worker_id": worker_id(), "result": result},
    )
    r.raise_for_status()


def fail(client: httpx.Client, work_id: str, error: str, *, retry: bool = False) -> None:
    r = client.post(
        f"/v1/internal/work/{work_id}/fail",
        json={"worker_id": worker_id(), "error": error, "retry": retry},
    )
    r.raise_for_status()


def heartbeat(client: httpx.Client, work_id: str) -> None:
    r = client.post(
        f"/v1/internal/work/{work_id}/heartbeat",
        json={"worker_id": worker_id(), "lease_seconds": lease_seconds()},
    )
    r.raise_for_status()


def progress(client: httpx.Client, work_id: str, progress_body: dict[str, Any]) -> None:
    r = client.post(
        f"/v1/internal/work/{work_id}/progress",
        json={"worker_id": worker_id(), "progress": progress_body},
    )
    r.raise_for_status()


def _payload_parts(item: dict[str, Any]) -> tuple[str | None, dict[str, Any], str | None, str | None]:
    payload = dict(item.get("payload_json") or {})
    field_id = payload.get("field_id")
    land_id = payload.get("land_id")
    parcel_id = payload.get("parcel_id")
    extras = dict(payload.get("extras") or {})
    if not extras and payload.get("job_id"):
        extras = {k: v for k, v in payload.items() if k not in ("field_id", "land_id", "parcel_id")}
        field_id = field_id or payload.get("field_id")
    if not field_id and extras.get("field_id"):
        field_id = extras.get("field_id")
    return (
        str(field_id) if field_id else None,
        extras,
        str(land_id) if land_id else None,
        str(parcel_id) if parcel_id else None,
    )


def _dispatch_report(wtype: str, work_id: str, field_id: str, extras: dict[str, Any]) -> dict[str, Any]:
    job_id = extras.get("job_id")
    if wtype == "assessment_report":
        kwargs: dict[str, Any] = {
            "field_id": str(field_id),
            "work_item_id": work_id,
        }
        if job_id:
            kwargs["job_id"] = str(job_id)
        for key in ("crop_type", "crop_name_zh", "date_from", "date_to", "years"):
            if extras.get(key) is not None:
                kwargs[key] = extras[key]
        if extras.get("pull_data") is not None:
            kwargs["pull_data"] = bool(extras.get("pull_data"))
        if extras.get("wait_celery_ids"):
            kwargs["wait_celery_ids"] = list(extras["wait_celery_ids"])
        async_result = celery_client.send_task(
            "app.tasks.assessment_report.generate_assessment_report",
            kwargs=kwargs,
            queue="ingest",
        )
        return {
            "dispatched": ["app.tasks.assessment_report.generate_assessment_report"],
            "celery_id": async_result.id,
            "job_id": str(job_id) if job_id else None,
        }

    if wtype == "season_growth_report":
        kwargs = {
            "field_id": str(field_id),
            "work_item_id": work_id,
        }
        if job_id:
            kwargs["job_id"] = str(job_id)
        for key in ("start_date", "end_date", "crops", "label", "material_keys"):
            if extras.get(key) is not None:
                kwargs[key] = extras[key]
        if extras.get("pull_data") is not None:
            kwargs["pull_data"] = bool(extras.get("pull_data"))
        if extras.get("wait_celery_ids"):
            kwargs["wait_celery_ids"] = list(extras["wait_celery_ids"])
        async_result = celery_client.send_task(
            "app.tasks.season_growth_report.generate_season_growth_report",
            kwargs=kwargs,
            queue="ingest",
        )
        return {
            "dispatched": ["app.tasks.season_growth_report.generate_season_growth_report"],
            "celery_id": async_result.id,
            "job_id": str(job_id) if job_id else None,
        }

    raise ValueError(f"unsupported report type: {wtype}")


def _dispatch_via_handler(
    wtype: str,
    work_id: str,
    field_id: str | None,
    land_id: str | None,
    parcel_id: str | None,
    extras: dict[str, Any],
    task_id: str | None,
) -> dict[str, Any]:
    """Reuse mq_consumer handler dispatch for bootstrap / satellite / weather / soil."""
    from app.handler import (
        _dispatch_field_bootstrap,
        _dispatch_satellite_analysis,
        _dispatch_soil_fetch,
        _dispatch_weather_backfill,
        _resolve_field_and_land,
    )

    tid = task_id or work_id
    task = TaskMessage(
        task_id=str(tid),
        type=wtype if wtype != "agri_bridge" else "satellite_analysis",
        field_id=field_id,
        parcel_id=parcel_id,
        land_id=land_id,
        extras=dict(extras),
    )
    if wtype == "agri_bridge":
        task.extras = {**(task.extras or {}), "mode": "bridge_only"}

    resolved_fid, resolved_lid = _resolve_field_and_land(
        task.field_id, task.parcel_id, task.land_id
    )
    if not resolved_fid:
        raise ValueError(
            "field_id could not be resolved (provide field_id or agri-tagged parcel_id)"
        )

    if wtype in ("satellite_analysis", "agri_bridge"):
        info = _dispatch_satellite_analysis(task, resolved_fid, resolved_lid)
    elif wtype == "weather_backfill":
        info = _dispatch_weather_backfill(task, resolved_fid)
    elif wtype == "soil_fetch":
        info = _dispatch_soil_fetch(task, resolved_fid)
    elif wtype == "field_bootstrap":
        info = _dispatch_field_bootstrap(task, resolved_fid, resolved_lid)
    else:
        raise ValueError(f"unsupported work type for claim agent: {wtype}")

    out = dict(info or {})
    out["field_id"] = resolved_fid
    out["land_id"] = resolved_lid
    out["work_item_id"] = work_id
    return out


def _dispatch_celery(item: dict[str, Any]) -> dict[str, Any]:
    """Map work_item → existing Celery ingest tasks (mirrors mq_consumer handler)."""
    wtype = item.get("type") or ""
    work_id = str(item.get("id"))
    field_id, extras, land_id, parcel_id = _payload_parts(item)
    task_id = None
    payload = dict(item.get("payload_json") or {})
    task_id = payload.get("task_id") or extras.get("task_id")

    if wtype in ("assessment_report", "season_growth_report"):
        if not field_id:
            raise ValueError("work item missing field_id")
        return _dispatch_report(wtype, work_id, field_id, extras)

    if wtype in COMPLETE_ON_DISPATCH_TYPES or wtype in DEFAULT_TYPES:
        return _dispatch_via_handler(
            wtype, work_id, field_id, land_id, parcel_id, extras, task_id
        )

    raise ValueError(f"unsupported work type for claim agent: {wtype}")


def process_item(client: httpx.Client, item: dict[str, Any]) -> None:
    """Dispatch Celery; complete lease for fan-out types, else leave leased for reports."""
    work_id = str(item["id"])
    wtype = item.get("type") or ""
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
            },
        )
        logger.info(
            "work_item_dispatched id=%s type=%s celery=%s",
            work_id,
            wtype,
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
                    "field_id": result.get("field_id"),
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


def run_forever() -> None:
    """Short-poll claim loop. Refuses to start unless WORK_QUEUE_MODE=claim."""
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
                    continue
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
