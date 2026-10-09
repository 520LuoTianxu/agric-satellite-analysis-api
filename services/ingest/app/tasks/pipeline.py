"""Shared vegetation-index pipeline helpers.

Extracted from the original ``ndvi.py`` so that every index task
(NDVI, EVI, SAVI, NDWI, NDMI, NDRE, CIRE, MNDWI) reuses the same STAC search, band download,
COG write, zonal-stats, and alert-evaluation logic.
"""

from __future__ import annotations

import os
import math
import time
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import rasterio
from pyproj import Geod
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds, transform_geom
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm.attributes import flag_modified

import structlog
from agric_satellite_analysis_common.agri_classify import decloud_scene_id_sql

from app.core.band_parallel import (
    BandReadResult,
    band_max_workers,
    gdal_read_slot,
    run_parallel_band_jobs,
)
from app.core.config import scene_max_workers
from app.core.stac_client import open_stac_client
from app.tasks.indices import IndexDef

from agric_satellite_analysis_common.quality_metrics import PARCEL_VALID_FRACTION_V1

logger = structlog.get_logger()
_WGS84_GEOD = Geod(ellps="WGS84")
_TARGET_GRID_CELL_SIZE_M = 10.0
_MAX_ANALYSIS_GRID_CELLS = 4_000_000
INDEX_PIPELINE_VERSION = "2.3.0"

# ── Configuration (same as ndvi.py) ──────────────────────────────────

STAC_API_URL = os.environ.get(
    "STAC_API_URL", "https://earth-search.aws.element84.com/v1"
)
S2_PC_STAC_API_URL = os.environ.get(
    "S2_PC_STAC_API_URL", "https://planetarycomputer.microsoft.com/api/stac/v1"
)
STAC_COLLECTION = "sentinel-2-l2a"
# STAC eo:cloud_cover filter (scene-level). Stricter than the 30% agri
# product skip (parcel_cloud_cover_pct / drought display). Keep search
# conservative unless decloud raises the cap (see DECLOUD_STAC_CLOUD_MAX_PCT).
MAX_CLOUD_COVER = 20
# GDAL environment for reading remote COGs
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.TIF,.tiff")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")
os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
os.environ.setdefault("CPL_VSIL_CURL_USE_HEAD", "NO")
# 远程窗口很小，长尾主要来自连接或 Range 请求挂起。由 GDAL/curl 在 C 调用内部
# 中断阻塞；Python Future 超时无法安全终止正在执行的 rasterio/GDAL 调用。
os.environ.setdefault("GDAL_HTTP_CONNECTTIMEOUT", "10")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
os.environ.setdefault("GDAL_HTTP_LOW_SPEED_LIMIT", "1")
os.environ.setdefault("GDAL_HTTP_LOW_SPEED_TIME", "30")
# 应用层统一尝试三次时，GDAL 内部只允许一次补偿，避免两层重试相乘。
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "1")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "1")
# TCP keepalive 配置从 GDAL 3.6 起生效；旧运行时会忽略，不改变 SSL 校验。
os.environ.setdefault("GDAL_HTTP_TCP_KEEPALIVE", "YES")
os.environ.setdefault("GDAL_HTTP_TCP_KEEPIDLE", "30")
os.environ.setdefault("GDAL_HTTP_TCP_KEEPINTVL", "15")
os.environ.setdefault("VSI_CACHE", "TRUE")
os.environ.setdefault("VSI_CACHE_SIZE", "5000000")
# Scene and band threads each open their own datasets; keep GDAL's internal
# pool at 1 so scene x band parallelism does not spawn nested GDAL storms.
os.environ.setdefault("GDAL_NUM_THREADS", "1")

# Serialize job.progress_json updates. SQLAlchemy Session and the Job row
# are not thread-safe; each scene worker uses its own session and this lock.
_job_progress_lock = threading.Lock()

RETRY_DELAYS = [60, 300, 900]  # Per PRD Section 7.4


# ── Storage / DB helpers ─────────────────────────────────────────────


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
    session, land_id, layer_type: str, satellite: str | None = None
) -> set[date]:
    """Dates already present in raster_layers for this land parcel + layer type."""
    from app.models.tables import RasterLayer

    q = select(RasterLayer.date).where(
        RasterLayer.land_id == land_id, RasterLayer.layer_type == layer_type
    )
    if satellite is not None:
        q = q.where(RasterLayer.satellite == satellite)
    rows = session.execute(q).scalars().all()
    return {d for d in rows if d is not None}



