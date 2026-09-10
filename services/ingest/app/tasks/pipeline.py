"""Shared vegetation-index pipeline helpers.

Extracted from the original ``ndvi.py`` so that every index task
(NDVI, EVI, SAVI, NDWI, NDMI, NDRE, CIRE, MNDWI) reuses the same STAC search, band download,
COG write, zonal-stats, and alert-evaluation logic.
"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles
from pystac_client import Client as STACClient
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm.attributes import flag_modified

import structlog

from app.core.config import settings, scene_max_workers
from app.tasks.indices import IndexDef

logger = structlog.get_logger()

# ── Configuration (same as ndvi.py) ──────────────────────────────────

STAC_API_URL = os.environ.get(
    "STAC_API_URL", "https://earth-search.aws.element84.com/v1"
)
STAC_COLLECTION = "sentinel-2-l2a"
MAX_CLOUD_COVER = 20
# GDAL environment for reading remote COGs
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
os.environ.setdefault("VSI_CACHE", "TRUE")
os.environ.setdefault("VSI_CACHE_SIZE", "5000000")
# Scene threads each open their own datasets; keep GDAL's internal pool at 1
# so 16-way scene parallelism does not spawn nested thread storms.
os.environ.setdefault("GDAL_NUM_THREADS", "1")

# Serialize job.progress_json updates. SQLAlchemy Session and the Job row
# are not thread-safe; each scene worker uses its own session and this lock.
_job_progress_lock = threading.Lock()

RETRY_DELAYS = [60, 300, 900]  # Per PRD Section 7.4


# ── Storage / DB helpers ─────────────────────────────────────────────


def get_minio_client():
    """Deprecated: prefer ``get_storage()``. Thin wrapper for MinIO only."""
    import warnings
    from minio import Minio

    warnings.warn(
        "get_minio_client is deprecated; use app.core.storage.get_storage()",
        DeprecationWarning,
        stacklevel=2,
    )
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


# Deprecated alias kept for callers that still import MINIO_BUCKET.
MINIO_BUCKET = settings.minio_bucket


def get_db_session():
    from app.core.database_sync import SyncSession

    return SyncSession()


# ── Job progress helpers ─────────────────────────────────────────────


def update_job_progress(session, job, step: str, details: dict | None = None):
    """Update job.progress_json. Safe to call from scene worker threads.

    Reloads the row under a process-local lock so concurrent scene workers
    do not clobber each other's step maps.
    """
    with _job_progress_lock:
        session.refresh(job)
        progress = dict(job.progress_json or {})
        steps = dict(progress.get("steps") or {})
        entry = dict(steps.get(step) or {})
        entry["status"] = "running"
        entry["started_at"] = datetime.now(timezone.utc).isoformat()
        if details:
            entry.update(details)
        steps[step] = entry
        progress["current_step"] = step
        progress["steps"] = steps
        job.progress_json = progress
        flag_modified(job, "progress_json")
        session.commit()


def complete_step(session, job, step: str, details: dict | None = None):
    with _job_progress_lock:
        session.refresh(job)
        progress = dict(job.progress_json or {})
        steps = dict(progress.get("steps") or {})
        if step in steps:
            entry = dict(steps[step])
            entry["status"] = "completed"
            entry["finished_at"] = datetime.now(timezone.utc).isoformat()
            if details:
                entry.update(details)
            steps[step] = entry
            progress["steps"] = steps
            job.progress_json = progress
            flag_modified(job, "progress_json")
            session.commit()


# ── Existing-scene dedup (pre-COG) ────────────────────────────────────


def existing_layer_dates(
    session, field_id, layer_type: str, satellite: str | None = None
) -> set[date]:
    """Dates already present in raster_layers for this field + layer_type."""
    from app.models.tables import RasterLayer

    q = select(RasterLayer.date).where(
        RasterLayer.field_id == field_id, RasterLayer.layer_type == layer_type
    )
    if satellite is not None:
        q = q.where(RasterLayer.satellite == satellite)
    rows = session.execute(q).scalars().all()
    return {d for d in rows if d is not None}


def existing_agri_scene_dates(session, land_id: str, sensor: str) -> set[date]:
    """Dates already present in agri.parcel_scene_products for land_id + sensor."""
    from sqlalchemy import text as sa_text

    rows = session.execute(
        sa_text(
            """
            SELECT DISTINCT date
            FROM agri.parcel_scene_products
            WHERE land_id = :land_id
              AND sensor = :sensor
            """
        ),
        {"land_id": str(land_id), "sensor": sensor},
    ).fetchall()
    out: set[date] = set()
    for d in rows:
        if d is None:
            continue
        if isinstance(d, date):
            out.add(d)
        elif hasattr(d, "date"):
            out.add(d.date())
        else:
            out.add(date.fromisoformat(str(d)[:10]))
    return out


def collect_existing_scene_dates(
    session, field, *, layer_type: str, satellite: str, agri_sensor: str | None = None
) -> set[date]:
    """Union of raster_layers dates and (for agri fields) parcel_scene_products dates."""
    existing = existing_layer_dates(session, field.id, layer_type, satellite=satellite)
    sensor = agri_sensor or satellite
    try:
        from app.core.agri_tags import parse_agri_land_id

        land_id = parse_agri_land_id(getattr(field, "tags_json", None))
    except Exception:
        land_id = None
    if land_id:
        try:
            existing |= existing_agri_scene_dates(session, land_id, sensor)
        except Exception as e:
            logger.warning(
                "agri_existing_dates_failed",
                land_id=land_id,
                sensor=sensor,
                error=str(e),
            )
    return existing


def filter_scenes_skip_existing(
    scenes: list[dict],
    existing: set[date],
    *,
    force: bool,
    field_id: str | None = None,
    index: str | None = None,
) -> list[dict]:
    """Drop scenes whose date is already present unless force=True."""
    if force or not existing or not scenes:
        return scenes
    kept: list[dict] = []
    skipped = 0
    for scene in scenes:
        d = scene.get("date")
        if isinstance(d, date) and d in existing:
            skipped += 1
            continue
        kept.append(scene)
    if skipped:
        logger.info(
            "scene_skipped_existing",
            field_id=field_id,
            index=index,
            skipped=skipped,
            remaining=len(kept),
            existing_count=len(existing),
        )
    return kept


# ── STAC scene search ────────────────────────────────────────────────


def _resolve_band_hrefs(item, index_defs: list[IndexDef]) -> dict[str, str] | None:
    """Resolve unique band HREFs for one STAC item across index defs.

    Returns None when any required band is missing.
    """
    band_hrefs: dict[str, str | None] = {}
    for index_def in index_defs:
        for band_key in index_def.bands:
            if band_key in band_hrefs:
                continue
            href = None
            for asset_name in index_def.stac_asset_map.get(band_key, (band_key,)):
                asset = item.assets.get(asset_name)
                if asset:
                    href = asset.href
                    break
            band_hrefs[band_key] = href
    if not all(band_hrefs.values()):
        return None
    return {k: v for k, v in band_hrefs.items() if v is not None}


def search_scenes_for_defs(
    field_geom_geojson: dict,
    date_from: date,
    date_to: date,
    index_defs: list[IndexDef],
    *,
    index_label: str | None = None,
) -> list[dict]:
    """Search Element84 STAC and resolve HREFs for the union of index bands."""
    if not index_defs:
        return []
    label = index_label or ",".join(d.key for d in index_defs)
    catalog = STACClient.open(STAC_API_URL)
    search = catalog.search(
        collections=[STAC_COLLECTION],
        intersects=field_geom_geojson,
        datetime=f"{date_from.isoformat()}/{date_to.isoformat()}",
        query={"eo:cloud_cover": {"lt": MAX_CLOUD_COVER}},
        max_items=100,
    )
    items = list(search.items())
    logger.info(
        "stac_search_results",
        count=len(items),
        index=label,
        date_from=str(date_from),
        date_to=str(date_to),
    )
    if not items:
        return []

    # Group by week, pick lowest cloud cover per week
    weekly: dict[str, Any] = {}
    for item in items:
        item_date = item.datetime.date() if item.datetime else date_from
        week_key = item_date.isocalendar()[:2]
        week_str = f"{week_key[0]}-W{week_key[1]:02d}"
        cloud = item.properties.get("eo:cloud_cover", 100)
        if week_str not in weekly or cloud < weekly[week_str]["cloud"]:
            weekly[week_str] = {"item": item, "cloud": cloud, "date": item_date}

    scenes = []
    for week_str in sorted(weekly.keys()):
        entry = weekly[week_str]
        item = entry["item"]
        band_hrefs = _resolve_band_hrefs(item, index_defs)
        if band_hrefs:
            scenes.append(
                {
                    "id": item.id,
                    "date": entry["date"],
                    "cloud_cover": entry["cloud"],
                    "band_hrefs": band_hrefs,
                }
            )
        else:
            logger.warning("missing_bands", scene_id=item.id, index=label)

    return scenes


def search_scenes(
    field_geom_geojson: dict, date_from: date, date_to: date, index_def: IndexDef
) -> list[dict]:
    """Search Element84 STAC and resolve per-band HREFs for the given index."""
    return search_scenes_for_defs(
        field_geom_geojson, date_from, date_to, [index_def], index_label=index_def.key
    )


# ── Band reading ─────────────────────────────────────────────────────


def read_band_windowed(
    href: str, bounds: tuple, target_shape: tuple, target_transform
) -> np.ndarray:
    """Read a band from a remote COG, windowed to field extent."""
    with rasterio.Env():
        with rasterio.open(href) as src:
            src_bounds = transform_bounds("EPSG:4326", src.crs, *bounds)
            window = rasterio.windows.from_bounds(*src_bounds, transform=src.transform)
            data = src.read(1, window=window, boundless=True, fill_value=0)

            dst = np.zeros(target_shape, dtype=np.float32)
            reproject(
                source=data.astype(np.float32),
                destination=dst,
                src_transform=rasterio.windows.transform(window, src.transform),
                src_crs=src.crs,
                dst_transform=target_transform,
                dst_crs="EPSG:4326",
                resampling=Resampling.bilinear,
            )
            return dst


# ── COG writing ──────────────────────────────────────────────────────


def write_cog(
    data: np.ndarray,
    transform,
    crs: str,
    org_id: str,
    field_id: str,
    scene_date: date,
    index_key: str,
) -> str:
    """Write an index array as COG to object storage. Returns the ``cog_uri``."""
    object_key = f"cogs/{org_id}/{field_id}/{scene_date.isoformat()}/{index_key}.tif"
    src_fd, tmp_src_path = tempfile.mkstemp(suffix="_src.tif")
    dst_fd, tmp_dst_path = tempfile.mkstemp(suffix="_cog.tif")
    os.close(src_fd)
    os.close(dst_fd)

    try:
        profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "width": data.shape[1],
            "height": data.shape[0],
            "count": 1,
            "crs": crs,
            "transform": transform,
            "nodata": np.nan,
        }
        with rasterio.Env():
            with rasterio.open(tmp_src_path, "w", **profile) as dst:
                dst.write(data, 1)

        output_profile = cog_profiles.get("deflate")
        cog_translate(
            tmp_src_path, tmp_dst_path, output_profile, overview_level=2, quiet=True
        )

        from app.tasks.storage_tasks import upload_file_via_storage

        result = upload_file_via_storage(
            object_key, tmp_dst_path, content_type="image/tiff"
        )
        logger.info("cog_uploaded", object_key=object_key, index=index_key)
        return result["uri"]
    finally:
        for p in [tmp_src_path, tmp_dst_path]:
            try:
                os.unlink(p)
            except OSError:
                pass


# ── Zonal statistics ─────────────────────────────────────────────────


def compute_zonal_stats(data: np.ndarray) -> dict:
    """Compute zonal statistics over the valid (finite) pixels."""
    valid = data[np.isfinite(data)]
    if len(valid) == 0:
        return {
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
            "stddev": None,
            "p10": None,
            "p90": None,
            "quality_score": 0.0,
        }
    total_pixels = data.size
    return {
        "mean": float(np.nanmean(valid)),
        "median": float(np.nanmedian(valid)),
        "min": float(np.nanmin(valid)),
        "max": float(np.nanmax(valid)),
        "stddev": float(np.nanstd(valid)),
        "p10": float(np.nanpercentile(valid, 10)),
        "p90": float(np.nanpercentile(valid, 90)),
        "quality_score": round(len(valid) / total_pixels, 4) if total_pixels else 0.0,
    }


# ── Alert evaluation ────────────────────────────────────────────────


def _get_weather_context(session, field_id, alert_date: date) -> dict | None:
    """Query recent weather data to build context JSONB for alert enrichment."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.models.tables import WeatherDaily

    start = alert_date - timedelta(days=7)
    rows = (
        session.execute(
            select(WeatherDaily)
            .where(
                WeatherDaily.field_id == field_id,
                WeatherDaily.date >= start,
                WeatherDaily.date <= alert_date,
            )
            .order_by(WeatherDaily.date.desc())
        )
        .scalars()
        .all()
    )
    if not rows:
        return None

    latest = rows[0]
    precip_7d = sum(float(r.precipitation_sum or 0) for r in rows)
    et0_7d = sum(float(r.et0_fao_mm or 0) for r in rows)

    ctx: dict = {
        "period": f"{start.isoformat()} to {alert_date.isoformat()}",
        "precipitation_7d_mm": round(precip_7d, 1),
        "et0_7d_mm": round(et0_7d, 1),
    }
    if latest.water_balance_30d_mm is not None:
        ctx["water_deficit_mm"] = round(float(latest.water_balance_30d_mm), 1)
    if latest.soil_moisture_0_1cm is not None:
        ctx["soil_moisture_top"] = round(float(latest.soil_moisture_0_1cm), 3)
    if latest.gdd_cumulative is not None:
        ctx["gdd_cumulative"] = round(float(latest.gdd_cumulative), 1)
    if latest.drought_index is not None:
        ctx["drought_index"] = round(float(latest.drought_index), 2)
    return ctx


