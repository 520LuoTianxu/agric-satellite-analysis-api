"""Sentinel-1 GRD → agri.parcel_scene_products lonlat_v1 (VV_db/VH_db).

Searches Element84 earth-search ``sentinel-1-grd`` (VV/VH COGs on the AWS
Open Data bucket), converts amplitude DN to approximate σ⁰ dB, samples the
field polygon, and upserts ``sensor='S1'`` lonlat_v1 rows.

Index ``vv.tif`` / ``vh.tif`` COGs are **not** uploaded for agri fields
unless ``WRITE_INDEX_COGS=1``. Classic (non-agri) fields still write COGs.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import rasterio
import structlog
from geoalchemy2.shape import to_shape
from pystac_client import Client as STACClient
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds, xy
from rasterio.warp import Resampling, reproject
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles
from shapely.geometry import mapping
from sqlalchemy.orm.attributes import flag_modified

from app.core.band_parallel import run_parallel_band_jobs, band_max_workers
from app.core.config import settings, scene_max_workers
from app.core.index_cogs import upload_scene_json_enabled, write_index_cogs_enabled
from app.core.storage import get_storage
from app.tasks.storage_tasks import upload_file_via_storage
from app.tasks.pipeline import (
    RETRY_DELAYS,
    collect_existing_scene_dates,
    complete_step,
    compute_zonal_stats,
    existing_agri_scene_dates,
    filter_scenes_skip_existing,
    get_db_session,
    update_job_progress,
)
from app.worker import celery_app

logger = structlog.get_logger()

STAC_API_URL = os.environ.get("STAC_API_URL", settings.stac_api_url)
STAC_S1_COLLECTION = "sentinel-1-grd"
# Nominal IW GRDH amplitude calibration scale so DN→dB lands near typical σ⁰.
# Full LUT calibration is not applied; values are approximate but flood-usable.
_S1_DN_CAL = 1000.0
_S1_EPS = 1e-10
# Thread-local GDAL/AWS options for public sentinel-s1-l1c. Do not mutate
# process-wide os.environ (that would serialize VV/VH and race OSS uploads).
_S1_PUBLIC_AWS_ENV = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "AWS_VIRTUAL_HOSTING": "TRUE",
    "AWS_HTTPS": "YES",
    "AWS_REGION": "eu-central-1",
    "AWS_DEFAULT_REGION": "eu-central-1",
    "AWS_S3_ENDPOINT": "s3.eu-central-1.amazonaws.com",
    "AWS_ACCESS_KEY_ID": "",
    "AWS_SECRET_ACCESS_KEY": "",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
}

UPSERT_S1_SQL = """
INSERT INTO agri.parcel_scene_products (
  land_id, tile_id, date, sensor, scene_id, land_name,
  cloud_cover, cloud_cover_over_30, parcel_cloud_cover_pct,
  json_oss_key, pixel_count, generated_at_shanghai,
  pixel_data_url,
  vv_avg, vv_min, vv_max,
  vh_avg, vh_min, vh_max,
  pixel_data
) VALUES (
  %(land_id)s, %(tile_id)s, %(date)s, 'S1', %(scene_id)s, %(land_name)s,
  NULL, NULL, NULL,
  %(json_oss_key)s, %(pixel_count)s, %(generated_at_shanghai)s,
  %(pixel_data_url)s,
  %(vv_avg)s, %(vv_min)s, %(vv_max)s,
  %(vh_avg)s, %(vh_min)s, %(vh_max)s,
  %(pixel_data)s::jsonb
)
ON CONFLICT (land_id, date, sensor, scene_id) DO UPDATE SET
  tile_id = EXCLUDED.tile_id,
  land_name = EXCLUDED.land_name,
  json_oss_key = COALESCE(EXCLUDED.json_oss_key, agri.parcel_scene_products.json_oss_key),
  pixel_count = EXCLUDED.pixel_count,
  generated_at_shanghai = EXCLUDED.generated_at_shanghai,
  pixel_data_url = EXCLUDED.pixel_data_url,
  vv_avg = EXCLUDED.vv_avg,
  vv_min = EXCLUDED.vv_min,
  vv_max = EXCLUDED.vv_max,
  vh_avg = EXCLUDED.vh_avg,
  vh_min = EXCLUDED.vh_min,
  vh_max = EXCLUDED.vh_max,
  pixel_data = EXCLUDED.pixel_data,
  ingested_at = now()