def existing_agri_scene_dates(session, land_id: str, sensor: str) -> set[date]:
    """Dates already present in agric_satellite.parcel_scene_products for land_id + sensor.

    Prefers internal HTTP when ``API_BASE_URL`` + ``INTERNAL_API_TOKEN`` are set
    (download-host D2); falls back to SyncSession otherwise.
    """
    from app.core.date_coerce import coerce_to_date, dates_from_sql_rows

    try:
        from agric_satellite_analysis_common.internal_api import agri_scene_dates, internal_api_enabled
    except ImportError:
        internal_api_enabled = lambda: False  # noqa: E731
        agri_scene_dates = None  # type: ignore

    if agri_scene_dates is not None and internal_api_enabled():
        try:
            iso_dates = agri_scene_dates(str(land_id), sensor=str(sensor))
            out: set[date] = set()
            for raw in iso_dates:
                d = coerce_to_date(raw)
                if d is not None:
                    out.add(d)
            return out
        except Exception as e:
            logger.warning(
                "agri_scene_dates_http_failed falling_back_db",
                land_id=str(land_id),
                sensor=sensor,
                error=str(e),
            )

    # http_only callers pass session=None; treat as no existing dates.
    if session is None:
        return set()

    from sqlalchemy import text as sa_text

    rows = session.execute(
        sa_text(
            f"""
            SELECT DISTINCT date
            FROM agric_satellite.parcel_scene_products
            WHERE land_id = :land_id
              AND sensor = :sensor
              AND NOT ({decloud_scene_id_sql()})
              -- 来源元数据兼容旧 JSONB，避免去云产品日期让原始场景被跳过。
              AND LOWER(COALESCE(NULLIF(BTRIM(product_source), ''), NULLIF(BTRIM(pixel_data->>'source'), ''), '')) <> 'uncrtaints_decloud'
            """
        ),
        {"land_id": str(land_id), "sensor": sensor},
    ).fetchall()
    return dates_from_sql_rows(rows)


def collect_existing_scene_dates(
    session, land_id: str, *, layer_type: str, satellite: str, agri_sensor: str | None = None
) -> set[date]:
    """Union of derived raster dates and scene-product dates for one land_id."""
    existing = existing_layer_dates(session, land_id, layer_type, satellite=satellite)
    sensor = agri_sensor or satellite
    try:
        existing |= existing_agri_scene_dates(session, land_id, sensor)
    except Exception as e:
        logger.warning(
            "existing_scene_dates_failed",
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
    land_id: str | None = None,
    index: str | None = None,
) -> list[dict]:
    """按地块已有原始观测日期跳过重复场景；force用于明确要求重算。"""
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
            land_id=land_id,
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


def _s2_baseline_radiometry(item: Any) -> dict[str, float] | None:
    """按Sentinel-2处理基线推导L2A反射率比例与加性偏移。"""
    properties = getattr(item, "properties", {}) or {}
    baseline = properties.get("s2:processing_baseline")
    if baseline is None:
        return None
    try:
        major = int(str(baseline).strip().split(".", 1)[0])
    except (TypeError, ValueError):
        return None
    if not 0 <= major <= 99:
        return None

    # PB 04.00起DN增加了-1000偏移；转反射率时必须同时还原量化比例。
    return {"scale": 0.0001, "offset": -0.1 if major >= 4 else 0.0}


def _boa_offset_already_applied(item: Any) -> bool:
    """STAC 条目声明 DN 已扣除 BOA 偏移（Earth Search ``sentinel-2-l2a``）。"""
    properties = getattr(item, "properties", {}) or {}
    raw = properties.get("earthsearch:boa_offset_applied")
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "1", "yes"}
    return raw is True


