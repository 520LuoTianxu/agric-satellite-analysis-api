"""NDVI processing task - backward-compatible entry point.

Delegates to the shared vegetation-index pipeline (``pipeline.py``).
The Celery task name ``app.tasks.ndvi.process_ndvi`` is preserved so
existing jobs, imports, and the ``POST /jobs/ndvi`` endpoint keep working.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

import structlog

from app.worker import celery_app
from app.tasks.indices import get_index
from app.core.config import scene_max_workers
from app.tasks.pipeline import (
    RETRY_DELAYS,
    get_db_session,
    update_job_progress,
    complete_step,
    search_scenes,
    compute_target_grid,
    process_scenes_parallel,
    collect_existing_scene_dates,
    filter_scenes_skip_existing)

logger = structlog.get_logger()


@celery_app.task(
    name="app.tasks.ndvi.process_ndvi",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500)
def process_ndvi(self, job_id: str) -> dict:
    """Process NDVI for a field - delegates to the shared pipeline."""
    from app.models.tables import Job, Field, FieldStat

    index_def = get_index("ndvi")
    session = get_db_session()
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            logger.error("job_not_found", job_id=job_id)
            return {"job_id": job_id, "status": "error", "detail": "Job not found"}

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.progress_json = {"current_step": "scene_search", "steps": {}}
        session.commit()

        field = session.get(Field, job.field_id)
        if not field:
            job.status = "failed"
            job.error = "Field not found"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"job_id": job_id, "status": "failed"}

        field_geom = to_shape(field.geom)
        field_geom_geojson = mapping(field_geom)

        params = job.params_json or {}
        date_from = date.fromisoformat(params["date_from"])
        date_to = date.fromisoformat(params["date_to"])
        org_id_str = "default"  # STORAGE_TENANT; auth/orgs removed
        field_id_str = str(job.field_id)

        # Step 1: Scene Search
        update_job_progress(session, job, "scene_search")
        scenes = search_scenes(field_geom_geojson, date_from, date_to, index_def)
        force = bool(params.get("force") or False)
        if not force:
            existing = collect_existing_scene_dates(
                session,
                field,
                layer_type=index_def.label,
                satellite="S2",
                agri_sensor="S2")
            before = len(scenes)
            scenes = filter_scenes_skip_existing(
                scenes,
                existing,
                force=False,
                field_id=field_id_str,
                index=index_def.key)
            complete_step(
                session,
                job,
                "scene_search",
                {
                    "scene_count": before,
                    "scenes_after_dedup": len(scenes),
                    "skipped_existing": before - len(scenes),
                })
        else:
            complete_step(session, job, "scene_search", {"scene_count": len(scenes)})

        if not scenes:
            job.status = "completed"
            job.finished_at = datetime.now(timezone.utc)
            progress = job.progress_json or {}
            progress["current_step"] = "complete"
            progress["message"] = "No cloud-free scenes found for the date range."
            progress["layers_created"] = 0
            job.progress_json = progress
            flag_modified(job, "progress_json")
            session.commit()
            return {"job_id": job_id, "status": "completed", "scenes": 0}

        # Compute target grid
        target_transform, target_shape, field_mask, bounds = compute_target_grid(
            field_geom.bounds, field_geom
        )

        # Historical means for alerts
        existing_stats = (
            session.execute(
                select(FieldStat.mean)
                .where(FieldStat.field_id == job.field_id)
                .order_by(FieldStat.date.asc())
            )
            .scalars()
            .all()
        )
        historical_means = [float(m) for m in existing_stats if m is not None]

        workers = min(scene_max_workers(), len(scenes))
        update_job_progress(
            session,
            job,
            "process_scenes",
            {"total_scenes": len(scenes), "workers": workers})

        layers_created = process_scenes_parallel(
            job_id=job_id,
            scenes=scenes,
            index_def=index_def,
            target_transform=target_transform,
            target_shape=target_shape,
            field_mask=field_mask,
            bounds=bounds,
            org_id_str=org_id_str,
            field_id_str=field_id_str,
            date_from=date_from,
            date_to=date_to,
            historical_means=historical_means)

        # Scene workers used their own sessions; reload this job row.
        session.expire(job)
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            logger.error("job_missing_after_scenes", job_id=job_id)
            return {"job_id": job_id, "status": "error", "detail": "Job not found"}

        complete_step(
            session,
            job,
            "process_scenes",
            {"layers_created": layers_created, "workers": workers})

        # Step 7: Complete
        job.status = "completed"
        job.finished_at = datetime.now(timezone.utc)
        progress = job.progress_json or {}
        progress["current_step"] = "complete"
        progress["layers_created"] = layers_created
        progress["total_scenes"] = len(scenes)
        progress["scene_workers"] = workers
        job.progress_json = progress
        flag_modified(job, "progress_json")
        session.commit()

        logger.info("ndvi_job_completed", job_id=job_id, layers_created=layers_created)
        return {
            "job_id": job_id,
            "status": "completed",
            "layers_created": layers_created,
        }

    except Exception as e:
        logger.error("ndvi_job_failed", job_id=job_id, error=str(e))
        try:
            job = session.get(Job, uuid.UUID(job_id))
            if job:
                job.status = "failed"
                job.error = str(e)
                job.finished_at = datetime.now(timezone.utc)
                session.commit()
        except Exception:
            pass
        retry_num = self.request.retries
        if retry_num < len(RETRY_DELAYS):
            raise self.retry(exc=e, countdown=RETRY_DELAYS[retry_num])
        raise

    finally:
        session.close()
