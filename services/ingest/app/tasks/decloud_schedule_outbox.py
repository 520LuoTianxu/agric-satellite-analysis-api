"""通过 API Outbox 持久化并恢复 S2 原始产品的去云排程。"""

from __future__ import annotations

import hashlib
import json
import math
import socket
import uuid
from datetime import date, datetime
from typing import Any

import structlog
from agric_satellite_analysis_common.celery_app import CPU_COMPUTE_QUEUE
from agric_satellite_analysis_common.internal_api import (
    InternalApiError,
    claim_decloud_schedules,
    complete_decloud_schedule,
    create_decloud_schedule,
    fail_decloud_schedule,
)
from app.worker import celery_app

logger = structlog.get_logger()
OUTBOX_LEASE_SECONDS = 900
OUTBOX_CLAIM_LIMIT = 25


def _iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value or "").strip()[:10]
    return date.fromisoformat(raw).isoformat()


def _finite_percent(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _normalize_raw_results(
    raw_results: list[dict[str, Any]], *, date_from: str, date_to: str
) -> list[dict[str, Any]]:
    """把结果回执压成 planner 需要的质量字段，不把像元或OSS正文塞进队列。"""
    normalized: list[dict[str, Any]] = []
    for source in raw_results:
        if not isinstance(source, dict) or not source.get("date"):
            continue
        try:
            scene_date = _iso_date(source.get("date"))
        except (TypeError, ValueError):
            logger.warning(
                "decloud_schedule_outbox_invalid_scene_date",
                scene_date=str(source.get("date"))[:80],
            )
            continue
        if scene_date < date_from or scene_date > date_to:
            logger.warning(
                "decloud_schedule_outbox_scene_outside_window",
                scene_date=scene_date,
                date_from=date_from,
                date_to=date_to,
            )
            continue

        item: dict[str, Any] = {"date": scene_date}
        scene_id = str(
            source.get("scene_id")
            or source.get("stac_id")
            or source.get("raw_scene_id")
            or ""
        ).strip()
        if scene_id:
            item["scene_id"] = scene_id[:512]
        cloud_cover = _finite_percent(
            source.get("cloud_cover", source.get("stac_cloud"))
        )
        parcel_cloud = _finite_percent(
            source.get("parcel_cloud_cover_pct", source.get("parcel_cloud"))
        )
        if cloud_cover is not None:
            item["cloud_cover"] = cloud_cover
        if parcel_cloud is not None:
            item["parcel_cloud_cover_pct"] = parcel_cloud
        cloud_over_30 = source.get("cloud_cover_over_30", source.get("cloud_over_30"))
        if isinstance(cloud_over_30, bool):
            item["cloud_cover_over_30"] = cloud_over_30
        normalized.append(item)
    normalized.sort(
        key=lambda row: (
            row["date"],
            str(row.get("scene_id") or ""),
            json.dumps(row, sort_keys=True, separators=(",", ":")),
        )
    )
    return normalized


def _make_schedule_key(
    *,
    job_id: str,
    land_id: str,
    schedule_kind: str,
    date_from: str,
    date_to: str,
    raw_results: list[dict[str, Any]],
    mq_task_id: str | None,
    season_months: list[int] | None,
    crop_type: str | None,
) -> str:
    identity = {
        "job_id": str(job_id),
        "land_id": str(land_id),
        "schedule_kind": str(schedule_kind),
        "date_from": date_from,
        "date_to": date_to,
        "mq_task_id": mq_task_id,
        "season_months": sorted(season_months) if season_months is not None else None,
        "crop_type": crop_type,
        "raw_results": sorted(
            raw_results,
            key=lambda row: (
                row["date"],
                str(row.get("scene_id") or ""),
                json.dumps(row, sort_keys=True, separators=(",", ":")),
            ),
        ),
    }
    digest = hashlib.sha256(
        json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    # land_id已参与摘要，不放进URL键以免特殊字符破坏complete/fail路径。
    return f"sat-decloud:{job_id}:{digest}"


def _best_effort_direct_schedule(payload: dict[str, Any]) -> None:
    """API不可用时保留原同步排程路径，避免 Outbox 故障阻断原始任务收尾。"""
    from app.tasks.decloud_uncrtaints import schedule_decloud_after_raw

    schedule_decloud_after_raw(
        land_id=payload["land_id"],
        date_from=payload["date_from"],
        date_to=payload["date_to"],
        raw_results=payload["raw_results"],
        mq_task_id=payload.get("mq_task_id"),
        season_months=payload.get("season_months"),
        crop_type=payload.get("crop_type"),
    )


def persist_and_dispatch_decloud_schedule(
    *,
    job_id: str,
    land_id: str,
    date_from: str | date,
    date_to: str | date,
    raw_results: list[dict[str, Any]],
    mq_task_id: str | None,
    season_months: tuple[int, ...] | list[int] | None,
    crop_type: str | None,
    schedule_kind: str,
) -> dict[str, Any] | None:
    """先把意图提交到 API 数据库，再唤醒 dispatcher；Beat 是持久化兜底。"""
    try:
        normalized_date_from = _iso_date(date_from)
        normalized_date_to = _iso_date(date_to)
        if normalized_date_from > normalized_date_to:
            raise ValueError("date_from must not exceed date_to")
        normalized_land_id = str(land_id).strip()
        if not normalized_land_id:
            raise ValueError("land_id must not be blank")
        normalized = _normalize_raw_results(
            raw_results,
            date_from=normalized_date_from,
            date_to=normalized_date_to,
        )
        normalized_months = (
            sorted(int(month) for month in season_months)
            if season_months is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        logger.error(
            "decloud_schedule_outbox_invalid_schedule_input",
            job_id=str(job_id),
            land_id=str(land_id),
            error=str(exc),
        )
        return None
    if not normalized:
        return None
    payload: dict[str, Any] = {
        "job_id": str(job_id),
        "land_id": normalized_land_id,
        "date_from": normalized_date_from,
        "date_to": normalized_date_to,
        "raw_results": normalized,
        "mq_task_id": str(mq_task_id) if mq_task_id else None,
        "season_months": normalized_months,
        "crop_type": crop_type,
        "schedule_key": _make_schedule_key(
            job_id=job_id,
            land_id=normalized_land_id,
            schedule_kind=schedule_kind,
            date_from=normalized_date_from,
            date_to=normalized_date_to,
            raw_results=normalized,
            mq_task_id=str(mq_task_id) if mq_task_id else None,
            season_months=normalized_months,
            crop_type=crop_type,
        ),
    }
    try:
        stored = create_decloud_schedule(payload)
    except Exception as exc:
        if isinstance(exc, InternalApiError) and exc.status_code in {
            400,
            401,
            403,
            404,
            409,
            422,
        }:
            logger.error(
                "decloud_schedule_outbox_request_rejected",
                job_id=str(job_id),
                land_id=str(land_id),
                schedule_key=payload["schedule_key"],
                status_code=exc.status_code,
                error=str(exc),
            )
            return None
        logger.exception(
            "decloud_schedule_outbox_persist_failed",
            job_id=str(job_id),
            land_id=str(land_id),
            schedule_key=payload["schedule_key"],
        )
        try:
            _best_effort_direct_schedule(payload)
            return {
                "status": "scheduled_without_outbox",
                "schedule_key": payload["schedule_key"],
            }
        except Exception:
            logger.exception(
                "decloud_schedule_outbox_direct_fallback_failed",
                job_id=str(job_id),
                land_id=str(land_id),
                schedule_key=payload["schedule_key"],
            )
            return None

    if stored.get("status") != "completed":
        try:
            dispatch_decloud_schedule.apply_async(
                kwargs={"schedule_key": payload["schedule_key"]},
                queue=CPU_COMPUTE_QUEUE,
            )
        except Exception:
            # Outbox 已提交；周期扫描会在 broker 恢复后重新唤醒，不丢弃排程意图。
            logger.exception(
                "decloud_schedule_outbox_wakeup_failed",
                job_id=str(job_id),
                land_id=str(land_id),
                schedule_key=payload["schedule_key"],
            )
    return stored


def _dispatch_claimed(item: dict[str, Any], *, worker_id: str) -> bool:
    schedule_key = str(item["schedule_key"])
    try:
        from app.tasks.decloud_uncrtaints import schedule_decloud_after_raw

        outcome = schedule_decloud_after_raw(
            land_id=str(item["land_id"]),
            date_from=_iso_date(item["date_from"]),
            date_to=_iso_date(item["date_to"]),
            raw_results=item["raw_results"],
            mq_task_id=item.get("mq_task_id"),
            season_months=item.get("season_months"),
            crop_type=item.get("crop_type"),
        )
        complete_decloud_schedule(schedule_key=schedule_key, worker_id=worker_id)
        logger.info(
            "decloud_schedule_outbox_completed",
            schedule_key=schedule_key,
            job_id=str(item["job_id"]),
            land_id=str(item["land_id"]),
            outcome=outcome,
        )
        return True
    except Exception as exc:
        try:
            failure = fail_decloud_schedule(
                schedule_key=schedule_key,
                worker_id=worker_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.exception(
                "decloud_schedule_outbox_retry_scheduled",
                schedule_key=schedule_key,
                job_id=str(item.get("job_id") or ""),
                land_id=str(item.get("land_id") or ""),
                attempts=failure.get("attempts"),
                available_at=failure.get("available_at"),
            )
        except Exception:
            # 确认失败时租约会过期，下一轮扫描仍可回收；不能把原始异常吞成成功。
            logger.exception(
                "decloud_schedule_outbox_failure_report_failed",
                schedule_key=schedule_key,
                worker_id=worker_id,
            )
        return False


def _worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex}"


@celery_app.task(
    bind=True,
    name="app.tasks.decloud_schedule_outbox.dispatch_decloud_schedule",
    time_limit=180,
    soft_time_limit=150,
)
def dispatch_decloud_schedule(self, schedule_key: str) -> dict[str, Any]:
    """即时领取单个已提交排程；抢不到租约时由其他 dispatcher 正在处理。"""
    worker_id = _worker_id()
    items = claim_decloud_schedules(
        worker_id=worker_id,
        schedule_key=str(schedule_key),
        limit=1,
        lease_seconds=OUTBOX_LEASE_SECONDS,
    )
    if not items:
        return {"status": "not_claimed", "schedule_key": str(schedule_key)}
    succeeded = _dispatch_claimed(items[0], worker_id=worker_id)
    return {
        "status": "completed" if succeeded else "retry_pending",
        "schedule_key": str(schedule_key),
    }


@celery_app.task(
    name="app.tasks.decloud_schedule_outbox.dispatch_pending_schedules",
    time_limit=600,
    soft_time_limit=540,
)
def dispatch_pending_schedules() -> dict[str, Any]:
    """Beat 每分钟扫描到期 Outbox，回收即时唤醒失败或已过期的处理租约。"""
    worker_id = _worker_id()
    try:
        items = claim_decloud_schedules(
            worker_id=worker_id,
            limit=OUTBOX_CLAIM_LIMIT,
            lease_seconds=OUTBOX_LEASE_SECONDS,
        )
    except Exception:
        logger.exception("decloud_schedule_outbox_claim_failed", worker_id=worker_id)
        return {"status": "api_unavailable", "claimed": 0, "completed": 0}

    completed = sum(_dispatch_claimed(item, worker_id=worker_id) for item in items)
    logger.info(
        "decloud_schedule_outbox_sweep_finished",
        worker_id=worker_id,
        claimed=len(items),
        completed=completed,
        retry_pending=len(items) - completed,
    )
    return {"status": "completed", "claimed": len(items), "completed": completed}


__all__ = [
    "dispatch_decloud_schedule",
    "dispatch_pending_schedules",
    "persist_and_dispatch_decloud_schedule",
]