def _resolve_band_radiometry(
    item: Any, index_defs: list[IndexDef]
) -> tuple[
    dict[str, dict[str, float]] | None,
    str | None,
    dict[str, str],
    str | None,
]:
    """解析每个光谱波段的定标值；缺少可靠依据时返回原因并跳过该景。"""
    fallback = _s2_baseline_radiometry(item)
    radiometry: dict[str, dict[str, float]] = {}
    sources: dict[str, str] = {}
    # Earth Search v1 ``sentinel-2-l2a`` 的 COG 已扣除 PB≥04.00 的 BOA 偏移
    # （earthsearch:boa_offset_applied=true），但 raster:bands 仍声明 offset=-0.1；
    # 再扣一次会让暗目标反射率变负、NDVI 饱和为 1.0，故这类条目偏移必须置 0。
    offset_applied = _boa_offset_already_applied(item)

    for index_def in index_defs:
        for band_key in index_def.bands:
            if band_key in radiometry:
                continue
            asset = None
            for asset_name in index_def.stac_asset_map.get(
                band_key, (band_key,)
            ):
                candidate = item.assets.get(asset_name)
                if candidate and getattr(candidate, "href", None):
                    asset = candidate
                    break
            if asset is None:
                return None, None, sources, f"missing_asset_{band_key}"

            extra_fields = getattr(asset, "extra_fields", {}) or {}
            raster_bands = extra_fields.get("raster:bands") or []
            raster_band = (
                raster_bands[0]
                if isinstance(raster_bands, list) and raster_bands
                else {}
            )
            if not isinstance(raster_band, dict):
                raster_band = {}

            raw_scale = raster_band.get("scale")
            raw_offset = raster_band.get("offset")
            has_extension_value = raw_scale is not None or raw_offset is not None
            if not has_extension_value and fallback is None:
                return None, None, sources, f"missing_calibration_{band_key}"

            try:
                # 对未给出的扩展字段优先沿用处理基线；仍无信息时使用STAC默认值。
                scale = float(
                    raw_scale
                    if raw_scale is not None
                    else (fallback["scale"] if fallback else 1.0)
                )
                offset = float(
                    raw_offset
                    if raw_offset is not None
                    else (fallback["offset"] if fallback else 0.0)
                )
            except (TypeError, ValueError):
                return None, None, sources, f"invalid_calibration_{band_key}"
            if (
                not math.isfinite(scale)
                or scale <= 0
                or not math.isfinite(offset)
            ):
                return None, None, sources, f"invalid_calibration_{band_key}"

            if offset_applied and offset != 0.0:
                offset = 0.0
            radiometry[band_key] = {"scale": scale, "offset": offset}
            if raw_scale is not None and raw_offset is not None:
                sources[band_key] = "stac_raster_bands"
            elif has_extension_value and fallback is not None:
                sources[band_key] = "stac_raster_bands+baseline_fallback"
            elif has_extension_value:
                sources[band_key] = "stac_raster_bands_defaults"
            else:
                sources[band_key] = "s2_processing_baseline"
            if offset_applied:
                sources[band_key] += "+boa_offset_already_applied"

    unique_sources = set(sources.values())
    source = next(iter(unique_sources)) if len(unique_sources) == 1 else "mixed"
    return radiometry, source, sources, None


def _resolve_extra_asset_hrefs(
    item: Any,
    extra_assets: dict[str, tuple[str, ...]] | None,
) -> dict[str, str]:
    """Optional STAC assets (e.g. SCL). Missing extras do not drop the scene."""
    if not extra_assets:
        return {}
    out: dict[str, str] = {}
    for key, names in extra_assets.items():
        for asset_name in names:
            asset = item.assets.get(asset_name)
            if asset and getattr(asset, "href", None):
                out[key] = asset.href
                break
    return out