def run_alerts(
    session,
    field_id,
    scene_date: date,
    stats: dict,
    historical_means: list[float],
    index_def: IndexDef,
):
    """Evaluate threshold and drop alert rules for any index type."""
    from app.models.tables import Alert

    current_mean = stats.get("mean")
    if current_mean is None:
        return

    # Fetch recent weather context for alert enrichment
    weather_ctx = _get_weather_context(session, field_id, scene_date)

    alert_cfg = index_def.alerts
    label = index_def.label

    # threshold rule
    if current_mean < alert_cfg.threshold:
        severity = "high" if current_mean < alert_cfg.threshold_high else "medium"
        session.add(
            Alert(
                field_id=field_id,
                date=scene_date,
                severity=severity,
                rule_name=f"{index_def.key}_threshold",
                rule_params_json={"threshold": alert_cfg.threshold},
                message=(
                    f"{label} mean ({current_mean:.3f}) below threshold "
                    f"({alert_cfg.threshold}). Consider scouting."
                ),
                status="open",
                index_type=index_def.key,
                weather_context=weather_ctx,
            )
        )

    # drop rule
    if len(historical_means) >= 2:
        window = historical_means[-alert_cfg.drop_window :]
        rolling_avg = sum(window) / len(window)
        if rolling_avg > 0:
            drop_pct = ((rolling_avg - current_mean) / rolling_avg) * 100
            if drop_pct >= alert_cfg.drop_pct:
                severity = (
                    "high" if drop_pct >= 30 else "medium" if drop_pct >= 20 else "low"
                )
                session.add(
                    Alert(
                        field_id=field_id,
                        date=scene_date,
                        severity=severity,
                        rule_name=f"{index_def.key}_drop",
                        rule_params_json={
                            "drop_pct": alert_cfg.drop_pct,
                            "window": alert_cfg.drop_window,
                        },
                        message=(
                            f"{label} dropped {drop_pct:.1f}% "
                            f"(from avg {rolling_avg:.3f} to {current_mean:.3f}). "
                            f"Investigate crop stress."
                        ),
                        status="open",
                        index_type=index_def.key,
                        weather_context=weather_ctx,
                    )
                )
    session.commit()


