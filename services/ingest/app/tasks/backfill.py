"""Canonical land-parcel remote-sensing backfill orchestration.

Each date chunk creates one optical job for the canonical land parcel.  The
worker calculates all supported optical indices in memory and writes the
result to ``parcel_scene_products``; there is no second parcel identity or
identity-selection flag in this path.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import or_, select, text

import structlog

from agric_satellite_analysis_common.scheduled_land_filter import (
    EXCLUDED_SCHEDULE_BASE_IDS,
    MAX_SCHEDULE_LAND_AREA_MU,
    is_scheduled_land_allowed,
)
from app.core.config import settings
from app.worker import celery_app

logger = structlog.get_logger()


def get_db_session():
    """按需创建本地数据库会话，避免 HTTP-only 编排加载栅格依赖。"""
    from app.core.database_sync import SyncSession

    return SyncSession()


def _date_chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Split [start, end] into non-overlapping segments of chunk_days."""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


@celery_app.task(
    name="app.tasks.backfill.backfill_indices_for_land",
    bind=True,
    max_retries=1,
    time_limit=120,
    soft_time_limit=90,
)
def backfill_indices_for_land(
    self,
    land_id: str,
    months: int | None = None,
    sentinel_job_id: str | None = None,
    force: bool = False,
    mq_task_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    growing_seasons: list | None = None,
    season_months: list | None = None,
    processing_window_km: float | None = None,
) -> dict:
    """Backfill vegetation indices for *land_id* over *months*.

    Splits the date range into chunks and dispatches one canonical optical job
    per chunk plus the Sentinel-1 pull. ``force`` is passed to workers so a
    caller can explicitly reprocess existing dates.

    When download-host HTTP-only mode is on (``INGEST_PG_WRITES=0`` / claim),
    orchestration uses internal land resolve HTTP and fans out Celery kwargs
    without SyncSession or local Job rows (same idea as weather/soil http_only).
    """
    from app.core.http_mode import ingest_http_only

    if ingest_http_only():
        return _backfill_indices_http_only(
            land_id=land_id,
            months=months,
            sentinel_job_id=sentinel_job_id,
            force=force,
            mq_task_id=mq_task_id,
            date_from=date_from,
            date_to=date_to,
            growing_seasons=growing_seasons,
            season_months=season_months,
            processing_window_km=processing_window_km,
        )

    from app.models.tables import LandParcel, Job

    months = months or settings.index_backfill_months
    chunk_days = settings.index_backfill_chunk_days

    session = get_db_session()
    try:
        land = session.get(LandParcel, land_id)
        if not land or land.deleted_at is not None:
            logger.error("backfill_land_not_found", land_id=land_id)
            return {
                "land_id": land_id,
                "status": "error",
                "detail": "Land parcel not found",
            }
        if not is_scheduled_land_allowed(land.base_id, land.land_area_mu):
            logger.info("backfill_land_filtered", land_id=land_id)
            return {
                "land_id": land_id,
                "status": "cancelled",
                "reason": "land_filtered",
            }

        # MQ 至少一次投递可能让同一地块同时启动两个编排任务；事务锁让
        # 重复消息在首个任务提交后再检查 sentinel，避免重复创建下载分片。
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"backfill:{land_id}"},
        )
        if sentinel_job_id:
            existing_sentinel = session.get(Job, uuid.UUID(sentinel_job_id))
            if existing_sentinel and existing_sentinel.status in {"completed", "failed"}:
                return {
                    "land_id": land_id,
                    "status": "already_handled",
                    "sentinel_job_id": sentinel_job_id,
                }

        end_date = date.fromisoformat(date_to) if date_to else date.today()
        if date_from:
            start_date = date.fromisoformat(date_from)
        else:
            start_date = end_date - timedelta(days=months * 30)
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        chunks = _date_chunks(start_date, end_date, chunk_days)
        jobs_dispatched = 0
        stagger_seconds = 30  # seconds between chunk groups
        pending_sends: list[tuple[str, str, int]] = []
        # 一个分片对应一个规范光学任务；任务内部一次计算全部指数，避免
        # 为同一地块再维护按指数拆分的旧任务链。
        for chunk_idx, (chunk_start, chunk_end) in enumerate(chunks):
            params_json = {
                "date_from": chunk_start.isoformat(),
                "date_to": chunk_end.isoformat(),
                "is_backfill": True,
                "force": bool(force),
                "path": "agri_lonlat_direct",
                **({"mq_task_id": mq_task_id} if mq_task_id else {}),
                **({"growing_seasons": growing_seasons} if growing_seasons else {}),
                **({"season_months": season_months} if season_months else {}),
                **(
                    {"processing_window_km": processing_window_km}
                    if processing_window_km is not None
                    else {}
                ),
            }
            job = Job(
                land_id=land.land_id,
                type="agri_optical",
                status="pending",
                params_json=params_json,
            )
            session.add(job)
            session.flush()
            countdown = chunk_idx * stagger_seconds
            pending_sends.append(
                (
                    "app.tasks.agri_lonlat.process_agri_optical_lonlat",
                    str(job.id),
                    countdown,
                )
            )
            jobs_dispatched += 1
            logger.info(
                "backfill_agri_optical_dispatched",
                job_id=str(job.id),
                land_id=land_id,
                chunk=f"{chunk_start} → {chunk_end}",
                countdown=countdown,
            )

        # Mark sentinel job as completed now that real jobs are created
        if sentinel_job_id:
            sentinel = session.get(Job, uuid.UUID(sentinel_job_id))
            if sentinel:
                sentinel.status = "completed"

        # Commit Job rows BEFORE Celery workers can see them (avoids Job not found).
        session.commit()
        for task_name, job_id, countdown in pending_sends:
            celery_app.send_task(task_name, args=[job_id], countdown=countdown)

        # Sentinel-1 GRD: agri writes lonlat_v1 (no index TIFs unless opt-in)
        s1_result = None
        try:
            from app.tasks.sentinel1 import backfill_s1_for_land

            async_result = backfill_s1_for_land.delay(
                land_id,
                months=months,
                force=force,
                mq_task_id=mq_task_id,
                date_from=start_date.isoformat(),
                date_to=end_date.isoformat(),
                processing_window_km=processing_window_km,
            )
            s1_result = {"task_id": async_result.id, "status": "queued"}
            logger.info("s1_backfill_dispatched", land_id=land_id, result=s1_result)
        except Exception as e:
            logger.warning(
                "s1_backfill_dispatch_failed", land_id=land_id, error=str(e)
            )

        logger.info(
            "backfill_orchestration_complete",
            land_id=land_id,
            chunks=len(chunks),
            jobs_dispatched=jobs_dispatched,
            force=force,
            s1=s1_result,
        )
        return {
            "land_id": land_id,
            "status": "dispatched",
            "jobs": jobs_dispatched,
            "chunks": len(chunks),
            "indices": ["agri_optical"],
            "force": force,
            "s1": s1_result,
        }

    except Exception as e:
        logger.error("backfill_orchestration_failed", land_id=land_id, error=str(e))
        session.rollback()
        raise
    finally:
        session.close()