def search_scenes_for_defs(
    land_geom_geojson: dict,
    date_from: date,
    date_to: date,
    index_defs: list[IndexDef],
    *,
    index_label: str | None = None,
    max_cloud_cover: float | None = None,
    extra_assets: dict[str, tuple[str, ...]] | None = None,
    cloud_dedupe: str = "week",
    max_items: int | None = None,
    catalog_url: str | None = None,
    planetary_computer_signing: bool = False,
    source_catalog: str | None = None,
) -> list[dict]:
    """Search Element84 STAC and resolve HREFs for the union of index bands.

    ``cloud_dedupe``:
      - ``week`` (default): one lowest-cloud scene per ISO week (legacy NDVI).
      - ``day``: one lowest-cloud scene per calendar day.
      - ``none``: keep every matching STAC item (agri + decloud need full series).

    云量去重策略会改变时序采样密度；农业完整序列和去云处理必须显式选择none，
    否则默认周优选会漏掉同周内的有效观测。
    """
    if not index_defs:
        return []
    label = index_label or ",".join(d.key for d in index_defs)
    cloud_cap = MAX_CLOUD_COVER if max_cloud_cover is None else float(max_cloud_cover)
    dedupe = (cloud_dedupe or "week").strip().lower()
    if dedupe not in ("week", "day", "none"):
        dedupe = "week"
    # max_items是所有分页累计返回的总上限；未明确要求截断时必须读取完整时序。
    item_cap = int(max_items) if max_items is not None else None
    t0 = time.perf_counter()
    selected_catalog_url = catalog_url or STAC_API_URL
    if planetary_computer_signing:
        # PC 的私有化签名链接必须通过官方 signer 即时生成 SAS，过期后不缓存。
        import planetary_computer as pc

        catalog = open_stac_client(selected_catalog_url, modifier=pc.sign_inplace)
    else:
        catalog = open_stac_client(selected_catalog_url)
    search = catalog.search(
        collections=[STAC_COLLECTION],
        intersects=land_geom_geojson,
        datetime=f"{date_from.isoformat()}/{date_to.isoformat()}",
        # lte so DECLOUD_STAC_CLOUD_MAX_PCT=100 still includes 100.0% scenes
        query={"eo:cloud_cover": {"lte": cloud_cap}},
        max_items=item_cap,
    )
    items = list(search.items())
    logger.info(
        "stac_search_results",
        count=len(items),
        index=label,
        date_from=str(date_from),
        date_to=str(date_to),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        max_cloud_cover=cloud_cap,
        cloud_dedupe=dedupe,
        max_items=item_cap,
    )
    if not items:
        return []

    selected: list[dict[str, Any]] = []
    if dedupe == "none":
        for item in items:
            item_date = item.datetime.date() if item.datetime else date_from
            cloud = item.properties.get("eo:cloud_cover", 100)
            selected.append({"item": item, "cloud": cloud, "date": item_date})
        selected.sort(key=lambda e: (e["date"], e["cloud"], e["item"].id))
    else:
        buckets: dict[str, Any] = {}
        for item in items:
            item_date = item.datetime.date() if item.datetime else date_from
            if dedupe == "day":
                key = item_date.isoformat()
            else:
                week_key = item_date.isocalendar()[:2]
                key = f"{week_key[0]}-W{week_key[1]:02d}"
            cloud = item.properties.get("eo:cloud_cover", 100)
            if key not in buckets or cloud < buckets[key]["cloud"]:
                buckets[key] = {"item": item, "cloud": cloud, "date": item_date}
        selected = [buckets[k] for k in sorted(buckets.keys())]

    scenes = []
    for entry in selected:
        item = entry["item"]
        band_hrefs = _resolve_band_hrefs(item, index_defs)
        if band_hrefs:
            (
                band_radiometry,
                band_radiometry_source,
                band_radiometry_sources,
                radiometry_error,
            ) = _resolve_band_radiometry(item, index_defs)
            if band_radiometry is None:
                # 指数常数按物理反射率定义，缺少定标元数据时不能静默用DN计算。
                logger.warning(
                    "stac_scene_missing_radiometry",
                    scene_id=item.id,
                    index=label,
                    processing_baseline=(item.properties or {}).get(
                        "s2:processing_baseline"
                    ),
                    reason=radiometry_error,
                )
                continue
            band_hrefs.update(_resolve_extra_asset_hrefs(item, extra_assets))
            scenes.append(
                {
                    "id": item.id,
                    "date": entry["date"],
                    "cloud_cover": entry["cloud"],
                    "band_hrefs": band_hrefs,
                    # 聚合下载需要按景覆盖范围筛选地块，避免写入景外的填充值。
                    "geometry": item.geometry,
                    "source_catalog": source_catalog or "element84",
                    "band_radiometry": band_radiometry,
                    "band_radiometry_source": band_radiometry_source,
                    "band_radiometry_sources": band_radiometry_sources,
                }
            )
        else:
            logger.warning("missing_bands", scene_id=item.id, index=label)

    return scenes


def search_scenes(
    land_geom_geojson: dict, date_from: date, date_to: date, index_def: IndexDef
) -> list[dict]:
    """Search Element84 STAC and resolve per-band HREFs for the given index."""
    return search_scenes_for_defs(
        land_geom_geojson, date_from, date_to, [index_def], index_label=index_def.key
    )


# ── Band reading ─────────────────────────────────────────────────────


