"""HTTP claim work agent: poll API /v1/internal/work/claim → Celery dispatch.

Enabled when WORK_QUEUE_MODE=claim. Uses API_BASE_URL + INTERNAL_API_TOKEN.
Does not require DATABASE_URL or CloudAMQP.
Celery tasks call work complete/fail when finished (D3 upsert path).
"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any

import httpx
from openfarm_common.celery_app import celery_client

logger = logging.getLogger("work_agent")

DEFAULT_TYPES = ["assessment_report", "season_growth_report"]


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def work_queue_mode() -> str:
    mode = _env("WORK_QUEUE_MODE", "legacy").lower()
    return mode if mode in ("legacy", "claim", "dual") else "legacy"


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
        "types": types or DEFAULT_TYPES,
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


def _dispatch_celery(item: dict[str, Any]) -> dict[str, Any]:
    """Map work_item → existing Celery ingest tasks (mirrors mq_consumer handler)."""
    wtype = item.get("type") or ""
    payload = dict(item.get("payload_json") or {})
    field_id = payload.get("field_id")
    extras = dict(payload.get("extras") or {})
    # Allow flat payload shape as well
    if not extras and payload.get("job_id"):
        extras = {k: v for k, v in payload.items() if k != "field_id"}
        field_id = field_id or payload.get("field_id")

    if not field_id:
        raise ValueError("work item missing field_id")

    job_id = extras.get("job_id")
    work_id = str(item.get("id"))

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

    raise ValueError(f"unsupported work type for claim agent: {wtype}")


def progress(client: httpx.Client, work_id: str, progress_body: dict[str, Any]) -> None:
    r = client.post(
        f"/v1/internal/work/{work_id}/progress",
        json={"worker_id": worker_id(), "progress": progress_body},
    )
    r.raise_for_status()


def process_item(client: httpx.Client, item: dict[str, Any]) -> None:
    """Dispatch Celery; leave work_item leased until the task POSTs complete (D3).

    Previously we marked complete on dispatch, which prevented complete→upsert
    of assessment/season result metadata. Progress records the celery id instead.
    """
    work_id = str(item["id"])
    try:
        result = _dispatch_celery(item)
        progress(
            client,
            work_id,
            {
                "stage": "dispatched",
                "celery_id": result.get("celery_id"),
                "dispatched": result.get("dispatched"),
                "job_id": result.get("job_id"),
            },
        )
        logger.info(
            "work_item_dispatched id=%s type=%s celery=%s",
            work_id,
            item.get("type"),
            result.get("celery_id"),
        )
    except Exception as exc:
        logger.exception("work_item_dispatch_failed id=%s", work_id)
        try:
            fail(client, work_id, str(exc), retry=False)
        except Exception:
            logger.exception("work_item_fail_report_failed id=%s", work_id)


def run_forever() -> None:
    """Short-poll claim loop."""
    logger.info(
        "work_agent starting worker_id=%s api=%s interval=%s",
        worker_id(),
        api_base_url(),
        claim_interval_sec(),
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


__all__ = ["claim_batch", "process_item", "run_forever", "work_queue_mode"]
