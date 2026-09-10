"""Celery tasks: bridge STAC COGs → agri.parcel_scene_products lonlat_v1."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select

from app.tasks.pipeline import get_db_session
from app.worker import celery_app

logger = structlog.get_logger()


def _dispatch_agri_alerts(field_id: str, land_id: str | None = None) -> None:
    """Fire-and-forget RS alert re-eval after lonlat upsert."""
    try:
        from app.tasks.agri_alerts import evaluate_agri_alerts_for_field

        evaluate_agri_alerts_for_field.delay(
            field_id, land_id=land_id, replace_open=True
        )
    except Exception as e:
        logger.warning(
            "agri_alerts_dispatch_failed",
            field_id=field_id,
            error=str(e),
        )


def _publish_mq_result(
    mq_task_id: str,
    *,
    status: str,
    field_id: str | None = None,
    land_id: str | None = None,
    error: str | None = None,
    extras: dict | None = None,
    oss_urls: dict | None = None,
) -> None:
    """Best-effort CloudAMQP ResultMessage publish (outer scheduling bus)."""
    try:
        from openfarm_common.mq_results import publish_task_result

        publish_task_result(
            task_id=mq_task_id,
            status=status,
            field_id=field_id,
            land_id=land_id,
            error=error,
            extras=extras,
            oss_urls=oss_urls,
            collect_parcel_urls=True,
            upload_summary_if_empty=not bool(oss_urls),
        )
    except Exception as e:
        logger.warning(
            "mq_result_publish_failed",
            mq_task_id=mq_task_id,
            error=str(e),
        )


@celery_app.task(
    name="app.tasks.agri_bridge.bridge_field_stac_to_agri",
    bind=True,
    max_retries=2,
    time_limit=1800,
    soft_time_limit=1500,
)
def bridge_field_stac_to_agri_task(
    self,
    field_id: str,
    land_id: str | None = None,
    mq_task_id: str | None = None,
) -> dict:
    """Sample active-store (OSS) COGs for field and upsert agri lonlat_v1."""
    from app.tasks.bridge_stac_cogs_to_agri_lonlat import bridge_field_stac_to_agri

    try:
        result = bridge_field_stac_to_agri(field_id, land_id, quiet=True)
        logger.info(
            "bridge_field_stac_to_agri_done",
            field_id=field_id,
            land_id=result.get("land_id"),
            upserted=result.get("upserted"),
            skipped=result.get("skipped"),
        )
        _dispatch_agri_alerts(field_id, land_id=result.get("land_id") or land_id)
        if mq_task_id:
            _publish_mq_result(
                mq_task_id,
                status="success",
                field_id=field_id,
                land_id=result.get("land_id") or land_id,
                extras={
                    "upserted": result.get("upserted"),
                    "source": "bridge_field",
                    "oss_key_count": len(result.get("oss_urls") or {}),
                },
                oss_urls=result.get("oss_urls") or None,
            )
        return result
    except Exception as e:
        logger.error(
            "bridge_field_stac_to_agri_failed",
            field_id=field_id,
            error=str(e),
        )
        if mq_task_id:
            _publish_mq_result(
                mq_task_id,
                status="failed",
                field_id=field_id,
                land_id=land_id,
                error=str(e)[:500],
            )
        raise


@celery_app.task(
    name="app.tasks.agri_bridge.bridge_after_backfill",
    bind=True,
    max_retries=90,
    default_retry_delay=60,
    # Exists-probe loops over many COG keys routinely exceed 90s; keep retries
    # for unfinished backfill jobs but allow a longer soft window per attempt.
    time_limit=720,
    soft_time_limit=600,
)
def bridge_after_backfill(
    self,
    field_id: str,
    land_id: str | None = None,
    bridge_job_id: str | None = None,
    mq_task_id: str | None = None,
) -> dict:
    """Wait until index backfill jobs finish, then bridge COGs → agri lonlat_v1.

    Also bridges immediately-available COGs on first attempt so existing
    layers appear without waiting for the full STAC refresh.
    """
    from datetime import datetime, timedelta, timezone

    from app.models.tables import Job

    session = get_db_session()
    bridge_job = None
    try:
        if bridge_job_id:
            bridge_job = session.get(Job, uuid.UUID(bridge_job_id))
            if bridge_job and bridge_job.status == "pending":
                bridge_job.status = "running"
                bridge_job.started_at = datetime.now(timezone.utc)
                session.commit()

        # First attempt (or retries): bridge whatever COGs exist now
        from app.tasks.bridge_stac_cogs_to_agri_lonlat import bridge_field_stac_to_agri

        # First attempt: bridge whatever COGs already exist
        if self.request.retries == 0:
            try:
                bridge_field_stac_to_agri(field_id, land_id, quiet=True)
            except Exception as e:
                logger.warning(
                    "bridge_after_backfill_early_bridge_failed",
                    field_id=field_id,
                    error=str(e),
                )

        # Only wait on current-wave index jobs (not sentinel/bridge; not ancient stuck)
        wave_cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        if bridge_job and bridge_job.created_at:
            wave_cutoff = bridge_job.created_at

        active = (
            session.execute(
                select(Job.id).where(
                    Job.field_id == uuid.UUID(field_id),
                    Job.status.in_(["pending", "running"]),
                    Job.params_json["is_backfill"].as_boolean().is_(True),
                    Job.type.notin_(["backfill", "agri_bridge"]),
                    Job.created_at >= wave_cutoff,
                )
            )
            .scalars()
            .all()
        )
        if active:
            logger.info(
                "bridge_after_backfill_waiting",
                field_id=field_id,
                active=len(active),
                retry=self.request.retries,
            )
            raise self.retry(countdown=60)

        result = bridge_field_stac_to_agri(field_id, land_id, quiet=True)
        logger.info(
            "bridge_after_backfill_complete",
            field_id=field_id,
            upserted=result.get("upserted"),
        )
        _dispatch_agri_alerts(field_id, land_id=land_id or result.get("land_id"))
        if bridge_job:
            bridge_job = session.get(Job, uuid.UUID(bridge_job_id))
            if bridge_job:
                bridge_job.status = "completed"
                bridge_job.finished_at = datetime.now(timezone.utc)
                params = dict(bridge_job.params_json or {})
                params["upserted"] = result.get("upserted")
                if mq_task_id:
                    params["mq_task_id"] = mq_task_id
                bridge_job.params_json = params
                session.commit()
        if mq_task_id:
            _publish_mq_result(
                mq_task_id,
                status="success",
                field_id=field_id,
                land_id=land_id or result.get("land_id"),
                extras={
                    "upserted": result.get("upserted"),
                    "source": "bridge_after_backfill",
                    "oss_key_count": len(result.get("oss_urls") or {}),
                },
                oss_urls=result.get("oss_urls") or None,
            )
        return result
    except Exception as e:
        # Don't mark failed on retry signals
        from celery.exceptions import Retry

        if isinstance(e, Retry):
            raise
        if bridge_job_id:
            try:
                bj = session.get(Job, uuid.UUID(bridge_job_id))
                if bj and bj.status in ("pending", "running"):
                    bj.status = "failed"
                    bj.error = str(e)[:500]
                    bj.finished_at = datetime.now(timezone.utc)
                    session.commit()
            except Exception:
                session.rollback()
        if mq_task_id:
            _publish_mq_result(
                mq_task_id,
                status="failed",
                field_id=field_id,
                land_id=land_id,
                error=str(e)[:500],
            )
        raise
    finally:
        session.close()