def _read_band_windowed_profiled(
    href: str,
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    *,
    target_crs: str = "EPSG:4326",
    resampling: Resampling = Resampling.bilinear,
) -> BandReadResult[np.ndarray]:
    """读取远程 COG 窗口，并分别记录远端 I/O 与本地重投影耗时。"""
    # SCL 与 RGB 直读也必须占用进程内名额，不能绕过波段池的并发限制。
    with gdal_read_slot(), rasterio.Env():
        t_io = time.perf_counter()
        with rasterio.open(href) as src:
            src_bounds = transform_bounds("EPSG:4326", src.crs, *bounds)
            window = rasterio.windows.from_bounds(*src_bounds, transform=src.transform)
            # 用Rasterio掩膜同时保留源NoData与boundless窗口外区域，避免0参与双线性插值。
            data = src.read(1, window=window, boundless=True, masked=True)
            data = np.asarray(data.astype(np.float32).filled(np.nan))
            source_transform = rasterio.windows.transform(window, src.transform)
            source_crs = src.crs
        io_ms = int((time.perf_counter() - t_io) * 1000)

        t_reproject = time.perf_counter()
        dst = np.full(target_shape, np.nan, dtype=np.float32)
        reproject(
            source=data,
            destination=dst,
            src_transform=source_transform,
            src_crs=source_crs,
            dst_transform=target_transform,
            dst_crs=target_crs,
            src_nodata=np.nan,
            dst_nodata=np.nan,
            resampling=resampling,
        )
        reproject_ms = int((time.perf_counter() - t_reproject) * 1000)
        return BandReadResult(dst, io_ms=io_ms, reproject_ms=reproject_ms)


def read_band_windowed(
    href: str,
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    *,
    target_crs: str = "EPSG:4326",
    resampling: Resampling = Resampling.bilinear,
) -> np.ndarray:
    """Read a band from a remote COG, windowed to field extent.

    Caller must treat this as one GDAL dataset open. Band workers each call
    this under their own ``rasterio.Env()`` (this function opens one).
    Categorical layers (SCL) must pass ``resampling=Resampling.nearest``.

    读取窗口限制网络范围；连续反射率波段与分类SCL使用不同重采样方式，
    避免把分类编码插值成不存在的类别。
    """
    return _read_band_windowed_profiled(
        href,
        bounds,
        target_shape,
        target_transform,
        target_crs=target_crs,
        resampling=resampling,
    ).value


def read_bands_windowed_parallel(
    band_hrefs: dict[str, str],
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    *,
    target_crs: str = "EPSG:4326",
    scene_workers: int = 1,
    resampling_by_band: dict[str, Resampling] | None = None,
    log_context: dict[str, object] | None = None,
) -> dict[str, np.ndarray]:
    """同景波段并行读取；可为 SCL 等分类波段指定 nearest 重采样。"""

    def _one(band_key: str, href: str) -> BandReadResult[np.ndarray]:
        resampling = (resampling_by_band or {}).get(
            band_key, Resampling.bilinear
        )
        return _read_band_windowed_profiled(
            href,
            bounds,
            target_shape,
            target_transform,
            target_crs=target_crs,
            resampling=resampling,
        )

    return run_parallel_band_jobs(
        band_hrefs,
        _one,
        scene_workers=scene_workers,
        log_context=log_context,
    )


def read_rgb_windowed(
    href: str,
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    *,
    target_crs: str = "EPSG:4326",
    resampling: Resampling = Resampling.bilinear,
) -> np.ndarray | None:
    """Read a 3-band visual/true_color COG windowed to the target grid.

    Returns HxWx3 float32 (or None if the asset has fewer than 3 bands).
    Element84 ``visual`` is typically uint8 RGB; values are preserved.
    """
    # SCL 与 RGB 直读也必须占用进程内名额，不能绕过波段池的并发限制。
    with gdal_read_slot(), rasterio.Env():
        with rasterio.open(href) as src:
            if src.count < 3:
                return None
            src_bounds = transform_bounds("EPSG:4326", src.crs, *bounds)
            window = rasterio.windows.from_bounds(*src_bounds, transform=src.transform)
            # RGB预览同样屏蔽景幅外像元，避免边缘黑边被平滑扩散进地块。
            data = src.read([1, 2, 3], window=window, boundless=True, masked=True)
            data = data.astype(np.float32).filled(np.nan)
            dst = np.full((3, *target_shape), np.nan, dtype=np.float32)
            for i in range(3):
                reproject(
                    source=data[i],
                    destination=dst[i],
                    src_transform=rasterio.windows.transform(window, src.transform),
                    src_crs=src.crs,
                    dst_transform=target_transform,
                    dst_crs=target_crs,
                    src_nodata=np.nan,
                    dst_nodata=np.nan,
                    resampling=resampling,
                )
            return np.transpose(dst, (1, 2, 0))


# ── COG writing ──────────────────────────────────────────────────────