def _backfill_indices_http_only(
    *,
    land_id: str,
    months: int | None,
    sentinel_job_id: str | None,
    force: bool,
    mq_task_id: str | None,
    date_from: str | None,
    date_to: str | None,
    growing_seasons: list | None,
    season_months: list | None,
    processing_window_km: float | None,
) -> dict:
    """Orchestrate canonical optical chunks without a local database session."""
    from app.core.http_mode import patch_job_http, resolve_land_http

    months = months or settings.index_backfill_months
    chunk_days = settings.index_backfill_chunk_days

    try:
        resolved = resolve_land_http(land_id)
    except Exception as e:
        logger.error(
            "backfill_orchestration_failed",
            land_id=land_id,
            error=f"lands/resolve failed: {e}",
        )
        raise

    if not resolved or not resolved.get("land_id"):
        raise RuntimeError(f"land parcel not found: {land_id}")
    if not is_scheduled_land_allowed(
        resolved.get("base_id"), resolved.get("land_area_mu")
    ):
        # 编排层先截断过滤地块，避免后续再创建多个光学分片和S1任务。
        if sentinel_job_id:
            patch_job_http(
                sentinel_job_id,
                {
                    "status": "cancelled",
                    "error": "定时任务地块过滤：基地被排除或地块面积超过5000亩",
                },
            )
        return {
            "land_id": land_id,
            "status": "cancelled",
            "reason": "land_filtered",
            "http_only": True,
        }
    if sentinel_job_id:
        try:
            from app.core.http_mode import get_job_http

            existing = get_job_http(sentinel_job_id)
            if existing and existing.get("status") in {"completed", "failed"}:
                return {
                    "land_id": land_id,
                    "status": "already_handled",
                    "sentinel_job_id": sentinel_job_id,
                    "http_only": True,
                }
        except Exception:
            pass

    end_date = date.fromisoformat(date_to) if date_to else date.today()
    if date_from:
        start_date = date.fromisoformat(date_from)
    else:
        start_date = end_date - timedelta(days=months * 30)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    chunks = _date_chunks(start_date, end_date, chunk_days)
    stagger_seconds = 30
    jobs_dispatched = 0
    try:
        for chunk_idx, (chunk_start, chunk_end) in enumerate(chunks):
            countdown = chunk_idx * stagger_seconds
            celery_app.send_task(
                "app.tasks.agri_lonlat.process_agri_optical_lonlat",
                kwargs={
                    "land_id": land_id,
                    "date_from": chunk_start.isoformat(),
                    "date_to": chunk_end.isoformat(),
                    "force": bool(force),
                    "mq_task_id": mq_task_id,
                    "growing_seasons": growing_seasons,
                    "season_months": season_months,
                    "processing_window_km": processing_window_km,
                    "is_backfill": True,
                },
                countdown=countdown,
            )
            jobs_dispatched += 1
            logger.info(
                "backfill_agri_optical_dispatched",
                land_id=land_id,
                chunk=f"{chunk_start} → {chunk_end}",
                countdown=countdown,
                http_only=True,
            )

        if sentinel_job_id:
            try:
                patch_job_http(
                    sentinel_job_id,
                    {"status": "completed", "touch_finished": True},
                )
            except Exception as e:
                logger.warning(
                    "backfill_sentinel_patch_failed",
                    sentinel_job_id=sentinel_job_id,
                    error=str(e),
                )

        s1_result = None
        try:
            from app.tasks.sentinel1 import backfill_s1_for_land

            async_result = backfill_s1_for_land.delay(
                land_id,
                months=months,
                force=force,
                mq_task_id=mq_task_id,
                date_from=start_date.isoformat(),
                date_to=end_date.isoformat(),
                processing_window_km=processing_window_km,
            )
            s1_result = {"task_id": async_result.id, "status": "queued"}
            logger.info(
                "s1_backfill_dispatched",
                land_id=land_id,
                result=s1_result,
                http_only=True,
            )
        except Exception as e:
            logger.warning(
                "s1_backfill_dispatch_failed", land_id=land_id, error=str(e)
            )

        logger.info(
            "backfill_orchestration_complete",
            land_id=land_id,
            chunks=len(chunks),
            jobs_dispatched=jobs_dispatched,
            force=force,
            s1=s1_result,
            http_only=True,
        )
        return {
            "land_id": land_id,
            "status": "dispatched",
            "jobs": jobs_dispatched,
            "chunks": len(chunks),
            "indices": ["agri_optical"],
            "force": force,
            "s1": s1_result,
            "http_only": True,
        }
    except Exception as e:
        logger.error("backfill_orchestration_failed", land_id=land_id, error=str(e))
        raise


