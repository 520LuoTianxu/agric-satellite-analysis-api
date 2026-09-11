"""Agri optical path: STAC bands -> in-memory indices -> lonlat_v1 upsert.

Does **not** upload index COG/TIF rasters. Compact scene JSON may still go
to ``OSS_PREFIX`` (see ``UPLOAD_SCENE_JSON``).

Classic OpenFarm (non-agri) keeps per-index ``write_cog`` in ``pipeline.py``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import structlog
from geoalchemy2.shape import to_shape
from rasterio.warp import Resampling
from shapely.geometry import mapping
from sqlalchemy import text
from sqlalchemy.orm.attributes import flag_modified

from app.core.agri_classify import (
    PARCEL_CLOUD_SOURCE_LONLAT,
    PARCEL_CLOUD_SOURCE_SCL,
    SUSPICIOUS_STAC_MIN,
    SUSPICIOUS_STAC_OVER_PARCEL_GAP,
    parcel_cloud_from_lonlat_pixels,
    parcel_cloud_from_scl_values,
    scene_cloud_fields,
)
from app.core.band_parallel import band_max_workers
from app.core.config import scene_max_workers
from app.core.index_cogs import upload_scene_json_enabled, write_index_cogs_enabled
from app.tasks.indices import get_index
from app.tasks.pipeline import (
    RETRY_DELAYS,
    complete_step,
    compute_target_grid,
    existing_agri_scene_dates,
    filter_scenes_skip_existing,
    get_db_session,
    read_band_windowed,
    read_bands_windowed_parallel,
    search_scenes_for_defs,
    update_job_progress,
    write_cog,
)
from app.worker import celery_app

logger = structlog.get_logger()

# Pixel keys in lonlat_v1 (stable order; matches the OSS-COG bridge emit set).
AGRI_OPTICAL_INDEX_KEYS = ("ndvi", "evi", "ndmi", "ndre", "cire", "mndwi")
INDEX_KEY_TO_PIXEL = {
    "ndvi": "NDVI",
    "evi": "EVI",
    "ndmi": "NDMI",
    "ndre": "NDRE",
    "cire": "CIre",
    "mndwi": "MNDWI",
}

# Element84 / ESA SCL asset names. Optional; missing SCL falls back to STAC.
SCL_STAC_ASSETS = ("scl", "SCL")


def parcel_cloud_from_scl_window(
    scl: np.ndarray | None,
    field_mask: np.ndarray | None,
) -> float | None:
    """In-polygon SCL cloud/shadow fraction. Ignores nodata and window padding.

    Returns None (not 0) when SCL is missing, the polygon covers no cells,
    or every sample is nodata / not a Sen2Cor class 1-11.
    """
    if scl is None or field_mask is None:
        return None
    if getattr(scl, "shape", None) != getattr(field_mask, "shape", None):
        return None
    inside = field_mask & np.isfinite(scl)
    if not np.any(inside):
        return None
    classes = np.rint(scl[inside]).astype(np.int16)
    return parcel_cloud_from_scl_values(classes.tolist())


def agri_optical_index_defs():
    return [get_index(k) for k in AGRI_OPTICAL_INDEX_KEYS]


def _load_agri_meta(session, field) -> dict[str, Any]:
    from app.core.agri_tags import parse_agri_land_id

    land_id = parse_agri_land_id(getattr(field, "tags_json", None))
    if not land_id:
        raise RuntimeError("field is not agri-tagged (need agri:<land_id>)")
    row = (
        session.execute(
            text(
                "SELECT land_id, tile_id, land_name "
                "FROM agri.land_parcels WHERE land_id = :lid"
            ),
            {"lid": str(land_id)},
        )
        .mappings()
        .first()
    )
    if not row:
        raise RuntimeError(f"agri.land_parcels missing land_id={land_id}")
    return {
        "land_id": row["land_id"],
        "tile_id": row["tile_id"],
        "land_name": row["land_name"] or field.name,
        "field_id": str(field.id),
    }


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL_SYNC or DATABASE_URL required")
    if url.startswith("postgresql+"):
        url = "postgresql://" + url.split("://", 1)[1]
    return url


def count_parcel_scene_rows(session, land_id: str, sensor: str | None = None) -> int:
    if sensor:
        n = session.execute(
            text(
                "SELECT COUNT(*) FROM agri.parcel_scene_products "
                "WHERE land_id = :lid AND sensor = :sensor"
            ),
            {"lid": str(land_id), "sensor": sensor},
        ).scalar()
    else:
        n = session.execute(
            text(
                "SELECT COUNT(*) FROM agri.parcel_scene_products WHERE land_id = :lid"
            ),
            {"lid": str(land_id)},
        ).scalar()
    return int(n or 0)


def publish_optical_lonlat_to_oss_mq(
    row: dict[str, Any],
    *,
    mq_task_id: str | None = None,
    field_id: str | None = None,
    oss_sensor: str = "S2",
    extra_extras: dict[str, Any] | None = None,
) -> str | None:
    """Upload S2 lonlat JSON to OSS and publish one result MQ (no local PG upsert).

    Producer mq_result_writer pulls the OSS URL and writes agri.parcel_scene_products.
    ``oss_sensor`` only changes the object key / MQ label (e.g. ``S2_decloud``).
    The stored row stays ``sensor='S2'`` so existing clients keep working.
    """
    from openfarm_common.mq_results import (
        publish_task_result,
        scene_json_oss_key,
        upload_scene_product_json,
    )

    pixel_obj = row.pop("_pixel_data_obj", None)
    t_json = time.perf_counter()
    key = scene_json_oss_key(row["land_id"], row["date"], oss_sensor)
    product = {
        "land_id": row["land_id"],
        "tile_id": row["tile_id"],
        "date": row["date"],
        "sensor": "S2",
        "scene_id": row["scene_id"],
        "land_name": row["land_name"],
        "cloud_cover": row["cloud_cover"],
        "cloud_cover_over_30": row["cloud_cover_over_30"],
        "parcel_cloud_cover_pct": row["parcel_cloud_cover_pct"],
        "pixel_count": row["pixel_count"],
        "generated_at_shanghai": row["generated_at_shanghai"],
        "pixel_data_url": row["pixel_data_url"],
        "json_oss_key": key,
        "source": row.get("_source") or (pixel_obj or {}).get("source") or "stac_direct",
        "decloud_quality": row.get("_decloud_quality")
        or (pixel_obj or {}).get("decloud_quality"),
        "decloud_score": row.get("_decloud_score")
        or (pixel_obj or {}).get("decloud_score"),
        "ndvi_avg": row["ndvi_avg"],
        "ndvi_min": row["ndvi_min"],
        "ndvi_max": row["ndvi_max"],
        "evi_avg": row["evi_avg"],
        "evi_min": row["evi_min"],
        "evi_max": row["evi_max"],
        "ndmi_avg": row["ndmi_avg"],
        "ndmi_min": row["ndmi_min"],
        "ndmi_max": row["ndmi_max"],
        "ndre_avg": row["ndre_avg"],
        "ndre_min": row["ndre_min"],
        "ndre_max": row["ndre_max"],
        "cire_avg": row["cire_avg"],
        "cire_min": row["cire_min"],
        "cire_max": row["cire_max"],
        "mndwi_avg": row["mndwi_avg"],
        "mndwi_min": row["mndwi_min"],
        "mndwi_max": row["mndwi_max"],
        "pixel_data": pixel_obj or json.loads(row["pixel_data"]),
    }
    key, json_url = upload_scene_product_json(
        land_id=row["land_id"],
        date_str=row["date"],
        sensor=oss_sensor,
        product=product,
    )
    json_upload_ms = int((time.perf_counter() - t_json) * 1000)
    row["json_oss_key"] = key

    label = f"{row['date']}_{oss_sensor}"
    parent = (mq_task_id or "").strip() or None
    result_task_id = (
        f"{parent}:{label}" if parent else f"agri-scene:{row['land_id']}:{label}"
    )
    t_mq = time.perf_counter()
    extras: dict[str, Any] = {
        "kind": "parcel_scene_product",
        "sensor": "S2",
        "date": row["date"],
        "scene_id": row["scene_id"],
        "parent_mq_task_id": parent,
        "json_oss_key": key,
        "source": product.get("source"),
    }
    if extra_extras:
        extras.update(extra_extras)
    publish_task_result(
        task_id=result_task_id,
        status="success",
        land_id=str(row["land_id"]),
        field_id=field_id,
        oss_urls={label: json_url},
        collect_parcel_urls=False,
        upload_summary_if_empty=False,
        extras=extras,
    )
    mq_publish_ms = int((time.perf_counter() - t_mq) * 1000)
    logger.info(
        "lonlat_write_timing",
        land_id=row.get("land_id"),
        date=row.get("date"),
        sensor="S2",
        json_upload_ms=json_upload_ms,
        db_upsert_ms=0,
        mq_publish_ms=mq_publish_ms,
        uploaded_json=True,
        path="oss_mq",
    )
    logger.info(
        "lonlat_oss_mq_published",
        land_id=row.get("land_id"),
        date=row.get("date"),
        sensor="S2",
        json_url=json_url,
        result_task_id=result_task_id,
    )
    return json_url


# Back-compat alias (tests / call sites may still import the old name).
def upsert_optical_lonlat_row(
    row: dict[str, Any],
    *,
    mq_task_id: str | None = None,
    field_id: str | None = None,
) -> str | None:
    return publish_optical_lonlat_to_oss_mq(
        row, mq_task_id=mq_task_id, field_id=field_id
    )


def emit_optical_lonlat(
    *,
    meta: dict[str, Any],
    geom4326: dict,
    field_id_str: str,
    scene: dict,
    index_arrays: dict[str, np.ndarray],
    transform,
    parcel_cloud: float | None,
    parcel_cloud_source: str | None = None,
    scl: np.ndarray | None = None,
    mq_task_id: str | None = None,
) -> dict[str, Any] | None:
    """Sample lonlat_v1, upload OSS JSON, publish one MQ (PG write on producer)."""
    from app.tasks.bridge_stac_cogs_to_agri_lonlat import (
        _round6,
        _sample_lonlat,
        _stats,
    )

    if "NDVI" not in index_arrays:
        return None
    t_sample = time.perf_counter()
    pixels = _sample_lonlat(
        geom4326, index_arrays, transform, "EPSG:4326", scl=scl
    )
    sample_ms = int((time.perf_counter() - t_sample) * 1000)
    if not pixels:
        logger.info(
            "agri_lonlat_no_pixels",
            field_id=field_id_str,
            date=str(scene["date"]),
            scene_id=scene.get("id"),
        )
        return None

    source = parcel_cloud_source
    parcel = parcel_cloud
    if parcel is None:
        lonlat_cloud = parcel_cloud_from_lonlat_pixels(pixels)
        if lonlat_cloud is not None:
            parcel = lonlat_cloud
            source = PARCEL_CLOUD_SOURCE_LONLAT

    def _avg_triple(pix_key: str):
        if pix_key not in index_arrays:
            return None, None, None
        return _stats(index_arrays[pix_key])

    ndvi_avg, ndvi_min, ndvi_max = _avg_triple("NDVI")
    evi_avg, evi_min, evi_max = _avg_triple("EVI")
    ndmi_avg, ndmi_min, ndmi_max = _avg_triple("NDMI")
    ndre_avg, ndre_min, ndre_max = _avg_triple("NDRE")
    cire_avg, cire_min, cire_max = _avg_triple("CIre")
    mndwi_avg, mndwi_min, mndwi_max = _avg_triple("MNDWI")

    date_str = (
        scene["date"].isoformat()
        if isinstance(scene["date"], date)
        else str(scene["date"])[:10]
    )
    # Stable id so new writes update rows previously written by the OSS COG bridge.
    scene_id = f"stac_bridge_{date_str}_S2"
    cloud_f, cloud_over_30, parcel_cloud_raw = scene_cloud_fields(
        scene.get("cloud_cover"), parcel
    )
    # Do not persist a hard 0% parcel that under-reports Element84 eo:cloud_cover;
    # null lets UI/API fall back to STAC (matches earth-search).
    if (
        parcel_cloud_raw is not None
        and cloud_f is not None
        and cloud_f >= SUSPICIOUS_STAC_MIN
        and (cloud_f - parcel_cloud_raw) >= SUSPICIOUS_STAC_OVER_PARCEL_GAP
    ):
        parcel_cloud_raw = None
    if parcel_cloud_raw is None:
        source = None
    parcel_out = (
        _round6(parcel_cloud_raw) if parcel_cloud_raw is not None else None
    )

    pixel_data = {
        "format": "lonlat_v1",
        "source": "stac_direct",
        "pixels": pixels,
    }
    if source:
        pixel_data["parcel_cloud_source"] = source
    row = {
        "land_id": meta["land_id"],
        "tile_id": meta["tile_id"],
        "date": date_str,
        "scene_id": scene_id,
        "land_name": meta["land_name"],
        "cloud_cover": cloud_f,
        "cloud_cover_over_30": cloud_over_30,
        "parcel_cloud_cover_pct": parcel_out,
        "pixel_count": len(pixels),
        "generated_at_shanghai": datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S%z"
        ),
        "pixel_data_url": f"stac-direct://field/{field_id_str}/{date_str}",
        "ndvi_avg": ndvi_avg,
        "ndvi_min": ndvi_min,
        "ndvi_max": ndvi_max,
        "evi_avg": evi_avg,
        "evi_min": evi_min,
        "evi_max": evi_max,
        "ndmi_avg": ndmi_avg,
        "ndmi_min": ndmi_min,
        "ndmi_max": ndmi_max,
        "ndre_avg": ndre_avg,
        "ndre_min": ndre_min,
        "ndre_max": ndre_max,
        "cire_avg": cire_avg,
        "cire_min": cire_min,
        "cire_max": cire_max,
        "mndwi_avg": mndwi_avg,
        "mndwi_min": mndwi_min,
        "mndwi_max": mndwi_max,
        "pixel_data": json.dumps(pixel_data, separators=(",", ":")),
        "json_oss_key": None,
        "_pixel_data_obj": pixel_data,
    }
    json_url = publish_optical_lonlat_to_oss_mq(
        row, mq_task_id=mq_task_id, field_id=field_id_str
    )
    logger.info(
        "lonlat_upserted",
        land_id=meta["land_id"],
        date=date_str,
        sensor="S2",
        pixels=len(pixels),
        json_oss_key=row.get("json_oss_key"),
        json_url=json_url,
        sample_ms=sample_ms,
    )
    return {
        "date": date_str,
        "pixels": len(pixels),
        "json_oss_key": row.get("json_oss_key"),
        "json_url": json_url,
        "cloud_cover": cloud_f,
        "cloud_cover_over_30": cloud_over_30,
        "parcel_cloud_cover_pct": parcel_out,
        "parcel_cloud_source": source,
        "scene_id": scene_id,
        "stac_id": scene.get("id"),
    }


def _process_one_optical_scene(
    *,
    job_id: str,
    scene: dict,
    idx: int,
    total_scenes: int,
    index_defs,
    target_transform,
    target_shape: tuple,
    field_mask: np.ndarray,
    bounds: tuple,
    org_id_str: str,
    field_id_str: str,
    agri_meta: dict[str, Any],
    field_geom_geojson: dict,
    write_cogs: bool,
    scene_workers: int = 1,
    mq_task_id: str | None = None,
) -> dict[str, Any] | None:
    from app.models.tables import Job

    session = get_db_session()
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.error("job_not_found_in_agri_optical_worker", job_id=job_id)
            return None

        update_job_progress(
            session,
            job,
            "download_bands",
            {
                "scene": idx + 1,
                "total_scenes": total_scenes,
                "scene_id": scene["id"],
            },
        )
        t_scene = time.perf_counter()
        t0 = time.perf_counter()
        hrefs = dict(scene.get("band_hrefs") or {})
        scl_href = hrefs.pop("SCL", None)
        bands = read_bands_windowed_parallel(
            hrefs,
            bounds,
            target_shape,
            target_transform,
            scene_workers=scene_workers,
        )
        scl = None
        if scl_href:
            scl = read_band_windowed(
                scl_href,
                bounds,
                target_shape,
                target_transform,
                resampling=Resampling.nearest,
            )
        from app.core.decloud import decloud_enabled

        if decloud_enabled():
            from app.tasks.decloud_uncrtaints import cache_optical_s2_window

            scene_date = scene.get("date")
            date_str = (
                scene_date.isoformat()
                if hasattr(scene_date, "isoformat")
                else str(scene_date)[:10]
            )
            try:
                cache_optical_s2_window(
                    land_id=str(agri_meta["land_id"]),
                    date_str=date_str,
                    bands=bands,
                    band_hrefs=scene.get("band_hrefs"),
                    cloud_cover=scene.get("cloud_cover"),
                    stac_id=scene.get("id"),
                )
            except Exception as exc:
                # Decloud scratch cache is best-effort; lonlat OSS/MQ still publish.
                logger.warning(
                    "decloud_cache_window_failed",
                    land_id=str(agri_meta["land_id"]),
                    date=date_str,
                    scene_id=scene.get("id"),
                    error=str(exc),
                )
        download_ms = int((time.perf_counter() - t0) * 1000)
        complete_step(session, job, "download_bands")

        update_job_progress(session, job, "compute_indices")
        t0 = time.perf_counter()
        index_arrays: dict[str, np.ndarray] = {}
        for index_def in index_defs:
            needed = {b: bands[b] for b in index_def.bands if b in bands}
            arr = index_def.formula(needed)
            arr[~np.isfinite(arr)] = np.nan
            arr[~field_mask] = np.nan
            pix_key = INDEX_KEY_TO_PIXEL[index_def.key]
            index_arrays[pix_key] = arr
        compute_ms = int((time.perf_counter() - t0) * 1000)
        complete_step(session, job, "compute_indices")

        write_cog_ms = 0
        if write_cogs:
            update_job_progress(session, job, "write_cog")
            t0 = time.perf_counter()
            for index_def in index_defs:
                pix_key = INDEX_KEY_TO_PIXEL[index_def.key]
                write_cog(
                    index_arrays[pix_key],
                    target_transform,
                    "EPSG:4326",
                    org_id_str,
                    field_id_str,
                    scene["date"],
                    index_def.key,
                )
            write_cog_ms = int((time.perf_counter() - t0) * 1000)
            complete_step(session, job, "write_cog")

        t0 = time.perf_counter()
        parcel_from_scl = parcel_cloud_from_scl_window(scl, field_mask)
        parcel_source = PARCEL_CLOUD_SOURCE_SCL if parcel_from_scl is not None else None
        update_job_progress(session, job, "write_lonlat")
        result = emit_optical_lonlat(
            meta=agri_meta,
            geom4326=field_geom_geojson,
            field_id_str=field_id_str,
            scene=scene,
            index_arrays=index_arrays,
            transform=target_transform,
            parcel_cloud=parcel_from_scl,
            parcel_cloud_source=parcel_source,
            scl=scl,
            mq_task_id=mq_task_id,
        )
        write_lonlat_ms = int((time.perf_counter() - t0) * 1000)
        complete_step(
            session,
            job,
            "write_lonlat",
            {"pixels": (result or {}).get("pixels"), "date": str(scene["date"])},
        )
        logger.info(
            "scene_timing",
            sensor="S2",
            job_id=job_id,
            scene_id=scene.get("id"),
            date=str(scene.get("date")),
            download_ms=download_ms,
            compute_ms=compute_ms,
            write_cog_ms=write_cog_ms,
            write_lonlat_ms=write_lonlat_ms,
            total_ms=int((time.perf_counter() - t_scene) * 1000),
            bands=len(scene.get("band_hrefs") or {}),
            pixels=(result or {}).get("pixels"),
        )
        return result
    except Exception as e:
        logger.error(
            "agri_optical_scene_failed",
            scene_id=scene.get("id"),
            error=str(e),
        )
        try:
            session.rollback()
        except Exception:
            pass
        return None
    finally:
        session.close()


@celery_app.task(
    name="app.tasks.agri_lonlat.process_agri_optical_lonlat",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500,
)
def process_agri_optical_lonlat(self, job_id: str) -> dict:
    """Search S2, compute agri optical indices in memory, upsert lonlat_v1."""
    from app.core.agri_tags import is_agri_tagged
    from app.models.tables import Field, Job

    index_defs = agri_optical_index_defs()
    session = get_db_session()
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            logger.error("job_not_found", job_id=job_id, index="agri_optical")
            return {"job_id": job_id, "status": "error", "detail": "Job not found"}

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.progress_json = {"current_step": "scene_search", "steps": {}}
        session.commit()

        field = session.get(Field, job.field_id)
        if not field or field.geom is None:
            job.status = "failed"
            job.error = "Field not found or missing geom"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"job_id": job_id, "status": "failed"}

        if not is_agri_tagged(field.tags_json):
            job.status = "failed"
            job.error = "agri_optical requires an agri-tagged field"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"job_id": job_id, "status": "failed", "reason": "not_agri"}

        agri_meta = _load_agri_meta(session, field)
        field_geom = to_shape(field.geom)
        field_geom_geojson = mapping(field_geom)
        params = job.params_json or {}
        date_from = date.fromisoformat(params["date_from"])
        date_to = date.fromisoformat(params["date_to"])
        org_id_str = "default"
        field_id_str = str(job.field_id)
        write_cogs = write_index_cogs_enabled(is_agri=True)
        force = bool(params.get("force") or False)
        mq_task_id = (params.get("mq_task_id") or None)
        if mq_task_id is not None:
            mq_task_id = str(mq_task_id)

        update_job_progress(session, job, "scene_search")
        t_search = time.perf_counter()
        from app.core.decloud import decloud_enabled, decloud_stac_cloud_max_pct

        extra_cloud = decloud_stac_cloud_max_pct() if decloud_enabled() else None
        extra_assets: dict[str, tuple[str, ...]] = {"SCL": SCL_STAC_ASSETS}
        if decloud_enabled():
            from app.core.decloud import decloud_s2_extra_assets

            extra_assets.update(decloud_s2_extra_assets())
        # Agri needs the full Element84 series (not weekly lowest-cloud):
        # weekly dedupe dropped the high-cloud days users compare in STAC.
        scenes = search_scenes_for_defs(
            field_geom_geojson,
            date_from,
            date_to,
            index_defs,
            index_label="agri_optical",
            max_cloud_cover=extra_cloud,
            extra_assets=extra_assets,
            cloud_dedupe="none",
            max_items=2000,
        )
        # Out of growing season: skip STAC cloud >30% (no pull, no decloud).
        # In season: keep all cloudy scenes for UnCRtainTS.
        from app.core.decloud import (
            filter_scenes_outside_season_high_cloud,
            normalize_season_months,
        )

        crop_type = getattr(field, "crop_type", None)
        # User-selected rotation windows (from backfill) beat crop default.
        season_months = normalize_season_months(
            season_months=params.get("season_months"),
            growing_seasons=params.get("growing_seasons"),
            crop_type=crop_type,
        )
        scenes, skipped_offseason_cloudy = filter_scenes_outside_season_high_cloud(
            scenes,
            season_months=season_months,
        )
        if skipped_offseason_cloudy:
            logger.info(
                "agri_optical_skip_offseason_cloudy",
                job_id=job_id,
                skipped=skipped_offseason_cloudy,
                season_months=list(season_months),
                crop_type=crop_type,
            )
        skipped_existing = 0
        if not force:
            existing = existing_agri_scene_dates(session, agri_meta["land_id"], "S2")
            before = len(scenes)
            scenes = filter_scenes_skip_existing(
                scenes,
                existing,
                force=False,
                field_id=field_id_str,
                index="agri_optical",
            )
            skipped_existing = before - len(scenes)
            complete_step(
                session,
                job,
                "scene_search",
                {
                    "scene_count": before,
                    "scenes_after_dedup": len(scenes),
                    "skipped_existing": skipped_existing,
                },
            )
        else:
            complete_step(session, job, "scene_search", {"scene_count": len(scenes)})
        logger.info(
            "job_phase_timing",
            phase="scene_search",
            sensor="S2",
            job_id=job_id,
            elapsed_ms=int((time.perf_counter() - t_search) * 1000),
            scenes=len(scenes),
            skipped_existing=skipped_existing,
            date_from=str(date_from),
            date_to=str(date_to),
        )

        if not scenes:
            job.status = "completed"
            job.finished_at = datetime.now(timezone.utc)
            progress = job.progress_json or {}
            progress["current_step"] = "complete"
            progress["message"] = "No new cloud-free scenes (or none missing lonlat)."
            progress["scenes_upserted"] = 0
            progress["skipped_existing"] = skipped_existing
            job.progress_json = progress
            flag_modified(job, "progress_json")
            session.commit()
            return {
                "job_id": job_id,
                "status": "completed",
                "scenes": 0,
                "skipped_existing": skipped_existing,
            }

        target_transform, target_shape, field_mask, bounds = compute_target_grid(
            field_geom.bounds, field_geom
        )
        workers = min(scene_max_workers(), len(scenes))
        logger.info(
            "scene_parallel_start",
            job_id=job_id,
            index="agri_optical",
            scenes=len(scenes),
            workers=workers,
            band_gdal_cap=band_max_workers(),
        )
        update_job_progress(
            session,
            job,
            "process_scenes",
            {"total_scenes": len(scenes), "workers": workers},
        )

        upserted = 0
        raw_results: list[dict[str, Any]] = []
        t_process = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _process_one_optical_scene,
                    job_id=job_id,
                    scene=scene,
                    idx=idx,
                    total_scenes=len(scenes),
                    index_defs=index_defs,
                    target_transform=target_transform,
                    target_shape=target_shape,
                    field_mask=field_mask,
                    bounds=bounds,
                    org_id_str=org_id_str,
                    field_id_str=field_id_str,
                    agri_meta=agri_meta,
                    field_geom_geojson=field_geom_geojson,
                    write_cogs=write_cogs,
                    scene_workers=workers,
                    mq_task_id=mq_task_id,
                ): scene
                for idx, scene in enumerate(scenes)
            }
            for fut in as_completed(futures):
                scene = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    logger.error(
                        "agri_optical_scene_failed",
                        scene_id=scene.get("id"),
                        error=str(e),
                    )
                    continue
                if result is not None:
                    upserted += 1
                    raw_results.append(result)

        logger.info(
            "scene_parallel_done",
            job_id=job_id,
            index="agri_optical",
            layers_created=upserted,
            total_scenes=len(scenes),
            workers=workers,
            wall_ms=int((time.perf_counter() - t_process) * 1000),
        )

        session.expire(job)
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            return {"job_id": job_id, "status": "error", "detail": "Job not found"}

        complete_step(
            session,
            job,
            "process_scenes",
            {"scenes_upserted": upserted, "workers": workers},
        )

        decloud_schedule: dict[str, Any] | None = None
        if decloud_enabled() and raw_results:
            from app.tasks.decloud_uncrtaints import schedule_decloud_after_raw

            decloud_schedule = schedule_decloud_after_raw(
                field_id=field_id_str,
                land_id=str(agri_meta["land_id"]),
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                raw_results=raw_results,
                mq_task_id=mq_task_id,
                season_months=season_months,
                crop_type=crop_type,
            )

        job.status = "completed"
        job.finished_at = datetime.now(timezone.utc)
        progress = job.progress_json or {}
        progress["current_step"] = "complete"
        progress["scenes_upserted"] = upserted
        progress["total_scenes"] = len(scenes)
        progress["skipped_existing"] = skipped_existing
        progress["write_cogs"] = write_cogs
        if decloud_schedule:
            progress["decloud"] = {
                "mode": decloud_schedule.get("mode"),
                "batch": decloud_schedule.get("batch"),
                "per_scene": decloud_schedule.get("per_scene"),
                "hold_decloud_dates": decloud_schedule.get("hold_decloud_dates"),
            }
        job.progress_json = progress
        flag_modified(job, "progress_json")
        session.commit()

        logger.info(
            "agri_optical_job_completed",
            job_id=job_id,
            upserted=upserted,
            write_cogs=write_cogs,
        )
        return {
            "job_id": job_id,
            "status": "completed",
            "scenes_upserted": upserted,
            "skipped_existing": skipped_existing,
            "write_cogs": write_cogs,
        }
    except Exception as e:
        logger.error("agri_optical_job_failed", job_id=job_id, error=str(e))
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
