"""Celery tasks: bridge STAC COGs → agri.parcel_scene_products lonlat_v1."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select

from app.tasks.pipeline import get_db_session
from app.worker import celery_app

logger = structlog.get_logger()


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
) -> dict:
    """Sample MinIO COGs for field and upsert agri lonlat_v1 scene products."""
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
        return result
    except Exception as e:
        logger.error(
            "bridge_field_stac_to_agri_failed",
            field_id=field_id,
            error=str(e),
        )
        raise


@celery_app.task(
    name="app.tasks.agri_bridge.bridge_after_backfill",
    bind=True,
    max_retries=90,
    default_retry_delay=60,
    time_limit=120,
    soft_time_limit=90,
)
def bridge_after_backfill(
    self,
    field_id: str,
    land_id: str | None = None,
) -> dict:
    """Wait until index backfill jobs finish, then bridge COGs → agri lonlat_v1.

    Also bridges immediately-available COGs on first attempt so existing
    layers appear without waiting for the full STAC refresh.
    """
    from app.models.tables import Job

    session = get_db_session()
    try:
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

        active = (
            session.execute(
                select(Job.id).where(
                    Job.field_id == uuid.UUID(field_id),
                    Job.status.in_(["pending", "running"]),
                    Job.params_json["is_backfill"].as_boolean().is_(True),
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
        return result
    finally:
        session.close()
