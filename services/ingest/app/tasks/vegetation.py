"""Vegetation index tasks - EVI, SAVI, NDWI, NDMI, NDRE, CIRE, MNDWI.

Each task follows the same 7-step pipeline as NDVI but uses the
index registry for formula selection, band resolution, and alert defaults.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

import structlog

from agric_satellite_analysis_common.scheduled_land_filter import (
    is_scheduled_land_allowed,
)
from app.worker import celery_app
from app.tasks.indices import get_index
from app.core.config import scene_max_workers
from app.core.geo import geojson_to_shape
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


# ── Generic index processor ──────────────────────────────────────────


def _run_index_pipeline(self, job_id: str, index_key: str) -> dict:
    """Shared entry-point logic for any vegetation-index task."""
    from app.models.tables import Job, LandParcel, FieldStat

    index_def = get_index(index_key)
    session = get_db_session()
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            logger.error("job_not_found", job_id=job_id, index=index_key)
            return {"job_id": job_id, "status": "error", "detail": "Job not found"}

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.progress_json = {"current_step": "scene_search", "steps": {}}
        session.commit()

        land = session.get(LandParcel, job.land_id)
        if not land or land.deleted_at is not None:
            job.status = "failed"
            job.error = "Land parcel not found"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"job_id": job_id, "status": "failed"}
        if not is_scheduled_land_allowed(land.base_id, land.land_area_mu):
            job.status = "cancelled"
            job.error = "定时任务地块过滤：基地被排除或地块面积超过5000亩"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"job_id": job_id, "status": "cancelled", "reason": "land_filtered"}

        land_geom = geojson_to_shape(land.boundary_geojson)
        if land_geom is None:
            job.status = "failed"
            job.error = "Land parcel boundary is missing or invalid"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"job_id": job_id, "status": "failed"}
        land_geom_geojson = mapping(land_geom)

        params = job.params_json or {}
        date_from = date.fromisoformat(params["date_from"])
        date_to = date.fromisoformat(params["date_to"])
        org_id_str = "default"  # STORAGE_TENANT; auth/orgs removed
        land_id_str = str(job.land_id)

        # Extra params (e.g. savi_l)
        extra_params = {
            k: v for k, v in params.items() if k not in ("date_from", "date_to")
        }

        # Step 1: Scene Search
        update_job_progress(session, job, "scene_search")
        scenes = search_scenes(land_geom_geojson, date_from, date_to, index_def)
        force = bool(params.get("force") or False)
        if not force:
            existing = collect_existing_scene_dates(
                session,
                land_id_str,
                layer_type=index_def.label,
                satellite="S2",
                agri_sensor="S2")
            before = len(scenes)
            scenes = filter_scenes_skip_existing(
                scenes,
                existing,
                force=False,
                land_id=land_id_str,
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
            land_geom.bounds, land_geom
        )

        # Historical means for alerts
        existing_stats = (
            session.execute(
                select(FieldStat.mean)
                .where(FieldStat.land_id == job.land_id)
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
            land_id_str=land_id_str,
            date_from=date_from,
            date_to=date_to,
            historical_means=historical_means,
            extra_params=extra_params)

        session.expire(job)
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            logger.error(
                "job_missing_after_scenes", job_id=job_id, index=index_key
            )
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

        logger.info(
            f"{index_key}_job_completed",
            job_id=job_id,
            layers_created=layers_created)
        return {
            "job_id": job_id,
            "status": "completed",
            "layers_created": layers_created,
        }

    except Exception as e:
        logger.error(f"{index_key}_job_failed", job_id=job_id, error=str(e))
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


# ── EVI ──────────────────────────────────────────────────────────────


@celery_app.task(
    name="app.tasks.vegetation.process_evi",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500)
def process_evi(self, job_id: str) -> dict:
    """Process EVI for one land parcel."""
    return _run_index_pipeline(self, job_id, "evi")


# ── SAVI ─────────────────────────────────────────────────────────────


@celery_app.task(
    name="app.tasks.vegetation.process_savi",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500)
def process_savi(self, job_id: str) -> dict:
    """Process SAVI for one land parcel."""
    return _run_index_pipeline(self, job_id, "savi")


# ── NDWI ─────────────────────────────────────────────────────────────


@celery_app.task(
    name="app.tasks.vegetation.process_ndwi",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500)
def process_ndwi(self, job_id: str) -> dict:
    """Process NDWI for one land parcel."""
    return _run_index_pipeline(self, job_id, "ndwi")


# ── NDMI ─────────────────────────────────────────────────────────────


@celery_app.task(
    name="app.tasks.vegetation.process_ndmi",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500)
def process_ndmi(self, job_id: str) -> dict:
    """Process NDMI for one land parcel."""
    return _run_index_pipeline(self, job_id, "ndmi")


# ── NDRE ─────────────────────────────────────────────────────────────


@celery_app.task(
    name="app.tasks.vegetation.process_ndre",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500)
def process_ndre(self, job_id: str) -> dict:
    """Process NDRE for one land parcel."""
    return _run_index_pipeline(self, job_id, "ndre")


# ── CIRE ─────────────────────────────────────────────────────────────


@celery_app.task(
    name="app.tasks.vegetation.process_cire",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500)
def process_cire(self, job_id: str) -> dict:
    """Process CIre (chlorophyll index red-edge) for one land parcel."""
    return _run_index_pipeline(self, job_id, "cire")


# ── MNDWI ────────────────────────────────────────────────────────────


@celery_app.task(
    name="app.tasks.vegetation.process_mndwi",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500)
def process_mndwi(self, job_id: str) -> dict:
    """Process MNDWI for one land parcel."""
    return _run_index_pipeline(self, job_id, "mndwi")