def write_cog(
    data: np.ndarray,
    transform,
    crs: str,
    org_id: str,
    land_id: str,
    scene_date: date,
    index_key: str,
) -> str:
    """把指数数组写为云优化GeoTIFF并返回对象URI，供按窗口读取和地图服务使用。"""
    # rio-cogeo 只在真正写 COG 时需要；把重型可选依赖延迟到这里，避免
    # HTTP-only 编排和日期检查在不写本地 COG 的场景下无法加载任务模块。
    from rio_cogeo.cogeo import cog_translate
    from rio_cogeo.profiles import cog_profiles

    object_key = f"cogs/{org_id}/{land_id}/{scene_date.isoformat()}/{index_key}.tif"
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


def compute_zonal_stats(
    data: np.ndarray, *, expected_mask: np.ndarray | None = None
) -> dict:
    """计算有效像元统计；有地块掩膜时质量分只以地块内格点为分母。"""
    if expected_mask is None:
        valid_domain = None
        total_pixels = int(data.size)
    else:
        valid_domain = np.asarray(expected_mask, dtype=bool)
        if tuple(valid_domain.shape) != tuple(data.shape):
            raise ValueError("expected mask dimensions must match zonal data")
        total_pixels = int(np.count_nonzero(valid_domain))

    finite_mask = np.isfinite(data)
    if valid_domain is not None:
        finite_mask &= valid_domain
    valid = data[finite_mask]
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


