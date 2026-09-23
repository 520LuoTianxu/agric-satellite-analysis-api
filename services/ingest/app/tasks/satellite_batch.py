"""按聚合窗口下载一次影像，在内存中裁到请求地块后回调 API 结果缓存。"""

import os
import uuid
from datetime import date

import numpy as np
import structlog
from rasterio.features import geometry_mask
from rasterio.warp import Resampling, reproject
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from agric_satellite_analysis_common.internal_api import (
    agri_scene_dates,
    get_job,
    patch_job,
    resolve_land,
)
from agric_satellite_analysis_common.scheduled_land_filter import (
    is_scheduled_land_allowed,
)
from app.core.band_parallel import run_parallel_band_jobs
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
    compute_target_grid,
    compute_zonal_stats,
    read_bands_windowed_parallel,
    search_scenes_for_defs,
)
from app.tasks.sentinel1 import (
    _read_band_windowed_db_profiled,
    _sample_s1_lonlat,
    _upsert_agri_s1,
    search_s1_scenes,
)
from app.worker import celery_app

logger = structlog.get_logger()

SATELLITE_BATCH_MAX_COMPENSATIONS = 2
SATELLITE_COMPENSATION_DELAY_SECONDS = 30


def _positive_limit_env(name: str, default: int) -> int:
    try:
        return max(60, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


# 默认保持现网 25/30 分钟；若业务要求绝对十分钟内退出，可配置为 540/600。
# soft limit 可能无法打断 GDAL C 调用，最终由 hard limit 回收整个 prefork 子进程。
SATELLITE_BATCH_TIME_LIMIT_SEC = _positive_limit_env(
    "SATELLITE_BATCH_TIME_LIMIT_SEC", 1800
)
SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC = min(
    SATELLITE_BATCH_TIME_LIMIT_SEC - 1,
    _positive_limit_env("SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC", 1500),
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
    source, source_transform, target_shape, target_transform, *, categorical=False
):
    """从共享数组重采样到原有地块网格；新数组防止地块掩膜污染邻居的数据。"""
    destination = np.full(target_shape, np.nan, dtype=np.float32)
    reproject(
        source=source,
        destination=destination,
        src_transform=source_transform,
        src_crs="EPSG:4326",
        dst_transform=target_transform,
        dst_crs="EPSG:4326",
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling.nearest if categorical else Resampling.bilinear,
    )
    return destination


def _load_lands(land_ids, sensor, force, season_months=None, growing_seasons=None):
    """严格通过Internal HTTP读取地块与已有景日期，禁止回退直连API数据库。"""
    lands = []
    for land_id in land_ids:
        remote = resolve_land(land_id=land_id)
        # 任务可能在过滤规则发布前已经入队，因此下载机执行前再兜底过滤，
        # 防止被排除基地或超大地块继续访问 STAC、OSS 和卫星数据。
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
        if str(remote.get("land_id")) != land_id or not remote.get("boundary_geojson"):
            raise RuntimeError(f"地块{land_id}的内部HTTP元数据不完整")
        geom = shape(remote["boundary_geojson"])
        if (
            geom.is_empty
            or not geom.is_valid
            or geom.geom_type not in {"Polygon", "MultiPolygon"}
        ):
            raise RuntimeError(f"地块{land_id}的边界无效")
        existing = set()
        if not force:
            existing = {
                date.fromisoformat(str(value)[:10])
                for value in agri_scene_dates(land_id, sensor=sensor)
            }
        lands.append(
            {
                "meta": {
                    "land_id": land_id,
                    "tile_id": remote["tile_id"],
                    "land_name": remote.get("land_name") or land_id,
                },
                "geom": geom,
                "grid": compute_target_grid(geom.bounds, geom),
                "existing": existing,
                # 显式回填的轮作月份优先于作物默认季节，避免区域下载误过滤用户选定窗口。
                "season_months": normalize_season_months(
                    season_months=season_months,
                    growing_seasons=growing_seasons,
                    crop_type=remote.get("crop_type"),
                ),
                "crop_type": remote.get("crop_type"),
                "raw_results": [],
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
    """Return the shared processing geometry and only fully covered parcels."""
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


def _download_scene(scene, sensor, grid, *, job_id: str | None = None):
    """下载一个共享景窗口；全部波段走同一并发池和统一重试/日志链路。"""
    shared_transform, shared_shape, _, bounds = grid
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
        hrefs = {"vv": scene["vv_href"], "vh": scene["vh_href"]}
        resampling_by_band = {}
    else:
        hrefs = dict(scene["band_hrefs"])
        # RGB预览复用光谱波段，不把 visual 三通道资产混入指数波段缓存。
        hrefs.pop("visual", None)
        resampling_by_band = {"SCL": Resampling.nearest}

    if sensor == "S1":
        bands = run_parallel_band_jobs(
            hrefs,
            lambda _, href: _read_band_windowed_db_profiled(
                href, bounds, shared_shape, shared_transform
            ),
            scene_workers=1,
            log_context=log_context,
        )
        return bands, None
    # SCL 与光谱波段一起进入线程池，避免所有光谱完成后再串行发起一次远程读取。
    downloaded = read_bands_windowed_parallel(
        hrefs,
        bounds,
        shared_shape,
        shared_transform,
        scene_workers=1,
        resampling_by_band={"SCL": Resampling.nearest},
        log_context=log_context,
    )
    scl = downloaded.pop("SCL", None)
    # Sentinel-2零值为景外/无数据，先转NaN，避免EVI等公式把填充值算成有效像元。
    for band in downloaded.values():
        band[band == 0] = np.nan
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
):
    target_transform, target_shape, field_mask, _ = land["grid"]
    bands = {
        key: crop_shared_array(value, shared_grid[0], target_shape, target_transform)
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
            geom_json, bands["vv"], bands["vh"], target_transform
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
            compute_zonal_stats(bands["vv"]),
            compute_zonal_stats(bands["vh"]),
            mq_task_id=land_task_id,
            relative_orbit=scene.get("relative_orbit"),
            processing_window_km=processing_window_km,
            processing_window_bounds=shared_grid[3],
            # 日批结果走 API HTTP -> Redis 缓存 -> API 入库，不让下载机直写 PG 或发结果 MQ。
            result_delivery="http",
        )
        return True

    scl = (
        crop_shared_array(
            shared_scl, shared_grid[0], target_shape, target_transform, categorical=True
        )
        if shared_scl is not None
        else None
    )
    indices = {}
    for definition in agri_optical_index_defs():
        array = definition.formula({key: bands[key] for key in definition.bands})
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
            )
        except Exception as exc:
            # 去云缓存与旧光学流程一样尽力保存，缓存失败不能阻断原始地块结果。
            logger.warning(
                "satellite_batch_decloud_cache_failed",
                land_id=meta["land_id"],
                error=str(exc),
            )
    shared_mask = geometry_mask(
        [land["geom"]], out_shape=shared_grid[1], transform=shared_grid[0], invert=True
    )
    rgb = upload_field_rgb_preview(
        land_id=meta["land_id"],
        date_str=scene["date"].isoformat(),
        bands=bands,
        field_mask=field_mask,
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
    if result:
        land["raw_results"].append(result)
    return result is not None


@celery_app.task(
    name="app.tasks.satellite_batch.process_satellite_batch",
    time_limit=SATELLITE_BATCH_TIME_LIMIT_SEC,
    soft_time_limit=SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC,
)
def process_satellite_batch(
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
        grid = compute_target_grid(
            processing_geom.bounds, union, padding_degrees=0.0
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

        def record_failures(lands_to_record, scene_id: str | None) -> None:
            failed_land_ids.update(str(land["meta"]["land_id"]) for land in lands_to_record)
            if scene_id:
                failed_scene_ids.add(str(scene_id))

        progress = {
            "scenes_total": len(scenes),
            "scenes_done": 0,
            "products_published": 0,
            "failed": 0,
            "failed_land_ids": [],
            "failed_scene_ids": [],
            "published_products": [],
        }
        if compensation_attempt:
            progress = _compensation_progress(
                progress, compensation_attempt, active=True
            )
        for scene in scenes:
            selected = _scene_lands(scene, selected_lands, sensor)
            if selected:
                scene_products: list[tuple[dict, list, dict, np.ndarray | None]] = []
                try:
                    shared_bands, scl = _download_scene(
                        scene, sensor, grid, job_id=job_id
                    )
                    scene_products.append((scene, selected, shared_bands, scl))
                except Exception as exc:
                    # 只有 AWS/Element84 的 S2 COG 读取失败才试 PC；S1 路径不变。
                    unresolved = {
                        str(land["meta"]["land_id"]): land for land in selected
                    }
                    if sensor == "S2" and scene.get("source_catalog") != "planetary_computer":
                        try:
                            fallback_scenes = _search_s2_scene_fallback(
                                scene, processing_geom
                            )
                        except Exception as fallback_error:
                            fallback_scenes = []
                            logger.warning(
                                "s2_planetary_computer_cog_fallback_search_failed",
                                job_id=job_id,
                                scene_id=scene.get("id"),
                                error=str(fallback_error),
                            )
                        # 一个窗口跨越 PC 瓦片边缘时按剩余覆盖地块数优先，必要时可由多景补齐。
                        fallback_scenes.sort(
                            key=lambda candidate: (
                                -len(_scene_lands(candidate, list(unresolved.values()), "S2")),
                                float(candidate.get("cloud_cover") or 100),
                                str(candidate.get("id") or ""),
                            )
                        )
                        for fallback_scene in fallback_scenes:
                            fallback_selected = _scene_lands(
                                fallback_scene, list(unresolved.values()), "S2"
                            )
                            if not fallback_selected:
                                continue
                            try:
                                fallback_bands, fallback_scl = _download_scene(
                                    fallback_scene, sensor, grid, job_id=job_id
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
                    if unresolved:
                        failed_lands = list(unresolved.values())
                        progress["failed"] += len(failed_lands)
                        record_failures(failed_lands, str(scene.get("id") or ""))
                        logger.error(
                            "satellite_batch_download_failed",
                            job_id=job_id,
                            scene_id=scene.get("id"),
                            error=str(exc),
                            unresolved_land_ids=sorted(unresolved),
                        )

                for product_scene, product_lands, shared_bands, scl in scene_products:
                    for land in product_lands:
                        try:
                            if _publish_land(
                                product_scene,
                                sensor,
                                land,
                                shared_bands,
                                scl,
                                grid,
                                mq_task_id or job_id,
                                processing_window_km,
                            ):
                                progress["products_published"] += 1
                                # 完成下载不等于MQ结果已入库；API按这些地块/日期确认后才生成每日快照。
                                progress["published_products"].append(
                                    {
                                        "land_id": land["meta"]["land_id"],
                                        "date": product_scene["date"].isoformat(),
                                    }
                                )
                                # 同日优先景成功后跳过后续重复景，避免重下载和同一消息编号覆盖。
                                land["existing"].add(scene["date"])
                            else:
                                progress["failed"] += 1
                                record_failures([land], product_scene.get("id"))
                        except Exception as exc:
                            progress["failed"] += 1
                            record_failures([land], product_scene.get("id"))
                            logger.error(
                                "satellite_batch_land_failed",
                                job_id=job_id,
                                land_id=land["meta"]["land_id"],
                                error=str(exc),
                            )
            progress["failed_land_ids"] = sorted(failed_land_ids)
            progress["failed_scene_ids"] = sorted(failed_scene_ids)
            progress["scenes_done"] += 1
            patch_job(job_id, {"progress_json": progress})
        if sensor == "S2" and decloud_enabled():
            from app.tasks.decloud_uncrtaints import schedule_decloud_after_raw

            for land in selected_lands:
                if land["raw_results"]:
                    schedule_decloud_after_raw(
                        land_id=land["meta"]["land_id"],
                        date_from=str(d0),
                        date_to=str(d1),
                        raw_results=land["raw_results"],
                        mq_task_id=f"{mq_task_id or job_id}:{land['meta']['land_id']}",
                        season_months=land["season_months"],
                        crop_type=land["crop_type"],
                    )
        status = "failed" if progress["failed"] else "completed"
        if status == "failed":
            current_job = {**job, "progress_json": progress}
            if _schedule_satellite_compensation(
                job_id,
                current_job,
                compensation_attempt=compensation_attempt,
                error=f"{progress['failed']}个地块景处理失败",
            ):
                return {
                    "job_id": job_id,
                    "status": "compensating",
                    "compensation_attempt": compensation_attempt + 1,
                    **progress,
                }
            if _is_assessment_batch_child(job):
                # 补偿达到上限或派发失败时，补偿函数已将 Job 落为最终失败，
                # 不能再用本次 active=true 的进度覆盖最终状态。
                return {"job_id": job_id, "status": "failed", **progress}
        if compensation_attempt:
            progress = _compensation_progress(
                progress, compensation_attempt, active=False
            )
        patch_job(
            job_id,
            {
                "status": status,
                "touch_finished": True,
                "progress_json": progress,
                "error": f"{progress['failed']}个地块景处理失败"
                if progress["failed"]
                else "",
            },
        )
        return {"job_id": job_id, "status": status, **progress}
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