# ── Grid / mask helpers ──────────────────────────────────────────────


def compute_target_grid(field_bounds: tuple, field_geom):
    """Return (target_transform, target_shape, field_mask, expanded_bounds)."""
    minx, miny, maxx, maxy = field_bounds
    buf = 0.001
    minx -= buf
    miny -= buf
    maxx += buf
    maxy += buf
    pixel_size = 0.0001
    width = max(int((maxx - minx) / pixel_size), 1)
    height = max(int((maxy - miny) / pixel_size), 1)
    max_dim = 5000
    if width > max_dim or height > max_dim:
        scale = max_dim / max(width, height)
        width = max(int(width * scale), 1)
        height = max(int(height * scale), 1)

    target_transform = from_bounds(minx, miny, maxx, maxy, width, height)
    target_shape = (height, width)
    field_mask = geometry_mask(
        [field_geom], out_shape=target_shape, transform=target_transform, invert=True
    )
    return target_transform, target_shape, field_mask, (minx, miny, maxx, maxy)


# ── Full per-scene processing ────────────────────────────────────────


def process_scene(
    session,
    job,
    scene: dict,
    scene_idx: int,
    total_scenes: int,
    index_def: IndexDef,
    target_transform,
    target_shape: tuple,
    field_mask: np.ndarray,
    bounds: tuple,
    org_id_str: str,
    field_id_str: str,
    date_from: date,
    date_to: date,
    historical_means: list[float],
    extra_params: dict | None = None,
):
    """Download bands, compute index, optionally write COG, stats, alerts.

    Agri / ``WRITE_INDEX_COGS=0`` skips COG upload and raster_layers. Agri
    lonlat is emitted by ``app.tasks.agri_lonlat`` (all optical indices in
    one pass), not here.

    Returns the stats dict on success, ``None`` on failure.
    """
    from app.models.tables import RasterLayer, FieldStat, Field

    from app.core.agri_tags import is_agri_tagged
    from app.core.index_cogs import write_index_cogs_enabled

    scene_id = scene["id"]
    scene_date = scene["date"]
    band_hrefs = scene["band_hrefs"]
    index_key = index_def.key
    compute_step = f"compute_{index_key}"
    field_row = session.get(Field, job.field_id)
    is_agri = bool(field_row and is_agri_tagged(field_row.tags_json))
    write_cogs = write_index_cogs_enabled(is_agri=is_agri)

    # -- download bands --
    update_job_progress(
        session,
        job,
        "download_bands",
        {"scene": scene_idx + 1, "total_scenes": total_scenes, "scene_id": scene_id},
    )
    bands: dict[str, np.ndarray] = {}
    for band_key, href in band_hrefs.items():
        bands[band_key] = read_band_windowed(
            href, bounds, target_shape, target_transform
        )
    complete_step(session, job, "download_bands")

    # -- compute index --
    update_job_progress(session, job, compute_step)
    kwargs = extra_params or {}
    index_data = index_def.formula(bands, **kwargs)
    index_data[~np.isfinite(index_data)] = np.nan
    index_data[~field_mask] = np.nan
    complete_step(session, job, compute_step)

    # -- write COG (classic OpenFarm / explicit WRITE_INDEX_COGS=1 only) --
    cog_uri = None
    if write_cogs:
        update_job_progress(session, job, "write_cog")
        cog_uri = write_cog(
            index_data,
            target_transform,
            "EPSG:4326",
            org_id_str,
            field_id_str,
            scene_date,
            index_key,
        )
        complete_step(session, job, "write_cog")
    else:
        logger.info(
            "cog_upload_skipped",
            object_key=f"cogs/{org_id_str}/{field_id_str}/{scene_date.isoformat()}/{index_key}.tif",
            index=index_key,
            is_agri=is_agri,
        )

    # -- compute stats --
    update_job_progress(session, job, "compute_stats")
    stats = compute_zonal_stats(index_data)
    valid = index_data[np.isfinite(index_data)]
    data_min = float(np.nanmin(valid)) if len(valid) > 0 else None
    data_max = float(np.nanmax(valid)) if len(valid) > 0 else None

    if write_cogs and cog_uri:
        # -- upsert RasterLayer (field_id + date + layer_type) --
        layer_values = dict(
            field_id=job.field_id,
            layer_type=index_def.label,
            satellite="S2",
            date=scene_date,
            cog_uri=cog_uri,
            min=data_min,
            max=data_max,
            params_json={
                "date_from": str(date_from),
                "date_to": str(date_to),
                "cloud_cover": scene["cloud_cover"],
                **kwargs,
            },
            provenance_json={
                "scene_id": scene_id,
                "bands": band_hrefs,
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "pipeline_version": "2.0.0",
            },
        )
        stmt = (
            pg_insert(RasterLayer)
            .values(**layer_values)
            .on_conflict_do_update(
                constraint="uq_raster_field_date_type",
                set_={
                    "cog_uri": cog_uri,
                    "min": data_min,
                    "max": data_max,
                    "params_json": layer_values["params_json"],
                    "provenance_json": layer_values["provenance_json"],
                },
            )
            .returning(RasterLayer.id)
        )
        layer_id = session.execute(stmt).scalar_one()
        session.flush()

        # -- upsert FieldStat (via layer_id which is now stable) --
        existing_stat = session.execute(
            select(FieldStat.id).where(
                FieldStat.field_id == job.field_id,
                FieldStat.date == scene_date,
                FieldStat.layer_id == layer_id,
            )
        ).scalar_one_or_none()

        stat_values = dict(
            mean=stats["mean"],
            median=stats["median"],
            min=stats["min"],
            max=stats["max"],
            p10=stats["p10"],
            p90=stats["p90"],
            stddev=stats["stddev"],
            quality_score=stats["quality_score"],
        )
        if existing_stat:
            session.execute(
                FieldStat.__table__.update()
                .where(FieldStat.id == existing_stat)
                .values(**stat_values)
            )
        else:
            field_stat = FieldStat(
                field_id=job.field_id, layer_id=layer_id, date=scene_date, **stat_values
            )
            session.add(field_stat)
        session.commit()
    complete_step(session, job, "compute_stats")

    # -- run alerts (skip for backfill jobs to avoid flooding) --
    is_backfill = (job.params_json or {}).get("is_backfill", False)
    update_job_progress(session, job, "run_alerts")
    if stats["mean"] is not None:
        historical_means.append(stats["mean"])
    if not is_backfill and not is_agri:
        run_alerts(
            session, job.field_id, scene_date, stats, historical_means, index_def
        )
    complete_step(session, job, "run_alerts")

    logger.info(
        "scene_processed",
        scene_id=scene_id,
        index=index_key,
        date=str(scene_date),
        mean=stats["mean"],
    )
    return stats