"""


def _round6(v: float) -> float:
    return float(round(float(v), 6))


def _dn_to_db(dn: np.ndarray) -> np.ndarray:
    """Convert GRD amplitude DN to approximate σ⁰ dB."""
    amp = dn.astype(np.float32)
    amp[amp <= 0] = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        power = (amp / _S1_DN_CAL) ** 2
        db = 10.0 * np.log10(power + _S1_EPS)
    db[~np.isfinite(db)] = np.nan
    return db


def search_s1_scenes(
    field_geom_geojson: dict, date_from: date, date_to: date
) -> list[dict]:
    """Search sentinel-1-grd; keep lowest-id scene per ISO week (IW DV preferred)."""
    t0 = time.perf_counter()
    catalog = STACClient.open(STAC_API_URL)
    search = catalog.search(
        collections=[STAC_S1_COLLECTION],
        intersects=field_geom_geojson,
        datetime=f"{date_from.isoformat()}/{date_to.isoformat()}",
        max_items=200,
    )
    items = list(search.items())
    logger.info(
        "s1_stac_search_results",
        count=len(items),
        date_from=str(date_from),
        date_to=str(date_to),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
    )
    if not items:
        return []

    weekly: dict[str, Any] = {}
    for item in items:
        vv = item.assets.get("vv")
        vh = item.assets.get("vh")
        if not vv or not vh:
            continue
        item_date = item.datetime.date() if item.datetime else date_from
        week_key = item_date.isocalendar()[:2]
        week_str = f"{week_key[0]}-W{week_key[1]:02d}"
        # Prefer dual-pol IW GRDH (DV) when multiple per week
        score = 0
        props = item.properties or {}
        if "IW" in (item.id or ""):
            score += 2
        if props.get("sar:instrument_mode") == "IW":
            score += 2
        polarizations = props.get("sar:polarizations") or []
        if "VV" in polarizations and "VH" in polarizations:
            score += 3
        entry = weekly.get(week_str)
        if entry is None or score > entry["score"]:
            weekly[week_str] = {
                "item": item,
                "date": item_date,
                "score": score,
                "vv_href": vv.href,
                "vh_href": vh.href,
            }

    scenes = []
    for week_str in sorted(weekly.keys()):
        e = weekly[week_str]
        scenes.append(
            {
                "id": e["item"].id,
                "date": e["date"],
                "vv_href": e["vv_href"],
                "vh_href": e["vh_href"],
            }
        )
    return scenes


def _read_band_windowed_db(
    href: str, bounds: tuple, target_shape: tuple, target_transform
) -> np.ndarray:
    """Read GRD COG window, reproject to field grid, convert DN→dB.

    Sentinel-1 GRD COGs are often CRS-less with GCPs; WarpedVRT → EPSG:4326.
    """
    from rasterio.vrt import WarpedVRT

    s3_path = href
    if href.startswith("s3://"):
        s3_path = href.replace("s3://", "/vsis3/", 1)
    dst = np.zeros(target_shape, dtype=np.float32)
    with rasterio.Env(**_S1_PUBLIC_AWS_ENV):
        with rasterio.open(s3_path) as src:
            # Always warp via VRT so GCP-only products work
            with WarpedVRT(
                src, crs="EPSG:4326", resampling=Resampling.bilinear
            ) as vrt:
                # WarpedVRT forbids boundless reads — clip window to VRT extent
                window = rasterio.windows.from_bounds(
                    *bounds, transform=vrt.transform
                ).intersection(rasterio.windows.Window(0, 0, vrt.width, vrt.height))
                if window.width <= 0 or window.height <= 0:
                    return dst  # all-nan after dn_to_db of zeros→nan path
                window = window.round_offsets().round_lengths()
                data = vrt.read(1, window=window, boundless=False)
                src_transform = rasterio.windows.transform(window, vrt.transform)
                reproject(
                    source=data.astype(np.float32),
                    destination=dst,
                    src_transform=src_transform,
                    src_crs="EPSG:4326",
                    dst_transform=target_transform,
                    dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                )
    return _dn_to_db(dst)


def _write_index_cog(
    data: np.ndarray, transform, org_id: str, field_id: str, scene_date: date, stem: str
) -> str:
    """Write float32 COG to active storage; return storage URI."""
    object_key = f"cogs/{org_id}/{field_id}/{scene_date.isoformat()}/{stem}.tif"
    src_fd, tmp_src = tempfile.mkstemp(suffix="_src.tif")
    dst_fd, tmp_dst = tempfile.mkstemp(suffix="_cog.tif")
    os.close(src_fd)
    os.close(dst_fd)
    try:
        profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "width": data.shape[1],
            "height": data.shape[0],
            "count": 1,
            "crs": "EPSG:4326",
            "transform": transform,
            "nodata": np.nan,
        }
        with rasterio.open(tmp_src, "w", **profile) as dst:
            dst.write(data.astype(np.float32), 1)
        output_profile = cog_profiles.get("deflate")
        cog_translate(tmp_src, tmp_dst, output_profile, overview_level=2, quiet=True)
        result = upload_file_via_storage(object_key, tmp_dst, content_type="image/tiff")
        return result["uri"]
    finally:
        for p in (tmp_src, tmp_dst):
            try:
                os.unlink(p)
            except OSError:
                pass


def _sample_s1_lonlat(
    geom4326: dict, vv: np.ndarray, vh: np.ndarray, transform
) -> list[dict[str, Any]]:
    h, w = vv.shape
    inside = ~geometry_mask(
        [geom4326],
        out_shape=(h, w),
        transform=transform,
        all_touched=False,
        invert=False,
    )
    finite = np.isfinite(vv) & inside
    rows, cols = np.where(finite)
    if rows.size == 0:
        rows, cols = np.where(np.isfinite(vv))
    if rows.size == 0:
        return []

    xs, ys = xy(transform, rows, cols, offset="center")
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)

    pixels: list[dict[str, Any]] = []
    for i in range(rows.size):
        r, c = int(rows[i]), int(cols[i])
        v = vv[r, c]
        if not np.isfinite(v):
            continue
        pix: dict[str, Any] = {
            "lon": _round6(xs[i]),
            "lat": _round6(ys[i]),
            "VV_db": _round6(v),
        }
        hval = vh[r, c]
        if np.isfinite(hval):
            pix["VH_db"] = _round6(hval)
        pixels.append(pix)
    return pixels


def _resolve_agri_meta(session, field) -> dict[str, Any] | None:
    from sqlalchemy import text

    from app.core.agri_tags import parse_agri_land_id

    land_id = parse_agri_land_id(field.tags_json)
    if not land_id:
        return None
    row = (
        session.execute(
            text(
                "SELECT land_id, tile_id, land_name FROM agri.land_parcels WHERE land_id = :lid"
            ),
            {"lid": str(land_id)},
        )
        .mappings()
        .first()
    )
    if not row:
        return None
    return {
        "land_id": row["land_id"],
        "tile_id": row["tile_id"],
        "land_name": row["land_name"] or field.name,
    }


def _upsert_agri_s1(
    session,
    meta: dict,
    scene_date: date,
    scene_id: str,
    field_id: str,
    pixels: list,
    vv_stats: dict,
    vh_stats: dict,
    mq_task_id: str | None = None,
) -> str | None:
    """Upload S1 lonlat JSON to OSS and publish one result MQ (no local PG upsert).

    Returns public JSON URL when upload+publish succeed.
    """
    date_str = scene_date.isoformat()
    pixel_data = {"format": "lonlat_v1", "pixels": pixels}
    json_oss_key = None
    json_url = None
    json_upload_ms = 0
    from openfarm_common.mq_results import (
        scene_json_oss_key,
        upload_scene_product_json,
    )

    t_json = time.perf_counter()
    json_oss_key = scene_json_oss_key(meta["land_id"], date_str, "S1")
    product = {
        "land_id": meta["land_id"],
        "tile_id": meta["tile_id"],
        "date": date_str,
        "sensor": "S1",
        "scene_id": scene_id,
        "land_name": meta["land_name"],
        "cloud_cover": None,
        "cloud_cover_over_30": None,
        "parcel_cloud_cover_pct": None,
        "pixel_count": len(pixels),
        "generated_at_shanghai": datetime.now(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d %H:%M:%S%z"),
        "pixel_data_url": f"stac-s1://field/{field_id}/{date_str}",
        "json_oss_key": json_oss_key,
        "vv_avg": vv_stats.get("mean"),
        "vv_min": vv_stats.get("min"),
        "vv_max": vv_stats.get("max"),
        "vh_avg": vh_stats.get("mean"),
        "vh_min": vh_stats.get("min"),
        "vh_max": vh_stats.get("max"),
        "pixel_data": pixel_data,
    }
    json_oss_key, json_url = upload_scene_product_json(
        land_id=meta["land_id"], date_str=date_str, sensor="S1", product=product
    )
    json_upload_ms = int((time.perf_counter() - t_json) * 1000)
    product["json_url"] = json_url

    if not json_url or not json_oss_key:
        raise RuntimeError("S1 agri path requires OSS scene JSON upload before MQ publish")

    from openfarm_common.mq_results import publish_task_result

    label = f"{date_str}_S1"
    parent = (mq_task_id or "").strip() or None
    result_task_id = (
        f"{parent}:{label}" if parent else f"agri-scene:{meta['land_id']}:{label}"
    )
    t_mq = time.perf_counter()
    publish_task_result(
        task_id=result_task_id,
        status="success",
        land_id=str(meta["land_id"]),
        field_id=str(field_id) if field_id else None,
        oss_urls={label: json_url},
        collect_parcel_urls=False,
        upload_summary_if_empty=False,
        extras={
            "kind": "parcel_scene_product",
            "sensor": "S1",
            "date": date_str,
            "scene_id": scene_id,
            "parent_mq_task_id": parent,
            "json_oss_key": json_oss_key,
        },
    )
    mq_publish_ms = int((time.perf_counter() - t_mq) * 1000)
    logger.info(
        "lonlat_write_timing",
        land_id=meta.get("land_id"),
        date=date_str,
        sensor="S1",
        json_upload_ms=json_upload_ms,
        db_upsert_ms=0,
        mq_publish_ms=mq_publish_ms,
        uploaded_json=True,
        path="oss_mq",
    )
    logger.info(
        "lonlat_oss_mq_published",
        land_id=meta.get("land_id"),
        date=date_str,
        sensor="S1",
        json_url=json_url,
        result_task_id=result_task_id,
    )
    return json_url


def _process_one_s1_scene(
    *,
    job_id: str,
    scene: dict,
    idx: int,
    total_scenes: int,
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    field_mask: np.ndarray,
    org_id_str: str,
    field_id_str: str,
    field_id,
    date_from: date,
    date_to: date,
    agri_meta: dict | None,
    field_geom_geojson: dict,
    scene_workers: int = 1,
    mq_task_id: str | None = None,
) -> bool:
    """Download S1 bands, optionally write COGs, upsert agri lonlat. Own DB session."""
    from app.models.tables import Job, RasterLayer
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    session = get_db_session()
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if job is None:
            logger.error("job_not_found_in_s1_scene_worker", job_id=job_id)
            return False

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
        pol = run_parallel_band_jobs(
            {"vv": scene["vv_href"], "vh": scene["vh_href"]},
            lambda _key, href: _read_band_windowed_db(
                href, bounds, target_shape, target_transform
            ),
            scene_workers=scene_workers,
        )
        vv = pol["vv"]
        vh = pol["vh"]
        vv[~field_mask] = np.nan
        vh[~field_mask] = np.nan
        download_ms = int((time.perf_counter() - t0) * 1000)
        complete_step(session, job, "download_bands")

        is_agri = agri_meta is not None
        write_cogs = write_index_cogs_enabled(is_agri=is_agri)
        t0 = time.perf_counter()
        vv_stats = compute_zonal_stats(vv)
        vh_stats = compute_zonal_stats(vh)
        stats_ms = int((time.perf_counter() - t0) * 1000)

        write_cog_ms = 0
        if write_cogs:
            update_job_progress(session, job, "write_cog")
            t0 = time.perf_counter()
            vv_uri = _write_index_cog(
                vv, target_transform, org_id_str, field_id_str, scene["date"], "vv"
            )
            vh_uri = _write_index_cog(
                vh, target_transform, org_id_str, field_id_str, scene["date"], "vh"
            )
            write_cog_ms = int((time.perf_counter() - t0) * 1000)
            complete_step(session, job, "write_cog")
            logger.info(
                "cog_uploaded",
                object_key=f"cogs/{org_id_str}/{field_id_str}/{scene['date'].isoformat()}/vv.tif",
                index="s1",
            )

            for label, uri, stats in (
                ("VV", vv_uri, vv_stats),
                ("VH", vh_uri, vh_stats),
            ):
                layer_values = dict(
                    field_id=field_id,
                    layer_type=label,
                    satellite="S1",
                    date=scene["date"],
                    cog_uri=uri,
                    min=stats.get("min"),
                    max=stats.get("max"),
                    params_json={
                        "date_from": str(date_from),
                        "date_to": str(date_to),
                        "source": STAC_S1_COLLECTION,
                    },
                    provenance_json={
                        "scene_id": scene["id"],
                        "processed_at": datetime.now(timezone.utc).isoformat(),
                        "pipeline_version": "s1-1.0.0",
                    },
                )
                stmt = (
                    pg_insert(RasterLayer)
                    .values(**layer_values)
                    .on_conflict_do_update(
                        constraint="uq_raster_field_date_type",
                        set_={
                            "cog_uri": uri,
                            "min": stats.get("min"),
                            "max": stats.get("max"),
                            "params_json": layer_values["params_json"],
                            "provenance_json": layer_values["provenance_json"],
                            "satellite": "S1",
                        },
                    )
                )
                session.execute(stmt)
            session.commit()
        else:
            logger.info(
                "cog_upload_skipped",
                object_key=f"cogs/{org_id_str}/{field_id_str}/{scene['date'].isoformat()}/vv.tif",
                index="s1",
                is_agri=is_agri,
            )

        write_lonlat_ms = 0
        pixels_n = 0
        if agri_meta is not None:
            t0 = time.perf_counter()
            pixels = _sample_s1_lonlat(field_geom_geojson, vv, vh, target_transform)
            if pixels:
                pixels_n = len(pixels)
                json_url = _upsert_agri_s1(
                    session,
                    agri_meta,
                    scene["date"],
                    f"{scene['id']}_stac",
                    field_id_str,
                    pixels,
                    vv_stats,
                    vh_stats,
                    mq_task_id=mq_task_id,
                )
                logger.info(
                    "lonlat_upserted",
                    land_id=agri_meta.get("land_id"),
                    date=str(scene["date"]),
                    sensor="S1",
                    pixels=len(pixels),
                    json_url=json_url,
                )
            write_lonlat_ms = int((time.perf_counter() - t0) * 1000)
        logger.info(
            "scene_timing",
            sensor="S1",
            job_id=job_id,
            scene_id=scene.get("id"),
            date=str(scene.get("date")),
            download_ms=download_ms,
            stats_ms=stats_ms,
            write_cog_ms=write_cog_ms,
            write_lonlat_ms=write_lonlat_ms,
            total_ms=int((time.perf_counter() - t_scene) * 1000),
            pixels=pixels_n,
        )
        return True
    except Exception as e:
        logger.error("s1_scene_failed", scene_id=scene.get("id"), error=str(e))
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def _process_s1_scenes_parallel(
    *,
    job_id: str,
    scenes: list[dict],
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    field_mask: np.ndarray,
    org_id_str: str,
    field_id_str: str,
    field_id,
    date_from: date,
    date_to: date,
    agri_meta: dict | None,
    field_geom_geojson: dict,
    mq_task_id: str | None = None,
) -> int:
    """Process S1 scenes concurrently. Returns the processed count."""
    total = len(scenes)
    if total == 0:
        return 0
    workers = min(scene_max_workers(), total)
    logger.info(
        "scene_parallel_start",
        job_id=job_id,
        index="s1",
        scenes=total,
        workers=workers,
        band_gdal_cap=band_max_workers(),
    )

    processed = 0
    t_process = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _process_one_s1_scene,
                job_id=job_id,
                scene=scene,
                idx=idx,
                total_scenes=total,
                bounds=bounds,
                target_shape=target_shape,
                target_transform=target_transform,
                field_mask=field_mask,
                org_id_str=org_id_str,
                field_id_str=field_id_str,
                field_id=field_id,
                date_from=date_from,
                date_to=date_to,
                agri_meta=agri_meta,
                field_geom_geojson=field_geom_geojson,
                scene_workers=workers,
                mq_task_id=mq_task_id,
            ): scene
            for idx, scene in enumerate(scenes)
        }
        for fut in as_completed(futures):
            scene = futures[fut]
            try:
                ok = fut.result()
            except Exception as e:
                logger.error("s1_scene_failed", scene_id=scene.get("id"), error=str(e))
                continue
            if ok:
                processed += 1

    logger.info(
        "scene_parallel_done",
        job_id=job_id,
        index="s1",
        layers_created=processed,
        total_scenes=total,
        workers=workers,
    )
    return processed


@celery_app.task(
    name="app.tasks.sentinel1.process_s1_backfill",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500,
)
def process_s1_backfill(self, job_id: str) -> dict:
    """Celery entry: search S1 GRD, upsert agri lonlat_v1 (COGs only if enabled)."""
    from app.models.tables import Job, Field

    session = get_db_session()
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
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

        field_geom = to_shape(field.geom)
        field_geom_geojson = mapping(field_geom)
        params = job.params_json or {}
        mq_task_id = params.get("mq_task_id")
        if mq_task_id is not None:
            mq_task_id = str(mq_task_id)
        date_from = date.fromisoformat(params["date_from"])
        date_to = date.fromisoformat(params["date_to"])
        org_id_str = "default"  # STORAGE_TENANT; auth/orgs removed
        field_id_str = str(job.field_id)

        update_job_progress(session, job, "scene_search")
        t_search = time.perf_counter()
        scenes = search_s1_scenes(field_geom_geojson, date_from, date_to)
        force = bool(params.get("force") or False)
        skipped_existing = 0
        agri_meta = _resolve_agri_meta(session, field)
        if not force:
            if agri_meta is not None:
                existing = existing_agri_scene_dates(
                    session, agri_meta["land_id"], "S1"
                )
            else:
                existing = collect_existing_scene_dates(
                    session, field, layer_type="VV", satellite="S1", agri_sensor="S1"
                )
            before = len(scenes)
            scenes = filter_scenes_skip_existing(
                scenes, existing, force=False, field_id=field_id_str, index="s1"
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
            sensor="S1",
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
            session.commit()
            return {
                "job_id": job_id,
                "status": "completed",
                "scenes": 0,
                "skipped_existing": skipped_existing,
            }

        minx, miny, maxx, maxy = field_geom.bounds
        buf = 0.001
        bounds = (minx - buf, miny - buf, maxx + buf, maxy + buf)
        pixel_size = 0.0001  # ~10 m
        width = max(int((bounds[2] - bounds[0]) / pixel_size), 1)
        height = max(int((bounds[3] - bounds[1]) / pixel_size), 1)
        max_dim = 2000
        if width > max_dim or height > max_dim:
            scale = max_dim / max(width, height)
            width = max(int(width * scale), 1)
            height = max(int(height * scale), 1)
        target_transform = from_bounds(*bounds, width, height)
        target_shape = (height, width)
        field_mask = geometry_mask(
            [mapping(field_geom)],
            out_shape=target_shape,
            transform=target_transform,
            invert=True,
        )

        workers = min(scene_max_workers(), len(scenes))
        update_job_progress(
            session,
            job,
            "process_scenes",
            {"total_scenes": len(scenes), "workers": workers},
        )
        field_id = job.field_id
        processed = _process_s1_scenes_parallel(
            job_id=job_id,
            scenes=scenes,
            bounds=bounds,
            target_shape=target_shape,
            target_transform=target_transform,
            field_mask=field_mask,
            org_id_str=org_id_str,
            field_id_str=field_id_str,
            field_id=field_id,
            date_from=date_from,
            date_to=date_to,
            agri_meta=agri_meta,
            field_geom_geojson=field_geom_geojson,
            mq_task_id=mq_task_id,
        )

        session.expire(job)
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            return {
                "job_id": job_id,
                "status": "error",
                "detail": "Job not found",
            }
        complete_step(
            session,
            job,
            "process_scenes",
            {"layers_created": processed, "workers": workers},
        )
        job.status = "completed"
        job.finished_at = datetime.now(timezone.utc)
        progress = job.progress_json or {}
        progress["current_step"] = "complete"
        progress["layers_created"] = processed
        progress["total_scenes"] = len(scenes)
        progress["scene_workers"] = workers
        job.progress_json = progress
        flag_modified(job, "progress_json")
        session.commit()
        return {
            "job_id": job_id,
            "status": "completed",
            "scenes": len(scenes),
            "processed": processed,
            "skipped_existing": skipped_existing,
            "backend": get_storage().backend,
        }
    except Exception as e:
        logger.error("s1_backfill_failed", job_id=job_id, error=str(e))
        try:
            job = session.get(Job, uuid.UUID(job_id))
            if job:
                retries = self.request.retries
                if retries < self.max_retries:
                    raise self.retry(
                        exc=e,
                        countdown=RETRY_DELAYS[min(retries, len(RETRY_DELAYS) - 1)],
                    )
                job.status = "failed"
                job.error = str(e)[:500]
                job.finished_at = datetime.now(timezone.utc)
                session.commit()
        except Exception:
            session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(
    name="app.tasks.sentinel1.backfill_s1_for_field",
    bind=True,
    max_retries=1,
    time_limit=120,
    soft_time_limit=90,
)
def backfill_s1_for_field(
    self,
    field_id: str,
    months: int | None = None,
    force: bool = False,
    mq_task_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Orchestrate chunked S1 jobs for a field (same months as index backfill)."""
    from app.models.tables import Field, Job

    months = months or settings.index_backfill_months
    chunk_days = settings.index_backfill_chunk_days
    session = get_db_session()
    try:
        field = session.get(Field, uuid.UUID(field_id))
        if not field:
            return {
                "field_id": field_id,
                "status": "error",
                "detail": "Field not found",
            }

        end_date = date.fromisoformat(date_to) if date_to else date.today()
        if date_from:
            start_date = date.fromisoformat(date_from)
        else:
            start_date = end_date - timedelta(days=months * 30)
        if start_date > end_date:
            start_date, end_date = end_date, start_date

        # Always dispatch chunks; process_s1_backfill skips dates already present
        # unless force=True (coarse chunk skip left gaps unfilled).
        chunks: list[tuple[date, date]] = []
        cursor = start_date
        while cursor < end_date:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end_date)
            chunks.append((cursor, chunk_end))
            cursor = chunk_end + timedelta(days=1)

        pending_sends: list[tuple[str, int]] = []
        dispatched = 0
        for chunk_idx, (chunk_start, chunk_end) in enumerate(chunks):
            job = Job(
                field_id=field.id,
                type="s1",
                status="pending",
                params_json={
                    "date_from": chunk_start.isoformat(),
                    "date_to": chunk_end.isoformat(),
                    "is_backfill": True,
                    "sensor": "S1",
                    "force": bool(force),
                    **({"mq_task_id": mq_task_id} if mq_task_id else {}),
                },
            )
            session.add(job)
            session.flush()
            pending_sends.append((str(job.id), chunk_idx * 30))
            dispatched += 1

        session.commit()
        for job_id, countdown in pending_sends:
            celery_app.send_task(
                "app.tasks.sentinel1.process_s1_backfill",
                args=[job_id],
                countdown=countdown,
            )
        return {
            "field_id": field_id,
            "status": "dispatched",
            "jobs": dispatched,
            "months": months,
            "force": force,
            "date_from": start_date.isoformat(),
            "date_to": end_date.isoformat(),
        }
    except Exception as e:
        session.rollback()
        logger.error("s1_orchestration_failed", field_id=field_id, error=str(e))
        raise
    finally:
        session.close()