# ── 每周自动补算指数 ──────────────────────────────────────────────


def _schedule_via_http() -> bool:
    """下载机已配置 Internal HTTP 时，地块清单必须向 API 要。"""
    try:
        from agric_satellite_analysis_common.internal_api import internal_api_enabled

        return internal_api_enabled()
    except ImportError:
        return False


def _dispatch_weekly_index_items(items: list) -> int:
    """把 API 准备好的 10km 动态分组任务及天气补拉投到本机 Celery broker。"""
    jobs_dispatched = 0
    weather_dispatched: set[str] = set()
    for item in items:
        task_name = item.get("task_name") if isinstance(item, dict) else None
        job_id = item.get("job_id") if isinstance(item, dict) else None
        if not task_name or not job_id:
            continue
        countdown = int(item.get("countdown") or 0)
        if task_name == "app.tasks.satellite_batch.process_satellite_batch":
            celery_app.send_task(task_name, args=[str(job_id)], countdown=countdown)
            for window in item.get("weather_windows") or []:
                land_id = str(window.get("land_id") or "")
                if not land_id or land_id in weather_dispatched:
                    continue
                weather_dispatched.add(land_id)
                celery_app.send_task(
                    "app.tasks.weather.backfill_weather_for_land",
                    args=[land_id],
                    kwargs={
                        "date_from": window["date_from"],
                        "date_to": window["date_to"],
                    },
                    countdown=countdown,
                )
            jobs_dispatched += 1
            continue
        # 下载机没有 API 数据库连接，必须把规范 land_id 和日期窗口直接
        # 传给 HTTP-only worker；不再把 API 端的 Job/旧身份当作地块键。
        if task_name == "app.tasks.agri_lonlat.process_agri_optical_lonlat":
            celery_app.send_task(
                task_name,
                kwargs={
                    "job_id": str(job_id),
                    "land_id": str(item["land_id"]),
                    "date_from": item["date_from"],
                    "date_to": item["date_to"],
                    "processing_window_km": item.get("processing_window_km"),
                    "is_backfill": True,
                },
                countdown=countdown,
            )
            # 每周遥感补算使用的窗口也作为天气回填窗口，避免定时任务的
            # 天气数据退化成固定最近几天，导致遥感/天气时间范围不一致。
            celery_app.send_task(
                "app.tasks.weather.backfill_weather_for_land",
                args=[str(item["land_id"])],
                kwargs={
                    "date_from": item["date_from"],
                    "date_to": item["date_to"],
                },
                countdown=countdown,
            )
        else:
            celery_app.send_task(task_name, args=[str(job_id)], countdown=countdown)
        jobs_dispatched += 1
    return jobs_dispatched