def _get_weather_context(session, land_id, alert_date: date) -> dict | None:
    """Query recent weather data to build context JSONB for alert enrichment."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.models.tables import WeatherDaily

    start = alert_date - timedelta(days=7)
    rows = (
        session.execute(
            select(WeatherDaily)
            .where(
                WeatherDaily.land_id == land_id,
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
    land_id,
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
    weather_ctx = _get_weather_context(session, land_id, scene_date)

    alert_cfg = index_def.alerts
    label = index_def.label

    # threshold rule
    if current_mean < alert_cfg.threshold:
        severity = "high" if current_mean < alert_cfg.threshold_high else "medium"
        session.add(
            Alert(
                land_id=land_id,
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
                        land_id=land_id,
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


def analysis_crs_for_bounds(field_bounds: tuple) -> str:
    """根据WGS84范围中心选择UTM或极区UPS坐标系，供米制遥感分析使用。"""
    minx, miny, maxx, maxy = (float(value) for value in field_bounds)
    if minx >= maxx or miny >= maxy:
        raise ValueError("parcel bounds must have positive width and height")
    center_lon = (minx + maxx) / 2
    center_lat = (miny + maxy) / 2
    if not all(math.isfinite(value) for value in (center_lon, center_lat)):
        raise ValueError("parcel bounds must contain finite WGS84 coordinates")
    if minx < -180 or maxx > 180 or miny < -90 or maxy > 90:
        raise ValueError("parcel bounds are outside the WGS84 coordinate range")
    # 农业地块通常是区域级范围，按中心点选UTM可让目标像元直接以米计量。
    # 极区超出UTM适用纬度时改用UPS，避免错误投影或静默退回经纬度网格。
    if center_lat >= 84:
        return "EPSG:32661"
    if center_lat <= -80:
        return "EPSG:32761"
    zone = min(60, max(1, int((center_lon + 180) // 6) + 1))
    return f"EPSG:{(32600 if center_lat >= 0 else 32700) + zone}"


def compute_target_grid(
    field_bounds: tuple,
    land_geom,
    *,
    padding_degrees: float = 0.001,
    target_crs: str | None = None,
):
    """构建局部米制目标网格，并返回变换、尺寸、地块掩膜和扩展后的WGS84范围。

    固定公里级处理窗口传入 ``padding_degrees=0``，避免在请求范围外再加经纬度缓冲；
    其他旧调用方默认沿用历史缓冲。网格单边最多5000格、总格点最多400万，超限自动降采样。
    """
    if len(field_bounds) != 4:
        raise ValueError("field bounds must contain minx, miny, maxx, maxy")
    minx, miny, maxx, maxy = (float(value) for value in field_bounds)
    if (
        not all(math.isfinite(value) for value in (minx, miny, maxx, maxy))
        or minx >= maxx
        or miny >= maxy
        or minx < -180
        or maxx > 180
        or miny < -90
        or maxy > 90
    ):
        raise ValueError("field bounds must be an ordered WGS84 rectangle")
    buf = float(padding_degrees)
    if not math.isfinite(buf) or buf < 0:
        raise ValueError("padding_degrees must be finite and non-negative")
    minx -= buf
    miny -= buf
    maxx += buf
    maxy += buf
    output_crs = target_crs or analysis_crs_for_bounds((minx, miny, maxx, maxy))
    # STAC窗口仍以WGS84检索；只有目标栅格边界和地块掩膜转换为米制投影。
    projected_bounds = transform_bounds(
        "EPSG:4326", output_crs, minx, miny, maxx, maxy, densify_pts=21
    )
    left, bottom, right, top = projected_bounds
    if not all(math.isfinite(value) for value in projected_bounds):
        raise ValueError("parcel bounds could not be projected to the analysis CRS")
    width = max(math.ceil((right - left) / _TARGET_GRID_CELL_SIZE_M), 1)
    height = max(math.ceil((top - bottom) / _TARGET_GRID_CELL_SIZE_M), 1)
    max_dim = 5000
    # 只限制单边会允许5000×5000的2500万格点；多个波段并行读取时容易放大内存峰值。
    scale = min(
        1.0,
        max_dim / max(width, height),
        math.sqrt(_MAX_ANALYSIS_GRID_CELLS / (width * height)),
    )
    if scale < 1.0:
        width = max(int(width * scale), 1)
        height = max(int(height * scale), 1)

    target_transform = from_bounds(left, bottom, right, top, width, height)
    target_shape = (height, width)
    # 掩膜几何与像元 transform 必须在同一投影坐标系，才能保证像元确实位于地块内。
    projected_geom = transform_geom("EPSG:4326", output_crs, land_geom.__geo_interface__)
    field_mask = geometry_mask(
        [projected_geom],
        out_shape=target_shape,
        transform=target_transform,
        invert=True,
    )
    return target_transform, target_shape, field_mask, (minx, miny, maxx, maxy)


def describe_target_grid(
    target_transform, target_shape: tuple, target_crs: str
) -> dict[str, Any]:
    """记录分析网格的坐标系和实际像元间距，不把输出网格误称为传感器原生分辨率。"""
    height, width = (int(value) for value in target_shape)
    if height < 1 or width < 1:
        raise ValueError("analysis grid dimensions must be positive")

    if str(target_crs).upper() in {"EPSG:4326", "OGC:CRS84"}:
        # 兼容旧栅格的元数据读取；新分析网格均使用米制投影。
        col, row = width / 2, height / 2
        x0, y0 = target_transform * (col, row)
        x1, y1 = target_transform * (col + 1, row)
        x2, y2 = target_transform * (col, row + 1)
        spacing_x = abs(float(_WGS84_GEOD.inv(x0, y0, x1, y1)[2]))
        spacing_y = abs(float(_WGS84_GEOD.inv(x0, y0, x2, y2)[2]))
        measurement = "wgs84_geodesic_estimate"
    else:
        spacing_x = abs(float(target_transform.a))
        spacing_y = abs(float(target_transform.e))
        measurement = "projected_axis_spacing"
    if not math.isfinite(spacing_x) or not math.isfinite(spacing_y):
        raise ValueError("analysis grid spacing could not be measured")
    return {
        "crs": str(target_crs),
        "width": width,
        "height": height,
        "cell_size_m": {"x": round(spacing_x, 2), "y": round(spacing_y, 2)},
        "target_cell_size_m": _TARGET_GRID_CELL_SIZE_M,
        "measurement": measurement,
    }


def describe_target_grid_wgs84(target_transform, target_shape: tuple) -> dict[str, Any]:
    """兼容旧调用方，描述仍以WGS84存储的历史分析网格。"""
    return describe_target_grid(target_transform, target_shape, "EPSG:4326")


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
    target_crs: str,
    org_id_str: str,
    land_id_str: str,
    date_from: date,
    date_to: date,
    historical_means: list[float],
    extra_params: dict | None = None,
    scene_workers: int = 1,
):
    """Download bands, compute index, optionally write COG, stats, alerts.

    Canonical lonlat products are emitted by ``app.tasks.agri_lonlat`` (all
    optical indices in one pass). COG upload and the optional raster statistics
    copy are disabled unless ``WRITE_INDEX_COGS=1`` is explicitly configured.

    ``scene_workers`` is the parent scene-pool size so band ThreadPool size
    can be nested-capped (see ``app.core.band_parallel``).

    Returns the stats dict on success, ``None`` on failure.

    主农业时序由agri_lonlat一次遍历生成全部指数；此路径保留单指数栅格和告警兼容，
    避免把旧图层消费者误当作主像元数据来源。
    """
    from app.models.tables import RasterLayer, FieldStat
    from app.core.index_cogs import write_index_cogs_enabled

    scene_id = scene["id"]
    scene_date = scene["date"]
    band_hrefs = scene["band_hrefs"]
    index_key = index_def.key
    compute_step = f"compute_{index_key}"
    write_cogs = write_index_cogs_enabled()

    # -- download bands --
    update_job_progress(
        session,
        job,
        "download_bands",
        {"scene": scene_idx + 1, "total_scenes": total_scenes, "scene_id": scene_id},
    )
    bands = read_bands_windowed_parallel(
        band_hrefs,
        bounds,
        target_shape,
        target_transform,
        target_crs=target_crs,
        scene_workers=scene_workers,
        log_context={
            "job_id": str(getattr(job, "id", "")) or None,
            "scene_id": scene_id,
            "date": scene_date.isoformat(),
            "sensor": "S2",
        },
    )
    complete_step(session, job, "download_bands")

    # -- compute index --
    update_job_progress(session, job, compute_step)
    kwargs = extra_params or {}
    # 指数计算接收物理反射率定标；额外参数只用于原有公式选项，避免被场景元数据覆盖。
    formula_kwargs = {**kwargs, "band_radiometry": scene["band_radiometry"]}
    index_data = index_def.formula(bands, **formula_kwargs)
    index_data[~np.isfinite(index_data)] = np.nan
    index_data[~field_mask] = np.nan
    complete_step(session, job, compute_step)

    # -- optional COG copy (only when explicitly enabled) --
    cog_uri = None
    if write_cogs:
        update_job_progress(session, job, "write_cog")
        cog_uri = write_cog(
            index_data,
            target_transform,
            target_crs,
            org_id_str,
            land_id_str,
            scene_date,
            index_key,
        )
        complete_step(session, job, "write_cog")
    else:
        logger.info(
            "cog_upload_skipped",
            object_key=f"cogs/{org_id_str}/{land_id_str}/{scene_date.isoformat()}/{index_key}.tif",
            index=index_key,
        )

    # -- compute stats --
    update_job_progress(session, job, "compute_stats")
    stats = compute_zonal_stats(index_data, expected_mask=field_mask)
    valid = index_data[np.isfinite(index_data)]
    data_min = float(np.nanmin(valid)) if len(valid) > 0 else None
    data_max = float(np.nanmax(valid)) if len(valid) > 0 else None

    if write_cogs and cog_uri:
        # -- upsert RasterLayer (land_id + date + layer_type) --
        layer_values = dict(
            land_id=job.land_id,
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
                "band_radiometry": scene["band_radiometry"],
                "band_radiometry_source": scene["band_radiometry_source"],
                "band_radiometry_sources": scene["band_radiometry_sources"],
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "pipeline_version": INDEX_PIPELINE_VERSION,
                "quality_score_method": PARCEL_VALID_FRACTION_V1,
            },
        )
        stmt = (
            pg_insert(RasterLayer)
            .values(**layer_values)
            .on_conflict_do_update(
                constraint="uq_raster_land_date_type",
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
                FieldStat.land_id == job.land_id,
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
                land_id=job.land_id, layer_id=layer_id, date=scene_date, **stat_values
            )
            session.add(field_stat)
        session.commit()
    complete_step(session, job, "compute_stats")

    # -- run alerts (skip for backfill jobs to avoid flooding) --
    is_backfill = (job.params_json or {}).get("is_backfill", False)
    update_job_progress(session, job, "run_alerts")
    if stats["mean"] is not None:
        historical_means.append(stats["mean"])
    # canonical land 任务都使用同一套告警计算；仅批量补算跳过告警，避免历史数据回填时刷屏。
    if not is_backfill:
        run_alerts(
            session, job.land_id, scene_date, stats, historical_means, index_def
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
    target_crs: str,
    org_id_str: str,
    land_id_str: str,
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

    每景独立Session并使用历史均值快照，失败景单独记录后继续处理，避免线程共享会话或修改同一列表。
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
        band_gdal_cap=band_max_workers(),
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
                target_crs=target_crs,
                org_id_str=org_id_str,
                land_id_str=land_id_str,
                date_from=date_from,
                date_to=date_to,
                historical_means=list(hist_snapshot),
                extra_params=extra,
                scene_workers=workers,
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
