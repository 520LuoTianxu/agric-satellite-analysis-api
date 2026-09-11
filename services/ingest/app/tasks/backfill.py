"""Historical index backfill - orchestration layer.

Chunks a multi-month date range into segments, creates one Job per
(chunk × index) pair, and dispatches them with staggered countdowns
to avoid overwhelming the STAC API.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy import select

import structlog

from app.core.config import settings
from app.tasks.indices import INDEX_REGISTRY, INDEX_TASK_MAP
from app.tasks.pipeline import get_db_session
from app.worker import celery_app

logger = structlog.get_logger()


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
    name="app.tasks.backfill.backfill_indices_for_field",
    bind=True,
    max_retries=1,
    time_limit=120,
    soft_time_limit=90,
)
def backfill_indices_for_field(
    self,
    field_id: str,
    months: int | None = None,
    sentinel_job_id: str | None = None,
    allow_agri: bool = False,
    indices: list[str] | None = None,
    force: bool = False,
    mq_task_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    growing_seasons: list | None = None,
    season_months: list | None = None,
) -> dict:
    """Backfill vegetation indices for *field_id* over *months*.

    Splits the date range into 90-day chunks and dispatches one
    pipeline job per (chunk × index) with staggered countdowns.

    ``allow_agri``: run even for agri-tagged fields. Agri uses the lonlat-direct
    optical path (no index COG uploads) plus Sentinel-1 lonlat.
    ``indices``: optional subset of registry keys (classic COG path only).
    ``force``: passed to workers; when true they re-download/reprocess even if dates exist.
    """
    from app.models.tables import Field, Job

    months = months or settings.index_backfill_months
    chunk_days = settings.index_backfill_chunk_days

    session = get_db_session()
    try:
        field = session.get(Field, uuid.UUID(field_id))
        if not field:
            logger.error("backfill_field_not_found", field_id=field_id)
            return {
                "field_id": field_id,
                "status": "error",
                "detail": "Field not found",
            }

        from app.core.agri_tags import is_agri_tagged, parse_agri_land_id

        agri_field = is_agri_tagged(field.tags_json)
        if agri_field and not allow_agri:
            land_id = parse_agri_land_id(field.tags_json)
            logger.info(
                "backfill_indices_skipped_agri_field",
                field_id=field_id,
                land_id=land_id,
                reason="RS from agri.parcel_scene_products (lonlat_v1), not COG backfill",
            )
            if sentinel_job_id:
                sentinel = session.get(Job, uuid.UUID(sentinel_job_id))
                if sentinel:
                    sentinel.status = "completed"
                    params = dict(sentinel.params_json or {})
                    params["skipped"] = True
                    params["reason"] = "agri_tagged"
                    sentinel.params_json = params
                    session.commit()
            return {
                "field_id": field_id,
                "status": "skipped",
                "reason": "agri_tagged",
                "land_id": land_id,
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
        index_keys: list[str] = []

        pending_sends: list[tuple[str, str, int]] = []
        if agri_field:
            # One optical job per date chunk: bands -> indices -> lonlat_v1.
            # Do not dispatch per-index COG workers for agri parcels.
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
                }
                job = Job(
                    field_id=field.id,
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
                    field_id=field_id,
                    chunk=f"{chunk_start} → {chunk_end}",
                    countdown=countdown,
                )
            index_keys = ["agri_optical"]
        else:
            if indices:
                wanted = [k.lower() for k in indices]
                unknown = [k for k in wanted if k not in INDEX_REGISTRY]
                if unknown:
                    return {
                        "field_id": field_id,
                        "status": "error",
                        "detail": f"Unknown indices: {unknown}",
                    }
                index_keys = wanted
            else:
                index_keys = list(INDEX_REGISTRY.keys())

            # Always dispatch chunk jobs; workers skip per-scene dates already present
            # (force=True still reprocesses). Coarse chunk skip left gaps unfilled.
            for chunk_idx, (chunk_start, chunk_end) in enumerate(chunks):
                for idx_key in sorted(index_keys):
                    task_name = INDEX_TASK_MAP.get(idx_key)
                    if not task_name:
                        continue

                    params_json = {
                        "date_from": chunk_start.isoformat(),
                        "date_to": chunk_end.isoformat(),
                        "is_backfill": True,
                        "force": bool(force),
                    }

                    job = Job(
                        field_id=field.id,
                        type=idx_key,
                        status="pending",
                        params_json=params_json,
                    )
                    session.add(job)
                    session.flush()

                    countdown = chunk_idx * stagger_seconds
                    pending_sends.append((task_name, str(job.id), countdown))
                    jobs_dispatched += 1

                    logger.info(
                        "backfill_job_dispatched",
                        job_id=str(job.id),
                        field_id=field_id,
                        index=idx_key,
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
            from app.tasks.sentinel1 import backfill_s1_for_field

            async_result = backfill_s1_for_field.delay(
                field_id,
                months=months,
                force=force,
                mq_task_id=mq_task_id,
                date_from=start_date.isoformat(),
                date_to=end_date.isoformat(),
            )
            s1_result = {"task_id": async_result.id, "status": "queued"}
            logger.info("s1_backfill_dispatched", field_id=field_id, result=s1_result)
        except Exception as e:
            logger.warning(
                "s1_backfill_dispatch_failed", field_id=field_id, error=str(e)
            )

        logger.info(
            "backfill_orchestration_complete",
            field_id=field_id,
            chunks=len(chunks),
            indices=len(index_keys),
            jobs_dispatched=jobs_dispatched,
            allow_agri=allow_agri,
            force=force,
            s1=s1_result,
        )
        return {
            "field_id": field_id,
            "status": "dispatched",
            "jobs": jobs_dispatched,
            "chunks": len(chunks),
            "indices": index_keys,
            "allow_agri": allow_agri,
            "force": force,
            "s1": s1_result,
        }

    except Exception as e:
        logger.error("backfill_orchestration_failed", field_id=field_id, error=str(e))
        session.rollback()
        raise
    finally:
        session.close()


# ── Weekly auto-compute ──────────────────────────────────────────────


@celery_app.task(
    name="app.tasks.backfill.schedule_weekly_index_compute",
    bind=True,
    max_retries=1,
    time_limit=300,
    soft_time_limit=240,
)
def schedule_weekly_index_compute(self) -> dict:
    """Query all active fields, skip fresh ones, dispatch index jobs for stale ones.

    A field is considered *stale* if its latest raster layer is older than 7 days.
    """
    from sqlalchemy import func as sqla_func

    from app.models.tables import Field, Job, RasterLayer

    session = get_db_session()
    batch_size = settings.index_weekly_batch_size
    stale_threshold = date.today() - timedelta(days=7)

    try:
        from app.core.agri_tags import is_agri_tagged

        # Fetch all active fields (skip agri: they use lonlat-direct, not COGs)
        field_rows = session.execute(
            select(Field.id, Field.tags_json).where(Field.deleted_at.is_(None))
        ).all()

        fields_checked = 0
        fields_dispatched = 0
        jobs_dispatched = 0
        skipped_agri = 0
        stagger_seconds = 15

        for batch_start in range(0, len(field_rows), batch_size):
            batch = field_rows[batch_start : batch_start + batch_size]

            for field_id, tags_json in batch:
                fields_checked += 1
                if is_agri_tagged(tags_json):
                    skipped_agri += 1
                    continue

                # Check staleness: latest raster layer date
                latest_date = session.execute(
                    select(sqla_func.max(RasterLayer.date)).where(
                        RasterLayer.field_id == field_id
                    )
                ).scalar_one_or_none()

                if latest_date is not None and latest_date > stale_threshold:
                    continue  # fresh - skip

                # Determine date range: latest_date+1 → today (or 7 days back if none)
                date_from = (
                    (latest_date + timedelta(days=1))
                    if latest_date
                    else (date.today() - timedelta(days=7))
                )
                date_to = date.today()
                if date_from >= date_to:
                    continue

                # Dispatch one job per index
                for idx_key in sorted(INDEX_REGISTRY.keys()):
                    task_name = INDEX_TASK_MAP.get(idx_key)
                    if not task_name:
                        continue

                    job = Job(
                        field_id=field_id,
                        type=idx_key,
                        status="pending",
                        params_json={
                            "date_from": date_from.isoformat(),
                            "date_to": date_to.isoformat(),
                        },
                    )
                    session.add(job)
                    session.flush()

                    countdown = fields_dispatched * stagger_seconds
                    celery_app.send_task(
                        task_name, args=[str(job.id)], countdown=countdown
                    )
                    jobs_dispatched += 1

                fields_dispatched += 1

        session.commit()

        logger.info(
            "weekly_index_compute_complete",
            fields_checked=fields_checked,
            fields_dispatched=fields_dispatched,
            jobs_dispatched=jobs_dispatched,
            skipped_agri=skipped_agri,
        )
        return {
            "status": "completed",
            "fields_checked": fields_checked,
            "fields_dispatched": fields_dispatched,
            "jobs_dispatched": jobs_dispatched,
            "skipped_agri": skipped_agri,
        }

    except Exception as e:
        logger.error("weekly_index_compute_failed", error=str(e))
        session.rollback()
        raise
    finally:
        session.close()


# ── Bulk backfill (all existing fields) ──────────────────────────────


@celery_app.task(
    name="app.tasks.backfill.backfill_all_existing_fields",
    bind=True,
    max_retries=1,
    time_limit=300,
    soft_time_limit=240,
)
def backfill_all_existing_fields(self, months: int | None = None) -> dict:
    """Iterate all active fields and dispatch backfill for each one.

    Used as a one-time migration task for existing deployments that
    were set up before the auto-backfill feature.
    """
    from app.models.tables import Field

    months = months or settings.index_backfill_months
    stagger_seconds = 60  # 1 minute between fields to spread load

    session = get_db_session()
    try:
        from app.core.agri_tags import is_agri_tagged

        fields = (
            session.execute(select(Field).where(Field.deleted_at.is_(None)))
            .scalars()
            .all()
        )

        dispatched = 0
        skipped_agri = 0
        for field in fields:
            if is_agri_tagged(field.tags_json):
                skipped_agri += 1
                continue
            backfill_indices_for_field.apply_async(
                args=[str(field.id)],
                kwargs={"months": months},
                countdown=dispatched * stagger_seconds,
            )
            dispatched += 1

        logger.info(
            "bulk_backfill_dispatched",
            total_fields=len(fields),
            dispatched=dispatched,
            skipped_agri=skipped_agri,
            months=months,
        )
        return {
            "status": "dispatched",
            "total_fields": len(fields),
            "dispatched": dispatched,
            "skipped_agri": skipped_agri,
            "months": months,
        }

    except Exception as e:
        logger.error("bulk_backfill_failed", error=str(e))
        session.rollback()
        raise
    finally:
        session.close()