@celery_app.task(
    name="app.tasks.backfill.schedule_weekly_index_compute",
    bind=True,
    max_retries=1,
    time_limit=300,
    soft_time_limit=240,
)
def schedule_weekly_index_compute(self) -> dict:
    """给过期的规范地块派发统一光学任务。"""
    if _schedule_via_http():
        # 下载机只拿清单并投递，Job 已在 API 建好
        from agric_satellite_analysis_common.internal_api import weekly_index_prepare

        payload = weekly_index_prepare()
        items = payload.get("items") or []
        jobs_dispatched = _dispatch_weekly_index_items(items)
        logger.info(
            "weekly_index_compute_complete",
            lands_checked=payload.get("lands_checked"),
            lands_dispatched=payload.get("lands_dispatched"),
            jobs_dispatched=jobs_dispatched,
            http=True,
        )
        return {
            "status": "completed",
            "lands_checked": payload.get("lands_checked"),
            "lands_dispatched": payload.get("lands_dispatched"),
            "jobs_dispatched": jobs_dispatched,
            "http": True,
        }

    # 地块规划与 Job 都由 API 数据库维护；缺少 Internal HTTP 时必须显式失败，
    # 不能退回旧的逐地块下载，避免环境配置差异造成两套遥感数据链路。
    raise RuntimeError(
        "weekly satellite scheduling requires Internal HTTP for 10km grouping"
    )


# ── Bulk backfill (all existing lands) ───────────────────────────────


@celery_app.task(
    name="app.tasks.backfill.backfill_all_existing_lands",
    bind=True,
    max_retries=1,
    time_limit=300,
    soft_time_limit=240,
)
def backfill_all_existing_lands(self, months: int | None = None) -> dict:
    """Iterate all active land parcels and dispatch backfill for each one.

    Used as a one-time migration task for existing deployments that
    were set up before the auto-backfill feature.
    """
    from app.models.tables import LandParcel

    months = months or settings.index_backfill_months
    stagger_seconds = 60  # 1 minute between land parcels to spread load

    session = get_db_session()
    try:
        lands = (
            session.execute(
                select(LandParcel).where(
                    LandParcel.deleted_at.is_(None),
                    or_(
                        LandParcel.base_id.is_(None),
                        LandParcel.base_id.notin_(EXCLUDED_SCHEDULE_BASE_IDS),
                    ),
                    or_(
                        LandParcel.land_area_mu.is_(None),
                        LandParcel.land_area_mu <= MAX_SCHEDULE_LAND_AREA_MU,
                    ),
                )
            )
            .scalars()
            .all()
        )

        dispatched = 0
        for land in lands:
            backfill_indices_for_land.apply_async(
                args=[str(land.land_id)],
                kwargs={"months": months},
                countdown=dispatched * stagger_seconds,
            )
            dispatched += 1

        logger.info(
            "bulk_backfill_dispatched",
            total_lands=len(lands),
            dispatched=dispatched,
            months=months,
        )
        return {
            "status": "dispatched",
            "total_lands": len(lands),
            "dispatched": dispatched,
            "months": months,
        }

    except Exception as e:
        logger.error("bulk_backfill_failed", error=str(e))
        session.rollback()
        raise
    finally:
        session.close()
