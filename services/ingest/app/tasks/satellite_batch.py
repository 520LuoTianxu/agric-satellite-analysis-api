"""按聚合窗口下载一次影像，在内存中裁到请求地块后回调 API 结果缓存。"""

import os
import time
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date

import numpy as np
import structlog
from rasterio.features import geometry_mask
from rasterio.warp import Resampling, reproject, transform_geom
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from agric_satellite_analysis_common.internal_api import (
    agri_satellite_batch_inputs,
    get_job,
    internal_client,
    patch_job,
)
from agric_satellite_analysis_common.scheduled_land_filter import (
    is_scheduled_land_allowed,
)
from app.core.band_parallel import band_max_workers, run_parallel_band_jobs
from app.core.band_window_cache import (
    read_scene_window,
    window_cache_enabled,
    write_scene_window,
)
from app.core.config import scene_max_workers, scene_straggler_timeout_sec
from app.core.agri_classify import PARCEL_CLOUD_SOURCE_SCL
from app.core.decloud import (
    decloud_enabled,
    decloud_s2_extra_assets,
    decloud_stac_cloud_max_pct,
    filter_scenes_outside_season_high_cloud,
    normalize_season_months,
)
from app.core.processing_window import (
    build_complete_processing_window,
    resolve_processing_window_km,
)
from app.core.true_color_preview import upload_field_rgb_preview
from app.tasks.agri_lonlat import (
    INDEX_KEY_TO_PIXEL,
    SCL_STAC_ASSETS,
    agri_optical_index_defs,
    emit_optical_lonlat,
    parcel_cloud_from_scl_window,
)
from app.tasks.pipeline import (
    S2_PC_STAC_API_URL,
    analysis_crs_for_bounds,
    compute_target_grid,
    compute_zonal_stats,
    describe_target_grid,
    read_bands_windowed_parallel,
    search_scenes_for_defs,
)
from app.tasks.sentinel1 import (
    _read_band_windowed_db_profiled,
    _s1_radiometric_calibration,
    _sample_s1_lonlat,
    _upsert_agri_s1,
    search_s1_scenes,
)
from app.worker import celery_app
from celery.exceptions import MaxRetriesExceededError, SoftTimeLimitExceeded

logger = structlog.get_logger()

SATELLITE_BATCH_MAX_COMPENSATIONS = 2
SATELLITE_COMPENSATION_DELAY_SECONDS = 30
SATELLITE_BATCH_INPUTS_LAND_LIMIT = 50