def process_scenes_parallel(
    *,
    job_id: str,
    scenes: list[dict],
    index_def: IndexDef,
    target_transform,
    target_shape: tuple,
    field_mask: np.ndarray,
    bounds: tuple,
    org_id_str: str,
    field_id_str: str,
    date_from: date,
    date_to: date,
    historical_means: list[float],
    extra_params: dict | None = None,
) -> int:
    """Download and process scenes concurrently. Returns layers_created.

    Each worker opens its own SQLAlchemy session (Session is not thread-safe).
    Per-scene failures are logged and skipped, matching the serial loop.
    ``historical_means`` is copied per scene so workers do not share a list;
    backfill jobs already skip alerts, and weekly jobs typically have one scene.
    """
    from app.models.tables import Job

    total = len(scenes)
    if total == 0:
        return 0

    workers = min(scene_max_workers(), total)
    hist_snapshot = list(historical_means)
    extra = extra_params
    logger.info(
        "scene_parallel_start",
        job_id=job_id,
        index=index_def.key,
        scenes=total,
        workers=workers,
    )

    def _one(scene: dict, scene_idx: int):
        session = get_db_session()
        try:
            job = session.get(Job, uuid.UUID(job_id))
            if job is None:
                logger.error("job_not_found_in_scene_worker", job_id=job_id)
                return None
            return process_scene(
                session=session,
                job=job,
                scene=scene,
                scene_idx=scene_idx,
                total_scenes=total,
                index_def=index_def,
                target_transform=target_transform,
                target_shape=target_shape,
                field_mask=field_mask,
                bounds=bounds,
                org_id_str=org_id_str,
                field_id_str=field_id_str,
                date_from=date_from,
                date_to=date_to,
                historical_means=list(hist_snapshot),
                extra_params=extra,
            )
        except Exception as e:
            logger.error(
                "scene_processing_error",
                scene_id=scene.get("id"),
                index=index_def.key,
                error=str(e),
            )
            try:
                session.rollback()
            except Exception:
                pass
            return None
        finally:
            session.close()

    layers_created = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, scene, i): scene for i, scene in enumerate(scenes)}
        for fut in as_completed(futures):
            scene = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                logger.error(
                    "scene_processing_error",
                    scene_id=scene.get("id"),
                    index=index_def.key,
                    error=str(e),
                )
                continue
            if result is not None:
                layers_created += 1

    logger.info(
        "scene_parallel_done",
        job_id=job_id,
        index=index_def.key,
        layers_created=layers_created,
        total_scenes=total,
        workers=workers,
    )
    return layers_created