def _positive_limit_env(name: str, default: int) -> int:
    try:
        return max(60, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _nonneg_int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# 任务级软超时默认 12 分钟；硬超时略高以便 soft 路径可 retry。
# soft 可能打断不了 GDAL C 调用，最终由 hard limit 回收 prefork 子进程；
# hard 杀进程时依赖 task_acks_late + task_reject_on_worker_lost 重新入队。
# 环境变量名沿用既有 SATELLITE_BATCH_*（非 INGEST_ 前缀）；也接受 INGEST_ 别名。
def _batch_limit_env(primary: str, ingest_alias: str, default: int) -> int:
    if os.environ.get(primary) is not None:
        return _positive_limit_env(primary, default)
    if os.environ.get(ingest_alias) is not None:
        return _positive_limit_env(ingest_alias, default)
    return _positive_limit_env(primary, default)


SATELLITE_BATCH_TIME_LIMIT_SEC = _batch_limit_env(
    "SATELLITE_BATCH_TIME_LIMIT_SEC",
    "INGEST_SATELLITE_BATCH_TIME_LIMIT_SEC",
    780,
)
SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC = min(
    SATELLITE_BATCH_TIME_LIMIT_SEC - 1,
    _batch_limit_env(
        "SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC",
        "INGEST_SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC",
        720,
    ),
)
SATELLITE_BATCH_SOFT_TIMEOUT_MAX_RETRIES = _nonneg_int_env(
    "SATELLITE_BATCH_SOFT_TIMEOUT_MAX_RETRIES", 6
)
SATELLITE_BATCH_SOFT_TIMEOUT_RETRY_COUNTDOWN_SEC = _nonneg_int_env(
    "SATELLITE_BATCH_SOFT_TIMEOUT_RETRY_COUNTDOWN_SEC", 30
)


def _compensation_progress(progress: dict, attempt: int, *, active: bool) -> dict:
    """记录共享遥感任务的补偿次数，便于旧任务回收和运维审计。"""
    output = dict(progress or {})
    output.update(
        {
            "compensation_attempt": attempt,
            "compensation_count": attempt,
            "compensation_max": SATELLITE_BATCH_MAX_COMPENSATIONS,
            "total_attempt": attempt + 1,
            "compensation_active_attempt": attempt if active else None,
        }
    )
    return output


def _active_compensation_attempt(progress: dict) -> int | None:
    try:
        value = progress.get("compensation_active_attempt")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_assessment_batch_child(job: dict) -> bool:
    """只对 assessment_batch 的共享下载子任务开启批次补偿。"""
    params = job.get("params_json") or {}
    return bool(job.get("parent_job_id") or params.get("assessment_batch_id"))


def _schedule_satellite_compensation(
    job_id: str,
    job: dict,
    *,
    compensation_attempt: int,
    error: str,
) -> bool:
    """失败的批次下载复用原 Job 重试，最多额外执行两次。"""
    if not _is_assessment_batch_child(job):
        return False

    current_progress = dict(job.get("progress_json") or {})
    try:
        completed_compensations = int(current_progress.get("compensation_count") or 0)
    except (TypeError, ValueError):
        completed_compensations = 0
    active_attempt = _active_compensation_attempt(current_progress)
    # 旧 worker 迟到上报时，已经存在更高序号的补偿任务，不能再次派发。
    if active_attempt is not None and active_attempt > compensation_attempt:
        return True

    next_attempt = max(completed_compensations, compensation_attempt) + 1
    if next_attempt > SATELLITE_BATCH_MAX_COMPENSATIONS:
        final_progress = _compensation_progress(
            current_progress, completed_compensations, active=False
        )
        final_progress.update({"stage": "failed", "last_error": str(error)[:2000]})
        patch_job(
            job_id,
            {
                "status": "failed",
                "touch_finished": True,
                "progress_json": final_progress,
                "error": str(error)[:2000],
            },
        )
        return False

    retry_progress = _compensation_progress(
        current_progress, next_attempt, active=True
    )
    retry_progress.update(
        {
            "stage": "compensating",
            "percent": min(95, max(5, int(current_progress.get("percent") or 5))),
            "last_error": str(error)[:2000],
        }
    )
    try:
        # 先落库“补偿中”再投递，避免失败回调重复创建并行补偿任务。
        patch_job(
            job_id,
            {
                "status": "running",
                "progress_json": retry_progress,
                "error": f"第{next_attempt}次补偿中：{str(error)[:1800]}",
            },
        )
        try:
            task_id = str(
                uuid.uuid5(uuid.UUID(str(job_id)), f"satellite-compensation:{next_attempt}")
            )
        except (ValueError, TypeError):
            task_id = str(uuid.uuid4())
        retry_kwargs = {
            "job_id": job_id,
            "compensation_attempt": next_attempt,
        }
        original_mq_task_id = (job.get("params_json") or {}).get("mq_task_id")
        if original_mq_task_id:
            retry_kwargs["mq_task_id"] = str(original_mq_task_id)
        process_satellite_batch.apply_async(
            kwargs=retry_kwargs,
            countdown=SATELLITE_COMPENSATION_DELAY_SECONDS,
            task_id=task_id,
        )
    except Exception as exc:
        logger.exception(
            "satellite_compensation_dispatch_failed",
            job_id=job_id,
            attempt=next_attempt,
            error=str(exc),
        )
        failure_progress = _compensation_progress(
            retry_progress, next_attempt, active=False
        )
        failure_progress.update({"stage": "failed", "last_error": str(exc)[:2000]})
        try:
            patch_job(
                job_id,
                {
                    "status": "failed",
                    "touch_finished": True,
                    "progress_json": failure_progress,
                    "error": f"补偿任务派发失败：{str(exc)[:1800]}",
                },
            )
        except Exception:
            logger.exception(
                "satellite_compensation_failure_state_update_failed", job_id=job_id
            )
        return False

    logger.warning(
        "satellite_compensation_scheduled",
        job_id=job_id,
        attempt=next_attempt,
        max_compensations=SATELLITE_BATCH_MAX_COMPENSATIONS,
    )
    return True


def crop_shared_array(
    source,
    source_transform,
    target_shape,
    target_transform,
    *,
    source_crs="EPSG:4326",
    target_crs="EPSG:4326",
    categorical=False,
):
    """从共享数组重采样到原有地块网格；新数组防止地块掩膜污染邻居的数据。"""
    destination = np.full(target_shape, np.nan, dtype=np.float32)
    reproject(
        source=source,
        destination=destination,
        src_transform=source_transform,
        src_crs=source_crs,
        dst_transform=target_transform,
        dst_crs=target_crs,
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling.nearest if categorical else Resampling.bilinear,
    )
    return destination


def _load_lands(
    land_ids,
    sensor,
    force,
    season_months=None,
    growing_seasons=None,
    date_from: date | None = None,
    date_to: date | None = None,
):
    """分批通过 Internal HTTP 读取地块和已处理日期，避免逐地块请求放大网络开销。"""
    land_ids = list(dict.fromkeys(str(item).strip() for item in land_ids))
    if not land_ids or any(not land_id for land_id in land_ids):
        raise RuntimeError("遥感批次地块清单为空或包含空编号")

    lands = []
    # 每批最多50个边界并复用一个HTTP连接；已有日期仅需覆盖当前卫星搜索窗口。
    with internal_client(timeout=60.0) as client:
        for start in range(0, len(land_ids), SATELLITE_BATCH_INPUTS_LAND_LIMIT):
            batch_ids = land_ids[start : start + SATELLITE_BATCH_INPUTS_LAND_LIMIT]
            remote_items = agri_satellite_batch_inputs(
                land_ids=batch_ids,
                sensor=sensor,
                include_existing_dates=not force,
                date_from=date_from.isoformat() if date_from is not None else None,
                date_to=date_to.isoformat() if date_to is not None else None,
                client=client,
            )
            remote_by_id = {
                str(item.get("land_id")): item
                for item in remote_items
                if item.get("land_id") is not None
            }
            if set(remote_by_id) != set(batch_ids):
                raise RuntimeError("遥感批量内部HTTP返回的地块清单不完整")

            for land_id in batch_ids:
                remote = remote_by_id[land_id]
                # 入队后规则可能变化；执行前再过滤，避免已排除地块访问卫星数据。
                if not is_scheduled_land_allowed(
                    remote.get("base_id"), remote.get("land_area_mu")
                ):
                    logger.info(
                        "satellite_batch_land_filtered",
                        land_id=land_id,
                        base_id=remote.get("base_id"),
                        land_area_mu=remote.get("land_area_mu"),
                    )
                    continue
                if not remote.get("boundary_geojson") or not remote.get("tile_id"):
                    raise RuntimeError(f"地块{land_id}的内部HTTP元数据不完整")
                geom = shape(remote["boundary_geojson"])
                if (
                    geom.is_empty
                    or not geom.is_valid
                    or geom.geom_type not in {"Polygon", "MultiPolygon"}
                ):
                    raise RuntimeError(f"地块{land_id}的边界无效")
                existing = (
                    {
                        date.fromisoformat(str(value)[:10])
                        for value in remote.get("existing_dates", [])
                    }
                    if not force
                    else set()
                )
                land_crs = analysis_crs_for_bounds(geom.bounds)
                lands.append(
                    {
                        "meta": {
                            "land_id": land_id,
                            "tile_id": remote["tile_id"],
                            "land_name": remote.get("land_name") or land_id,
                        },
                        "geom": geom,
                        "grid": compute_target_grid(
                            geom.bounds, geom, target_crs=land_crs
                        ),
                        "grid_crs": land_crs,
                        "existing": existing,
                        # 显式回填轮作月份优先于作物默认季节，避免区域下载误过滤用户指定窗口。
                        "season_months": normalize_season_months(
                            season_months=season_months,
                            growing_seasons=growing_seasons,
                            crop_type=remote.get("crop_type"),
                        ),
                        "crop_type": remote.get("crop_type"),
                        "raw_results": [],
                        # 去云快照一旦由父线程封口，迟到的原始景必须单独补排。
                        "decloud_finalized": False,
                    }
                )
    return lands


def select_complete_processing_lands(
    lands,
    anchor_id: str,
    processing_window_km: float,
    *,
    oversized: bool = False,
    download_bbox=None,
    processing_boundary=None,
):
    """Return the shared processing geometry and only fully covered parcels.

    动态窗口按锚点地块与空间分组预先规划；这里只选择被窗口完整包含的地块，
    防止任务排队后边界变化或邻景幅边界使指标只覆盖地块的一部分。
    """
    if not lands:
        raise ValueError("satellite batch has no land parcels")
    anchor = next(
        (land for land in lands if str(land["meta"]["land_id"]) == str(anchor_id)),
        lands[0],
    )
    if oversized:
        # 超过窗口的地块不能被窗口截断，独立使用完整外接矩形。
        window_bounds = tuple(download_bbox or anchor["geom"].bounds)
        return [anchor], box(*window_bounds)

    if processing_boundary is not None:
        # API 将本轮动态规划的窗口边界放进 Job；下载机必须复用它以维持相同分组。
        processing_geom = (
            shape(processing_boundary)
            if isinstance(processing_boundary, dict)
            else processing_boundary
        )
        if processing_geom.is_empty or not processing_geom.is_valid:
            raise ValueError("任务中的动态处理窗口边界无效")
        if not processing_geom.covers(anchor["geom"]):
            raise ValueError("动态处理窗口未完整包含锚点地块")
        return [land for land in lands if processing_geom.covers(land["geom"])], processing_geom

    # 旧任务缺少动态边界时，使用任务携带的窗口宽度临时构造兼容处理范围。
    processing_geom, anchor_oversized = build_complete_processing_window(
        anchor["geom"], processing_window_km
    )
    if anchor_oversized:
        # 队列等待期间锚点边界可能变大；重新按完整地块独立处理，不能截断锚点。
        return [anchor], box(*anchor["geom"].bounds)
    selected = [
        land for land in lands if processing_geom.covers(land["geom"])
    ]
    return selected, processing_geom


def _scene_lands(scene, lands, sensor):
    footprint = shape(scene["geometry"]) if scene.get("geometry") else None
    selected = []
    for land in lands:
        if scene["date"] in land["existing"]:
            continue
        # 同一组可能横跨卫星瓦片边缘，只让完整覆盖地块的景生成结果。
        if footprint is not None and not footprint.covers(land["geom"]):
            continue
        if sensor == "S2":
            filtered, _ = filter_scenes_outside_season_high_cloud(
                [scene], season_months=land["season_months"]
            )
            if not filtered:
                continue
        selected.append(land)
    return selected


def _search_s2_options() -> dict:
    extra_assets = {"SCL": SCL_STAC_ASSETS}
    cloud_max = None
    if decloud_enabled():
        extra_assets.update(decloud_s2_extra_assets())
        cloud_max = decloud_stac_cloud_max_pct()
    return {
        "index_defs": agri_optical_index_defs(),
        "index_label": "satellite_batch",
        "max_cloud_cover": cloud_max,
        "extra_assets": extra_assets,
        "cloud_dedupe": "none",
        "max_items": 2000,
    }


def _search_s2_scenes(processing_geom, date_from: date, date_to: date) -> list[dict]:
    """S2 先搜 Element84/AWS；搜索失败或无结果时才用 PC 签名 STAC 降级。"""
    options = _search_s2_options()
    primary_error: Exception | None = None
    try:
        scenes = search_scenes_for_defs(
            mapping(processing_geom), date_from, date_to, **options
        )
    except Exception as exc:
        primary_error = exc
        scenes = []
        logger.warning(
            "s2_element84_stac_search_failed",
            date_from=str(date_from),
            date_to=str(date_to),
            error=str(exc),
        )
    if scenes:
        return scenes

    try:
        fallback_scenes = search_scenes_for_defs(
            mapping(processing_geom),
            date_from,
            date_to,
            **options,
            catalog_url=S2_PC_STAC_API_URL,
            planetary_computer_signing=True,
            source_catalog="planetary_computer",
        )
    except Exception as fallback_error:
        logger.exception(
            "s2_planetary_computer_stac_search_failed",
            date_from=str(date_from),
            date_to=str(date_to),
            error=str(fallback_error),
        )
        if primary_error is not None:
            raise RuntimeError(
                f"Element84 搜索失败，PC 降级搜索也失败：{fallback_error}"
            ) from primary_error
        raise
    if fallback_scenes:
        logger.warning(
            "s2_planetary_computer_stac_fallback_used",
            scene_count=len(fallback_scenes),
            date_from=str(date_from),
            date_to=str(date_to),
        )
    return fallback_scenes


def _search_s2_scene_fallback(scene, processing_geom) -> list[dict]:
    """AWS COG 读取失败时，按同一日期和窗口找 PC 的替代 Sentinel-2 景。"""
    scene_date = scene.get("date")
    if not isinstance(scene_date, date):
        scene_date = date.fromisoformat(str(scene_date)[:10])
    options = _search_s2_options()
    return search_scenes_for_defs(
        mapping(processing_geom),
        scene_date,
        scene_date,
        **options,
        catalog_url=S2_PC_STAC_API_URL,
        planetary_computer_signing=True,
        source_catalog="planetary_computer",
    )


def _download_scene(
    scene, sensor, grid, *, job_id: str | None = None, scene_workers: int = 1
):
    """下载一个共享景窗口；全部波段走同一并发池和统一重试/日志链路。

    ``scene_workers`` 是父级景线程池大小，交给 band 层做嵌套扇出限流，
    避免 SCENE×BAND 无界放大。
    """
    shared_transform, shared_shape, _, bounds = grid
    shared_crs = analysis_crs_for_bounds(bounds)
    scene_date = scene.get("date")
    log_context = {
        "job_id": job_id,
        "scene_id": scene.get("id"),
        "date": scene_date.isoformat()
        if hasattr(scene_date, "isoformat")
        else str(scene_date)[:10],
        "sensor": sensor,
    }
    if sensor == "S1":
        # VV/VH 必须使用同一套定标口径；有官方 LUT 时在读取阶段直接转为 Sigma0。
        _s1_radiometric_calibration(scene)
        hrefs = {"vv": scene["vv_href"], "vh": scene["vh_href"]}
        calibration_hrefs = {
            band: scene.get(f"{band}_calibration_href")
            for band in hrefs
            if scene.get(f"{band}_calibration_href")
        }
        resampling_by_band = {}
    else:
        hrefs = dict(scene["band_hrefs"])
        # RGB预览复用光谱波段，不把 visual 三通道资产混入指数波段缓存。
        hrefs.pop("visual", None)
        calibration_hrefs = None
        resampling_by_band = {"SCL": Resampling.nearest}

    cached = read_scene_window(
        scene_id=str(scene.get("id") or ""),
        sensor=sensor,
        target_shape=shared_shape,
        target_transform=shared_transform,
        target_crs=shared_crs,
        band_hrefs=hrefs,
        calibration_hrefs=calibration_hrefs,
        resampling_by_band=resampling_by_band,
    )
    if cached is not None:
        logger.info("satellite_window_cache_hit", **log_context, bands=len(cached))
        scl = None if sensor == "S1" else cached.pop("SCL", None)
        return cached, scl
    if window_cache_enabled():
        logger.info("satellite_window_cache_miss", **log_context)

    if sensor == "S1":
        bands = run_parallel_band_jobs(
            hrefs,
            lambda band, href: _read_band_windowed_db_profiled(
                href,
                bounds,
                shared_shape,
                shared_transform,
                shared_crs,
                calibration_href=scene.get(f"{band}_calibration_href"),
            ),
            scene_workers=scene_workers,
            log_context=log_context,
        )
        try:
            write_scene_window(
                scene_id=str(scene.get("id") or ""),
                sensor=sensor,
                target_shape=shared_shape,
                target_transform=shared_transform,
                target_crs=shared_crs,
                band_hrefs=hrefs,
                calibration_hrefs=calibration_hrefs,
                arrays=bands,
                resampling_by_band=resampling_by_band,
            )
        except Exception as exc:
            # 磁盘缓存只是加速手段，写入失败不能丢弃已成功读取的卫星波段。
            logger.warning(
                "satellite_window_cache_write_failed",
                **log_context,
                error=str(exc),
            )
        return bands, None
    # SCL 与光谱波段一起进入线程池，避免所有光谱完成后再串行发起一次远程读取。
    downloaded = read_bands_windowed_parallel(
        hrefs,
        bounds,
        shared_shape,
        shared_transform,
        target_crs=shared_crs,
        scene_workers=scene_workers,
        resampling_by_band=resampling_by_band,
        log_context=log_context,
    )
    scl = downloaded.pop("SCL", None)
    # Sentinel-2零值为景外/无数据，先转NaN，避免EVI等公式把填充值算成有效像元。
    for band in downloaded.values():
        band[band == 0] = np.nan
    cache_arrays = dict(downloaded)
    if scl is not None:
        cache_arrays["SCL"] = scl
    try:
        write_scene_window(
            scene_id=str(scene.get("id") or ""),
            sensor=sensor,
            target_shape=shared_shape,
            target_transform=shared_transform,
            target_crs=shared_crs,
            band_hrefs=hrefs,
            arrays=cache_arrays,
            resampling_by_band=resampling_by_band,
        )
    except Exception as exc:
        logger.warning(
            "satellite_window_cache_write_failed",
            **log_context,
            error=str(exc),
        )
    return downloaded, scl


def _publish_land(
    scene,
    sensor,
    land,
    shared_bands,
    shared_scl,
    shared_grid,
    parent_id,
    processing_window_km: float | None = None,
    raw_result_out: dict | None = None,
) -> bool:
    target_transform, target_shape, field_mask, _ = land["grid"]
    target_crs = land["grid_crs"]
    shared_crs = analysis_crs_for_bounds(shared_grid[3])
    bands = {
        key: crop_shared_array(
            value,
            shared_grid[0],
            target_shape,
            target_transform,
            source_crs=shared_crs,
            target_crs=target_crs,
        )
        for key, value in shared_bands.items()
    }
    meta = land["meta"]
    geom_json = mapping(land["geom"])
    # 同组多个地块同一天的结果必须具有不同结果编号，否则缓存/入库端会去重丢数据。
    land_task_id = f"{parent_id}:{meta['land_id']}"
    if sensor == "S1":
        for band in bands.values():
            band[~field_mask] = np.nan
        pixels = _sample_s1_lonlat(
            geom_json, bands["vv"], bands["vh"], target_transform, target_crs
        )
        if not pixels:
            return False
        _upsert_agri_s1(
            None,
            meta,
            scene["date"],
            f"{scene['id']}_stac",
            meta["land_id"],
            pixels,
            compute_zonal_stats(bands["vv"], expected_mask=field_mask),
            compute_zonal_stats(bands["vh"], expected_mask=field_mask),
            mq_task_id=land_task_id,
            relative_orbit=scene.get("relative_orbit"),
            stac_item_id=str(scene.get("id") or "") or None,
            analysis_grid=describe_target_grid(target_transform, target_shape, target_crs),
            processing_window_km=processing_window_km,
            processing_window_bounds=shared_grid[3],
            radiometric_calibration=_s1_radiometric_calibration(scene),
            # 日批结果走 API HTTP -> Redis 缓存 -> API 入库，不让下载机直写 PG 或发结果 MQ。
            result_delivery="http",
        )
        return True

    scl = (
        crop_shared_array(
            shared_scl,
            shared_grid[0],
            target_shape,
            target_transform,
            source_crs=shared_crs,
            target_crs=target_crs,
            categorical=True,
        )
        if shared_scl is not None
        else None
    )
    indices = {}
    for definition in agri_optical_index_defs():
        # 共享窗口仍缓存原始DN，只有指数入口按STAC定标恢复到物理反射率。
        array = definition.formula(
            {key: bands[key] for key in definition.bands},
            band_radiometry=scene.get("band_radiometry"),
        )
        array[~np.isfinite(array) | ~field_mask] = np.nan
        indices[INDEX_KEY_TO_PIXEL[definition.key]] = array
    if decloud_enabled():
        from app.tasks.decloud_uncrtaints import cache_optical_s2_window

        try:
            cache_optical_s2_window(
                land_id=meta["land_id"],
                date_str=scene["date"].isoformat(),
                bands=bands,
                band_hrefs=scene["band_hrefs"],
                cloud_cover=scene.get("cloud_cover"),
                stac_id=scene["id"],
                target_shape=target_shape,
                target_transform=target_transform,
                target_crs=target_crs,
                field_mask=field_mask,
                band_radiometry=scene.get("band_radiometry"),
                band_radiometry_source=scene.get("band_radiometry_source"),
                band_radiometry_sources=scene.get("band_radiometry_sources"),
            )
        except Exception as exc:
            # 去云缓存与旧光学流程一样尽力保存，缓存失败不能阻断原始地块结果。
            logger.warning(
                "satellite_batch_decloud_cache_failed",
                land_id=meta["land_id"],
                error=str(exc),
            )
    shared_geom = transform_geom("EPSG:4326", shared_crs, geom_json)
    shared_mask = geometry_mask(
        [shared_geom], out_shape=shared_grid[1], transform=shared_grid[0], invert=True
    )
    rgb = upload_field_rgb_preview(
        land_id=meta["land_id"],
        date_str=scene["date"].isoformat(),
        bands=bands,
        field_mask=field_mask,
        band_radiometry=scene.get("band_radiometry"),
        scene_bands=shared_bands,
        scene_field_mask=shared_mask,
    )
    parcel_cloud = parcel_cloud_from_scl_window(scl, field_mask)
    result = emit_optical_lonlat(
        meta=meta,
        geom4326=geom_json,
        land_id_str=meta["land_id"],
        scene=scene,
        index_arrays=indices,
        transform=target_transform,
        target_crs=target_crs,
        parcel_cloud=parcel_cloud,
        parcel_cloud_source=PARCEL_CLOUD_SOURCE_SCL
        if parcel_cloud is not None
        else None,
        scl=scl,
        mq_task_id=land_task_id,
        rgb_url=rgb.get("rgb_url"),
        large_rgb_url=rgb.get("large_rgb_url"),
        rgb_oss_key=rgb.get("rgb_oss_key"),
        processing_window_km=processing_window_km,
        processing_window_bounds=shared_grid[3],
        # 日批结果走 API HTTP -> Redis 缓存 -> API 入库，不让下载机直写 PG 或发结果 MQ。
        result_delivery="http",
    )
    if isinstance(result, dict) and raw_result_out is not None:
        # 用每次调用私有的容器返回去云规划所需信息，避免并发地块覆盖共享字段。
        raw_result_out["result"] = result
    return result is not None



def _release_scene_date_reservation(lands, scene_date) -> None:
    """下载/发布失败时释放乐观占位，让后续同日景或补偿可再试。"""
    for land in lands:
        land["existing"].discard(scene_date)


def _schedule_late_decloud_result(
    *,
    land: dict,
    raw_result: dict,
    job_id: str,
    mq_task_id: str | None,
    date_from: str,
    date_to: str,
    season_months: tuple[int, ...] | list[int] | None,
    crop_type: str | None,
) -> None:
    """父线程已封口后，为刚发布成功的单景补排去云任务。"""
    if not raw_result or not raw_result.get("date"):
        return
    from app.tasks.decloud_uncrtaints import schedule_decloud_after_raw

    land_id = str(land["meta"]["land_id"])
    date_str = str(raw_result["date"])[:10]
    scene_id = str(raw_result.get("scene_id") or raw_result.get("stac_id") or "")
    # 原批次快照已经排过；迟到景单独规划，避免再次提交快照中已有的所有日期。
    late_task_id = (
        f"{mq_task_id or job_id}:{land_id}:late:{date_str}:{scene_id or 'unknown'}"
    )
    try:
        schedule_decloud_after_raw(
            land_id=land_id,
            date_from=date_from,
            date_to=date_to,
            raw_results=[raw_result],
            mq_task_id=late_task_id,
            season_months=season_months,
            crop_type=crop_type,
        )
    except Exception:
        # 原始产品已经成功落地；单独记录派发失败，不能把它误记成影像发布失败。
        logger.exception(
            "satellite_batch_late_decloud_schedule_failed",
            job_id=job_id,
            land_id=land_id,
            date=date_str,
            scene_id=scene_id,
        )


def _process_one_batch_scene(
    scene,
    *,
    sensor: str,
    selected_lands: list,
    grid,
    processing_geom,
    job_id: str,
    mq_task_id: str | None,
    date_from: str,
    date_to: str,
    processing_window_km: float | None,
    scene_workers: int,
    state_lock: threading.Lock,
    progress: dict,
    progress_revision: dict[str, int],
    publish_semaphore: threading.BoundedSemaphore,
    patch_progress: Callable[[], None],
    failed_land_ids: set[str],
    failed_scene_ids: set[str],
    completed_scene_keys: set[str],
    abandon_event: threading.Event,
    active_reservations: dict,
) -> None:
    """处理单景；状态锁只保护共享内存，栅格裁剪、OSS和HTTP均在锁外执行。

    abandon_event表示父任务已放弃等待；正在进行的外部发布无法强杀，
    因此迟到的成功回执仍需记录，并在父线程封口后单独补排去云。
    """

    def record_failures(lands_to_record, scene_id: str | None) -> None:
        failed_land_ids.update(str(land["meta"]["land_id"]) for land in lands_to_record)
        if scene_id:
            failed_scene_ids.add(str(scene_id))

    def reservation_key() -> str:
        return str(scene.get("id") or id(scene))

    def clear_active_reservation() -> None:
        active_reservations.pop(reservation_key(), None)

    def flush_abandoned_progress() -> None:
        """已超时景若有成功发布，及时保存场景回执供总览核验。"""
        try:
            patch_progress()
        except Exception:
            logger.exception(
                "satellite_batch_abandoned_progress_patch_failed",
                job_id=job_id,
                scene_id=scene.get("id"),
            )

    scene_date = scene["date"]

    with state_lock:
        if abandon_event.is_set():
            return
        selected = _scene_lands(scene, selected_lands, sensor)
        # 同日多景并发时先占位，避免重复下载；成功保留，失败再释放。
        for land in selected:
            land["existing"].add(scene_date)
        reserved = list(selected)
        if reserved:
            # 同步保留本景已成功发布地块，超时补偿只回收仍未完成的占位。
            active_reservations[reservation_key()] = (
                scene_date,
                list(reserved),
                set(),
            )

    if not reserved:
        with state_lock:
            if abandon_event.is_set():
                return
            progress["failed_land_ids"] = sorted(failed_land_ids)
            progress["failed_scene_ids"] = sorted(failed_scene_ids)
            progress["scenes_done"] += 1
            completed_scene_keys.add(reservation_key())
            progress_revision["value"] += 1
        patch_progress()
        return

    scene_products: list[tuple[dict, list, dict, np.ndarray | None]] = []
    primary_error: Exception | None = None
    try:
        shared_bands, scl = _download_scene(
            scene, sensor, grid, job_id=job_id, scene_workers=scene_workers
        )
        scene_products.append((scene, reserved, shared_bands, scl))
        unresolved: dict[str, dict] = {}
    except Exception as exc:
        primary_error = exc
        unresolved = {str(land["meta"]["land_id"]): land for land in reserved}
        if sensor == "S2" and scene.get("source_catalog") != "planetary_computer":
            try:
                fallback_scenes = _search_s2_scene_fallback(scene, processing_geom)
            except Exception as fallback_error:
                fallback_scenes = []
                logger.warning(
                    "s2_planetary_computer_cog_fallback_search_failed",
                    job_id=job_id,
                    scene_id=scene.get("id"),
                    error=str(fallback_error),
                )
            def _fallback_cover_count(candidate: dict) -> int:
                footprint = (
                    shape(candidate["geometry"]) if candidate.get("geometry") else None
                )
                n = 0
                for land in unresolved.values():
                    if footprint is not None and not footprint.covers(land["geom"]):
                        continue
                    filtered, _ = filter_scenes_outside_season_high_cloud(
                        [candidate], season_months=land["season_months"]
                    )
                    if filtered:
                        n += 1
                return n

            fallback_scenes.sort(
                key=lambda candidate: (
                    -_fallback_cover_count(candidate),
                    float(candidate.get("cloud_cover") or 100),
                    str(candidate.get("id") or ""),
                )
            )
            for fallback_scene in fallback_scenes:
                # 占位已在 land["existing"]；fallback 覆盖判定用 unresolved 列表。
                fallback_selected = []
                footprint = (
                    shape(fallback_scene["geometry"])
                    if fallback_scene.get("geometry")
                    else None
                )
                for land in list(unresolved.values()):
                    if footprint is not None and not footprint.covers(land["geom"]):
                        continue
                    filtered, _ = filter_scenes_outside_season_high_cloud(
                        [fallback_scene], season_months=land["season_months"]
                    )
                    if not filtered:
                        continue
                    fallback_selected.append(land)
                if not fallback_selected:
                    continue
                try:
                    fallback_bands, fallback_scl = _download_scene(
                        fallback_scene,
                        sensor,
                        grid,
                        job_id=job_id,
                        scene_workers=scene_workers,
                    )
                except Exception as fallback_error:
                    logger.warning(
                        "s2_planetary_computer_cog_fallback_failed",
                        job_id=job_id,
                        primary_scene_id=scene.get("id"),
                        fallback_scene_id=fallback_scene.get("id"),
                        error=str(fallback_error),
                    )
                    continue
                scene_products.append(
                    (fallback_scene, fallback_selected, fallback_bands, fallback_scl)
                )
                for land in fallback_selected:
                    unresolved.pop(str(land["meta"]["land_id"]), None)

    published_land_ids: set[str] = set()
    for product_scene, product_lands, shared_bands, scl in scene_products:
        for land in product_lands:
            if abandon_event.is_set():
                # 父线程已接管剩余地块；先持久化本景成功增量，再补偿未完成部分。
                flush_abandoned_progress()
                return
            land_id = str(land["meta"]["land_id"])
            try:
                raw_result_out: dict = {}
                with publish_semaphore:
                    if abandon_event.is_set():
                        flush_abandoned_progress()
                        return
                    # 限制同时裁栅格/生成预览的地块数，避免移出状态锁后放大峰值内存。
                    published = _publish_land(
                        product_scene,
                        sensor,
                        land,
                        shared_bands,
                        scl,
                        grid,
                        mq_task_id or job_id,
                        processing_window_km,
                        raw_result_out=raw_result_out,
                    )
                raw_result = raw_result_out.get("result")
                schedule_late_decloud = False
                with state_lock:
                    if published:
                        progress["products_published"] += 1
                        receipt = {
                            "land_id": land["meta"]["land_id"],
                            "date": product_scene["date"].isoformat(),
                        }
                        receipt_scene_id = str(product_scene.get("id") or "").strip()
                        if receipt_scene_id:
                            # 没有来源景号的兼容输入只做日期级核对，不能伪造空景号。
                            receipt["scene_id"] = receipt_scene_id
                        progress["published_products"].append(receipt)
                        published_land_ids.add(land_id)
                        reservation = active_reservations.get(reservation_key())
                        if reservation is not None:
                            reservation[2].add(land_id)
                        if sensor == "S2" and raw_result is not None:
                            land["raw_results"].append(raw_result)
                            schedule_late_decloud = bool(
                                land.get("decloud_finalized")
                            )
                        progress_revision["value"] += 1
                    elif not abandon_event.is_set():
                        progress["failed"] += 1
                        record_failures([land], product_scene.get("id"))
                        land["existing"].discard(scene_date)
                        progress_revision["value"] += 1
                if schedule_late_decloud and raw_result is not None:
                    _schedule_late_decloud_result(
                        land=land,
                        raw_result=raw_result,
                        job_id=job_id,
                        mq_task_id=mq_task_id,
                        date_from=date_from,
                        date_to=date_to,
                        season_months=land.get("season_months"),
                        crop_type=land.get("crop_type"),
                    )
            except Exception as exc:
                with state_lock:
                    if not abandon_event.is_set():
                        progress["failed"] += 1
                        record_failures([land], product_scene.get("id"))
                        land["existing"].discard(scene_date)
                        progress_revision["value"] += 1
                logger.error(
                    "satellite_batch_land_failed",
                    job_id=job_id,
                    land_id=land["meta"]["land_id"],
                    error=str(exc),
                )

    with state_lock:
        # 超时父线程若已弹出预留，就已负责失败统计；迟到worker只补记真实成功结果。
        parent_finalized_scene = (
            abandon_event.is_set()
            and reservation_key() not in active_reservations
            and reservation_key() not in completed_scene_keys
        )
        if parent_finalized_scene:
            should_patch = bool(published_land_ids)
        else:
            if primary_error is not None and unresolved:
                failed_lands = list(unresolved.values())
                progress["failed"] += len(failed_lands)
                record_failures(failed_lands, str(scene.get("id") or ""))
                _release_scene_date_reservation(failed_lands, scene_date)
                logger.error(
                    "satellite_batch_download_failed",
                    job_id=job_id,
                    scene_id=scene.get("id"),
                    error=str(primary_error),
                    unresolved_land_ids=sorted(unresolved),
                )
            elif primary_error is None and not scene_products:
                _release_scene_date_reservation(reserved, scene_date)

            # 主下载成功但部分reserved地块未进入任何product：释放占位供同日其他景重试。
            if primary_error is None and scene_products:
                covered = {
                    str(land["meta"]["land_id"])
                    for _, lands, _, _ in scene_products
                    for land in lands
                }
                leftover = [
                    land
                    for land in reserved
                    if str(land["meta"]["land_id"]) not in covered
                    and str(land["meta"]["land_id"]) not in published_land_ids
                ]
                if leftover:
                    _release_scene_date_reservation(leftover, scene_date)

            progress["failed_land_ids"] = sorted(failed_land_ids)
            progress["failed_scene_ids"] = sorted(failed_scene_ids)
            progress["scenes_done"] += 1
            completed_scene_keys.add(reservation_key())
            clear_active_reservation()
            progress_revision["value"] += 1
            should_patch = True

    if should_patch:
        if abandon_event.is_set():
            flush_abandoned_progress()
        else:
            patch_progress()


@celery_app.task(
    bind=True,
    name="app.tasks.satellite_batch.process_satellite_batch",
    time_limit=SATELLITE_BATCH_TIME_LIMIT_SEC,
    soft_time_limit=SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC,
    max_retries=SATELLITE_BATCH_SOFT_TIMEOUT_MAX_RETRIES,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_satellite_batch(
    self,
    job_id: str,
    mq_task_id: str | None = None,
    compensation_attempt: int = 0,
) -> dict:
    """一个组、传感器和时间分片共用下载，逐地块掩膜并报告任务进度。"""
    job: dict | None = None
    try:
        job = get_job(job_id)
        active_attempt = _active_compensation_attempt(job.get("progress_json") or {})
        if (
            _is_assessment_batch_child(job)
            and active_attempt is not None
            and active_attempt > compensation_attempt
        ):
            # 旧任务可能因 worker 租约回收迟到执行；补偿已入队时直接结束旧执行，
            # 防止旧下载覆盖补偿任务的进度或再次消耗补偿次数。
            return {
                "job_id": job_id,
                "status": "compensating",
                "compensation_attempt": active_attempt,
            }
        if job.get("status") == "completed":
            return {"job_id": job_id, "status": "already_handled"}
        if job.get("status") in {"failed", "cancelled"} and (
            job.get("progress_json") or {}
        ).get("stale_recovered"):
            # API汇总已将丢失的worker任务回收为失败，迟到的旧worker不再重复消耗下载资源。
            return {"job_id": job_id, "status": "stale_recovered"}
        params = job["params_json"]
        sensor = params["sensor"]
        if sensor not in {"S1", "S2"}:
            raise ValueError(f"不支持的遥感传感器: {sensor}")
        d0, d1 = (
            date.fromisoformat(params["date_from"]),
            date.fromisoformat(params["date_to"]),
        )
        patch_job(job_id, {"status": "running", "touch_started": True})
        lands = _load_lands(
            params["land_ids"],
            sensor,
            params.get("force", False),
            season_months=params.get("season_months"),
            growing_seasons=params.get("growing_seasons"),
            date_from=d0,
            date_to=d1,
        )
        if not lands:
            patch_job(
                job_id,
                {
                    "status": "cancelled",
                    "error": "定时任务地块过滤：基地被排除或地块面积超过5000亩",
                    "progress_json": {
                        "current_step": "filtered",
                        "message": "所有地块均不参与自动化任务",
                    },
                },
            )
            return {"job_id": job_id, "status": "cancelled", "reason": "land_filtered"}
        processing_window_km = resolve_processing_window_km(
            params.get("processing_window_km")
        )
        anchor_id = str(params.get("anchor_land_id") or job.get("land_id"))
        selected_lands, processing_geom = select_complete_processing_lands(
            lands,
            anchor_id,
            processing_window_km,
            oversized=bool(params.get("oversized")),
            download_bbox=params.get("download_bbox"),
            processing_boundary=params.get("processing_boundary_geojson"),
        )
        selected_ids = {
            str(land["meta"]["land_id"]) for land in selected_lands
        }
        excluded = [
            land["meta"]["land_id"]
            for land in lands
            if str(land["meta"]["land_id"]) not in selected_ids
        ]
        if excluded:
            logger.warning(
                "satellite_batch_land_excluded_partial_window",
                job_id=job_id,
                anchor_land_id=anchor_id,
                excluded_land_ids=excluded,
                processing_window_km=processing_window_km,
            )
        if not selected_lands:
            raise RuntimeError("processing window contains no complete land parcel")
        if sensor == "S2" and params.get("overview_run_id"):
            # 每日总览的 S1/S2 任务共用同一批地块和日期窗口，只在 S2 子任务
            # 派发一次天气，避免同一遥感批次产生两份重复天气任务。
            for land in selected_lands:
                celery_app.send_task(
                    "app.tasks.weather.backfill_weather_for_land",
                    args=[str(land["meta"]["land_id"])],
                    kwargs={
                        "date_from": d0.isoformat(),
                        "date_to": d1.isoformat(),
                    },
                )
        # 任务排队期间边界可能更新；按HTTP最新边界重新求范围，保证窗口与地块完整覆盖。
        union = unary_union([land["geom"] for land in selected_lands])
        shared_crs = analysis_crs_for_bounds(processing_geom.bounds)
        grid = compute_target_grid(
            processing_geom.bounds,
            union,
            padding_degrees=0.0,
            target_crs=shared_crs,
        )
        try:
            if sensor == "S1":
                scenes = search_s1_scenes(
                    mapping(processing_geom), d0, d1, dedupe_week=False
                )
            else:
                scenes = _search_s2_scenes(processing_geom, d0, d1)
        except Exception:
            # S1 沿用原有 Planetary Computer 搜索；S2 的 primary/fallback 已在 helper 中处理。
            raise
        # 失败明细用于批次结束后的定向补偿；保留“失败过”的候选集合，重复补偿是幂等的。
        failed_land_ids: set[str] = set()
        failed_scene_ids: set[str] = set()
        state_lock = threading.Lock()
        # API回执PATCH与状态锁分离：外部请求仍串行保序，但不能阻塞超时分支读取场景状态。
        progress_patch_lock = threading.Lock()

        progress = {
            "scenes_total": len(scenes),
            "scenes_done": 0,
            "products_published": 0,
            "failed": 0,
            "failed_land_ids": [],
            "failed_scene_ids": [],
            "published_products": [],
            "scene_workers": 1,
            "straggler_timeout_sec": scene_straggler_timeout_sec(),
            "straggler_abandoned_scene_ids": [],
        }
        progress_revision = {"value": 0}
        if compensation_attempt:
            progress = _compensation_progress(
                progress, compensation_attempt, active=True
            )

        published_products_persisted = 0
        published_progress_revision = 0

        def patch_progress() -> None:
            """快照共享进度后在独立锁内提交，避免锁住场景状态等待HTTP。"""
            nonlocal published_products_persisted, published_progress_revision
            with progress_patch_lock:
                with state_lock:
                    revision = progress_revision["value"]
                    if revision <= published_progress_revision:
                        return
                    products = progress.get("published_products") or []
                    product_count = len(products)
                    delta = products[published_products_persisted:product_count]
                    # 补偿轮次和阶段由父任务单独推进；迟到worker不得用旧快照回退它们。
                    patch = {
                        key: value
                        for key, value in progress.items()
                        if key
                        not in {
                            "published_products",
                            "compensation_attempt",
                            "compensation_count",
                            "compensation_max",
                            "total_attempt",
                            "compensation_active_attempt",
                            "stage",
                            "percent",
                            "last_error",
                        }
                    }
                    patch["published_products_delta"] = delta
                patch_job(
                    job_id,
                    {"progress_json": patch, "merge_progress": True},
                    include_progress=False,
                )
                # 只在服务端确认成功后推进快照游标；失败重试可重复提交幂等回执。
                published_products_persisted = product_count
                published_progress_revision = revision

        def progress_snapshot() -> dict:
            """复制进度列表，避免迟到worker与Celery结果序列化并发读写。"""
            with state_lock:
                return {
                    key: (
                        [
                            dict(item) if isinstance(item, dict) else item
                            for item in value
                        ]
                        if key == "published_products"
                        else list(value)
                        if isinstance(value, list)
                        else value
                    )
                    for key, value in progress.items()
                }

        workers = min(scene_max_workers(), max(1, len(scenes))) if scenes else 1
        progress["scene_workers"] = workers
        publish_workers = min(2, workers)
        progress["publish_workers"] = publish_workers
        publish_semaphore = threading.BoundedSemaphore(publish_workers)
        straggler_timeout = scene_straggler_timeout_sec()
        progress["straggler_timeout_sec"] = straggler_timeout
        abandon_event = threading.Event()
        active_reservations: dict = {}
        completed_scene_keys: set[str] = set()
        logger.info(
            "scene_parallel_start",
            job_id=job_id,
            index=f"satellite_batch_{sensor}",
            scenes=len(scenes),
            workers=workers,
            publish_workers=publish_workers,
            band_gdal_cap=band_max_workers(),
            straggler_timeout_sec=straggler_timeout,
        )
        patch_job(job_id, {"progress_json": dict(progress)})

        if scenes:
            # 不用 with：默认 shutdown(wait=True) 会在 straggler 放弃后仍永久卡住。
            # cancel_futures 只能取消尚未开跑的任务；已在跑的 GDAL/HTTP 线程只能孤儿化。
            pool = ThreadPoolExecutor(max_workers=workers)
            try:
                futures = {
                    pool.submit(
                        _process_one_batch_scene,
                        scene,
                        sensor=sensor,
                        selected_lands=selected_lands,
                        grid=grid,
                        processing_geom=processing_geom,
                        job_id=job_id,
                        mq_task_id=mq_task_id,
                        date_from=str(d0),
                        date_to=str(d1),
                        processing_window_km=processing_window_km,
                        scene_workers=workers,
                        state_lock=state_lock,
                        progress=progress,
                        progress_revision=progress_revision,
                        publish_semaphore=publish_semaphore,
                        patch_progress=patch_progress,
                        failed_land_ids=failed_land_ids,
                        failed_scene_ids=failed_scene_ids,
                        completed_scene_keys=completed_scene_keys,
                        abandon_event=abandon_event,
                        active_reservations=active_reservations,
                    ): scene
                    for scene in scenes
                }
                pending = set(futures)
                # 滚动宽限：时钟从并行开始（零完成兜底）或最近一次景完成时刻起算。
                last_completion = time.monotonic()
                while pending:
                    remaining = straggler_timeout - (time.monotonic() - last_completion)
                    if remaining <= 0:
                        done, not_done = set(), set(pending)
                    else:
                        done, not_done = wait(
                            pending,
                            timeout=remaining,
                            return_when=FIRST_COMPLETED,
                        )
                    if not done:
                        # 超时窗口内可能刚好有 future 结束；再扫一遍避免误杀刚完成的景。
                        done = {fut for fut in pending if fut.done()}
                        not_done = pending - done
                    for fut in done:
                        pending.discard(fut)
                        scene = futures[fut]
                        try:
                            fut.result()
                        except Exception as exc:
                            # 单景未捕获异常仍计入失败，避免整个 batch 默默丢景。
                            should_patch = False
                            with state_lock:
                                key = str(scene.get("id") or id(scene))
                                if key in completed_scene_keys:
                                    # 产品处理已完成，只重试未确认的进度/回执PATCH，不再误报地块失败。
                                    should_patch = True
                                elif not abandon_event.is_set():
                                    progress["failed"] += 1
                                    sid = str(scene.get("id") or "")
                                    if sid:
                                        failed_scene_ids.add(sid)
                                    key = sid or key
                                    info = active_reservations.pop(key, None)
                                    if info is not None:
                                        # 只回收未发布地块；同景已成功产品无需重复补偿。
                                        unpublished = [
                                            land
                                            for land in info[1]
                                            if str(land["meta"]["land_id"])
                                            not in info[2]
                                        ]
                                        failed_land_ids.update(
                                            str(land["meta"]["land_id"])
                                            for land in unpublished
                                        )
                                        _release_scene_date_reservation(
                                            unpublished, info[0]
                                        )
                                    progress["failed_land_ids"] = sorted(
                                        failed_land_ids
                                    )
                                    progress["failed_scene_ids"] = sorted(
                                        failed_scene_ids
                                    )
                                    progress["scenes_done"] += 1
                                    completed_scene_keys.add(key)
                                    progress_revision["value"] += 1
                                    should_patch = True
                            if should_patch:
                                patch_progress()
                            logger.exception(
                                "satellite_batch_scene_worker_crashed",
                                job_id=job_id,
                                scene_id=scene.get("id"),
                                error=str(exc),
                            )
                        last_completion = time.monotonic()
                    if done and pending:
                        continue
                    if not_done and not done:
                        abandon_event.set()
                        abandoned_ids: list[str] = []
                        no_abandoned_scenes = False
                        with state_lock:
                            for fut in list(not_done):
                                scene = futures[fut]
                                sid = str(scene.get("id") or "")
                                key = sid or str(id(scene))
                                if key in completed_scene_keys:
                                    # worker已完成状态合并；HTTP PATCH仍在锁外串行，不会阻塞超时汇总。
                                    pending.discard(fut)
                                    continue
                                info = active_reservations.pop(key, None)
                                if info is None and sid:
                                    info = active_reservations.pop(str(id(scene)), None)
                                if info is not None:
                                    # 已发布地块保留原结果，只释放并补偿本景未完成部分。
                                    unpublished = [
                                        land
                                        for land in info[1]
                                        if str(land["meta"]["land_id"])
                                        not in info[2]
                                    ]
                                    failed_land_ids.update(
                                        str(land["meta"]["land_id"])
                                        for land in unpublished
                                    )
                                    _release_scene_date_reservation(
                                        unpublished, info[0]
                                    )
                                else:
                                    # Future尚未占位时也按场景覆盖范围补齐失败地块，不能退化为空明细。
                                    unpublished = _scene_lands(
                                        scene, selected_lands, sensor
                                    )
                                    failed_land_ids.update(
                                        str(land["meta"]["land_id"])
                                        for land in unpublished
                                    )
                                    _release_scene_date_reservation(
                                        unpublished, scene["date"]
                                    )
                                if sid:
                                    failed_scene_ids.add(sid)
                                    abandoned_ids.append(sid)
                                else:
                                    abandoned_ids.append(key)
                                progress["failed"] += 1
                                progress["scenes_done"] += 1
                                fut.cancel()
                            if not abandoned_ids:
                                # 所有超时候选都已在锁内确认完成，无需把正常结果报成straggler。
                                last_completion = time.monotonic()
                                no_abandoned_scenes = True
                            else:
                                progress["failed_land_ids"] = sorted(failed_land_ids)
                                progress["failed_scene_ids"] = sorted(failed_scene_ids)
                                progress["failed_land_ids_complete"] = True
                                progress["straggler_abandoned_scene_ids"] = sorted(
                                    set(
                                        progress.get("straggler_abandoned_scene_ids")
                                        or []
                                    ).union(abandoned_ids)
                                )
                                progress_revision["value"] += 1
                        if no_abandoned_scenes:
                            continue
                        patch_progress()
                        timeout_progress = progress_snapshot()
                        logger.warning(
                            "scene_straggler_timeout",
                            job_id=job_id,
                            index=f"satellite_batch_{sensor}",
                            timeout_sec=straggler_timeout,
                            abandoned=len(abandoned_ids),
                            abandoned_scene_ids=abandoned_ids,
                            scenes_done=timeout_progress["scenes_done"],
                            scenes_total=timeout_progress["scenes_total"],
                            products_published=timeout_progress["products_published"],
                        )
                        pending.clear()
                        break
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        # 只有全部场景完成或超时分支逐一收集了未完成地块后，空列表才代表“确无失败地块”。
        with state_lock:
            progress["failed_land_ids_complete"] = True
            progress_revision["value"] += 1
        summary = progress_snapshot()
        logger.info(
            "scene_parallel_done",
            job_id=job_id,
            index=f"satellite_batch_{sensor}",
            scenes_done=summary["scenes_done"],
            products_published=summary["products_published"],
            failed=summary["failed"],
            workers=workers,
        )
        if sensor == "S2" and decloud_enabled():
            from app.tasks.decloud_uncrtaints import schedule_decloud_after_raw

            # 与worker的迟到发布共用状态锁：快照前完成的结果由本轮统一规划，
            # 快照后到达的结果由worker按单景补排，避免漏排或重复提交整批日期。
            decloud_snapshots = []
            with state_lock:
                for land in selected_lands:
                    land["decloud_finalized"] = True
                    raw_results = list(land["raw_results"])
                    if raw_results:
                        decloud_snapshots.append((land, raw_results))
            for land, raw_results in decloud_snapshots:
                if raw_results:
                    schedule_decloud_after_raw(
                        land_id=land["meta"]["land_id"],
                        date_from=str(d0),
                        date_to=str(d1),
                        raw_results=raw_results,
                        mq_task_id=f"{mq_task_id or job_id}:{land['meta']['land_id']}",
                        season_months=land["season_months"],
                        crop_type=land["crop_type"],
                    )
        # 先尽力提交未确认的场景回执；迟到worker后续仍会按同一幂等增量补写。
        patch_progress()
        status_progress = progress_snapshot()
        status = "failed" if status_progress["failed"] else "completed"
        if status == "failed":
            current_job = {**job, "progress_json": status_progress}
            if _schedule_satellite_compensation(
                job_id,
                current_job,
                compensation_attempt=compensation_attempt,
                error=f"{status_progress['failed']}个地块景处理失败",
            ):
                return {
                    "job_id": job_id,
                    "status": "compensating",
                    "compensation_attempt": compensation_attempt + 1,
                    **progress_snapshot(),
                }
            if _is_assessment_batch_child(job):
                # 补偿达到上限或派发失败时，补偿函数已将 Job 落为最终失败，
                # 不能再用本次 active=true 的进度覆盖最终状态。
                return {
                    "job_id": job_id,
                    "status": "failed",
                    **progress_snapshot(),
                }
        if compensation_attempt:
            # 共享同一进度字典，迟到worker仍使用此对象补写产品与场景回执。
            with state_lock:
                progress.update(
                    _compensation_progress(progress, compensation_attempt, active=False)
                )
                progress_revision["value"] += 1
        # 终态PATCH与进度PATCH共用串行锁，防止慢回执请求在终态之后写入旧进度快照。
        with progress_patch_lock:
            final_snapshot = progress_snapshot()
            failed_count = final_snapshot["failed"]
            # 每景增量已单独持久化；终态只提交汇总字段，避免重传累计回执列表。
            final_progress = {
                key: value
                for key, value in final_snapshot.items()
                if key != "published_products"
            }
            patch_job(
                job_id,
                {
                    "status": status,
                    "touch_finished": True,
                    "progress_json": final_progress,
                    "error": f"{failed_count}个地块景处理失败"
                    if failed_count
                    else "",
                },
            )
        return {"job_id": job_id, "status": status, **progress_snapshot()}
    except SoftTimeLimitExceeded as exc:
        # 12 分钟软超时：不落永久 failed，保持 running 并 Celery retry 重入队列。
        # 重入仍走 params.force（默认 false）+_load_lands 日期跳过，不重做已发布景。
        retry_abandon_event = locals().get("abandon_event")
        if retry_abandon_event is not None:
            # 软超时后旧线程仍可能完成当前上传；禁止它再启动下一块地的发布。
            retry_abandon_event.set()
        retry_state_lock = locals().get("state_lock")
        retry_lands = locals().get("selected_lands") or []
        retry_sensor = locals().get("sensor")
        retry_d0 = locals().get("d0")
        retry_d1 = locals().get("d1")
        retry_mq_task_id = locals().get("mq_task_id")
        if (
            retry_state_lock is not None
            and retry_sensor == "S2"
            and decloud_enabled()
        ):
            # 软超时会直接重排Celery任务，也必须先封口原始结果快照；
            # 快照后的在途结果会看到 finalized 标记并按单景补排。
            retry_decloud_snapshots = []
            with retry_state_lock:
                for land in retry_lands:
                    land["decloud_finalized"] = True
                    raw_results = list(land.get("raw_results") or [])
                    if raw_results:
                        retry_decloud_snapshots.append((land, raw_results))
            try:
                from app.tasks.decloud_uncrtaints import schedule_decloud_after_raw
            except Exception:
                logger.exception(
                    "satellite_batch_soft_timeout_decloud_scheduler_unavailable",
                    job_id=job_id,
                )
            else:
                # 各地块独立排程；单块数据或缓存异常不能阻断同批其他地块补排。
                for land, raw_results in retry_decloud_snapshots:
                    land_id = str(land["meta"]["land_id"])
                    try:
                        schedule_decloud_after_raw(
                            land_id=land_id,
                            date_from=str(retry_d0),
                            date_to=str(retry_d1),
                            raw_results=raw_results,
                            mq_task_id=(
                                f"{retry_mq_task_id or job_id}:{land_id}"
                            ),
                            season_months=land.get("season_months"),
                            crop_type=land.get("crop_type"),
                        )
                    except Exception:
                        # 记录失败地块以便测试环境按地块核对；不要丢弃同批后续地块的排程机会。
                        logger.exception(
                            "satellite_batch_soft_timeout_decloud_schedule_failed",
                            job_id=job_id,
                            land_id=land_id,
                            raw_scene_ids=[
                                str(result.get("scene_id") or result.get("stac_id") or "")
                                for result in raw_results
                                if isinstance(result, dict)
                            ],
                        )
        retries = int(getattr(getattr(self, "request", None), "retries", 0) or 0)
        logger.warning(
            "satellite_batch_soft_time_limit",
            job_id=job_id,
            soft_time_limit_sec=SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC,
            time_limit_sec=SATELLITE_BATCH_TIME_LIMIT_SEC,
            retries=retries,
            max_retries=SATELLITE_BATCH_SOFT_TIMEOUT_MAX_RETRIES,
            countdown_sec=SATELLITE_BATCH_SOFT_TIMEOUT_RETRY_COUNTDOWN_SEC,
        )
        try:
            pending_progress_patch = locals().get("patch_progress")
            if callable(pending_progress_patch):
                # 先尽力冲刷在途结果回执；失败时仍继续记录软超时并重排任务。
                try:
                    pending_progress_patch()
                except Exception:
                    logger.exception(
                        "satellite_batch_soft_timeout_progress_flush_failed",
                        job_id=job_id,
                    )
            patch_job(
                job_id,
                {
                    "status": "running",
                    # 只合并超时阶段字段，保留worker最新计数和已规范化的场景回执状态。
                    "progress_json": {
                        "stage": "soft_timeout_requeue",
                        "last_error": (
                            f"soft_time_limit={SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC}s "
                            f"retry={retries}/{SATELLITE_BATCH_SOFT_TIMEOUT_MAX_RETRIES}"
                        )[:2000],
                        "soft_time_limit_sec": SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC,
                        "soft_timeout_retries": retries,
                    },
                    "merge_progress": True,
                    "error": (
                        "任务软超时，稍后重试；已发布景将按日期跳过"
                    )[:2000],
                },
            )
        except Exception:
            logger.exception(
                "satellite_batch_soft_timeout_state_update_failed", job_id=job_id
            )
        try:
            raise self.retry(
                exc=exc,
                countdown=SATELLITE_BATCH_SOFT_TIMEOUT_RETRY_COUNTDOWN_SEC,
            )
        except MaxRetriesExceededError:
            logger.error(
                "satellite_batch_soft_time_limit_exhausted",
                job_id=job_id,
                retries=retries,
            )
        # 仅当 soft-timeout 重试次数耗尽时落到此处；self.retry 的 Retry 信号会直接抛出。
        compensation_scheduled = False
        try:
            if job is None:
                job = get_job(job_id)
            if _is_assessment_batch_child(job):
                compensation_scheduled = _schedule_satellite_compensation(
                    job_id,
                    job,
                    compensation_attempt=compensation_attempt,
                    error=str(exc),
                )
        except Exception:
            logger.exception("satellite_batch_compensation_failed", job_id=job_id)
        if compensation_scheduled:
            return {
                "job_id": job_id,
                "status": "compensating",
                "compensation_attempt": compensation_attempt + 1,
            }
        try:
            patch_job(
                job_id, {"status": "failed", "touch_finished": True, "error": str(exc)}
            )
        except Exception:
            logger.exception("satellite_batch_failure_report_failed", job_id=job_id)
        raise
    except Exception as exc:
        # HTTP和下载错误必须可见，不得把未产出数据的分组默认为成功。
        compensation_scheduled = False
        try:
            if job is None:
                job = get_job(job_id)
            if _is_assessment_batch_child(job):
                compensation_scheduled = _schedule_satellite_compensation(
                    job_id,
                    job,
                    compensation_attempt=compensation_attempt,
                    error=str(exc),
                )
        except Exception:
            logger.exception("satellite_batch_compensation_failed", job_id=job_id)
        if compensation_scheduled:
            return {
                "job_id": job_id,
                "status": "compensating",
                "compensation_attempt": compensation_attempt + 1,
            }
        try:
            patch_job(
                job_id, {"status": "failed", "touch_finished": True, "error": str(exc)}
            )
        except Exception:
            logger.exception("satellite_batch_failure_report_failed", job_id=job_id)
        raise
