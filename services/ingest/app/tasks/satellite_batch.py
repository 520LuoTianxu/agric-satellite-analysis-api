"""按聚合窗口下载一次影像，在内存中裁到请求地块后发布现有OSS/MQ结果。"""

from datetime import date

import numpy as np
import structlog
from rasterio.features import geometry_mask
from rasterio.warp import Resampling, reproject
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from agric_satellite_analysis_common.internal_api import (
    agri_scene_dates,
    get_job,
    patch_job,
    resolve_land,
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
from app.core.true_color_preview import upload_field_rgb_preview
from app.tasks.agri_lonlat import (
    INDEX_KEY_TO_PIXEL,
    SCL_STAC_ASSETS,
    agri_optical_index_defs,
    emit_optical_lonlat,
    parcel_cloud_from_scl_window,
)
from app.tasks.pipeline import (
    compute_target_grid,
    compute_zonal_stats,
    read_band_windowed,
    read_bands_windowed_parallel,
    search_scenes_for_defs,
)
from app.tasks.sentinel1 import (
    _read_band_windowed_db,
    _sample_s1_lonlat,
    _upsert_agri_s1,
    search_s1_scenes,
)
from app.worker import celery_app

logger = structlog.get_logger()


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


def _load_lands(land_ids, sensor, force):
    """严格通过Internal HTTP读取地块与已有景日期，禁止回退直连API数据库。"""
    lands = []
    for land_id in land_ids:
        remote = resolve_land(land_id=land_id)
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
                "season_months": normalize_season_months(
                    crop_type=remote.get("crop_type")
                ),
                "crop_type": remote.get("crop_type"),
                "raw_results": [],
            }
        )
    return lands


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


def _download_scene(scene, sensor, grid):
    shared_transform, shared_shape, _, bounds = grid
    if sensor == "S1":
        bands = run_parallel_band_jobs(
            {"vv": scene["vv_href"], "vh": scene["vh_href"]},
            lambda _, href: _read_band_windowed_db(
                href, bounds, shared_shape, shared_transform
            ),
            scene_workers=1,
        )
        return bands, None
    hrefs = dict(scene["band_hrefs"])
    scl_href = hrefs.pop("SCL", None)
    # RGB预览复用光谱波段，不再为每个地块重复下载visual或周边窗口。
    hrefs.pop("visual", None)
    bands = read_bands_windowed_parallel(
        hrefs, bounds, shared_shape, shared_transform, scene_workers=1
    )
    # Sentinel-2零值为景外/无数据，先转NaN，避免EVI等公式把填充值算成有效像元。
    for band in bands.values():
        band[band == 0] = np.nan
    scl = None
    if scl_href:
        scl = read_band_windowed(
            scl_href,
            bounds,
            shared_shape,
            shared_transform,
            resampling=Resampling.nearest,
        )
    return bands, scl


def _publish_land(
    scene, sensor, land, shared_bands, shared_scl, shared_grid, parent_id
):
    target_transform, target_shape, field_mask, _ = land["grid"]
    bands = {
        key: crop_shared_array(value, shared_grid[0], target_shape, target_transform)
        for key, value in shared_bands.items()
    }
    meta = land["meta"]
    geom_json = mapping(land["geom"])
    # 同组多个地块同一天的结果必须具有不同消息编号，否则结果写入端会去重丢数据。
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
    )
    if result:
        land["raw_results"].append(result)
    return result is not None


@celery_app.task(
    name="app.tasks.satellite_batch.process_satellite_batch",
    time_limit=1800,
    soft_time_limit=1500,
)
def process_satellite_batch(job_id: str, mq_task_id: str | None = None) -> dict:
    """一个组、传感器和时间分片共用下载，逐地块掩膜并报告任务进度。"""
    try:
        job = get_job(job_id)
        if job.get("status") == "completed":
            return {"job_id": job_id, "status": "already_handled"}
        params = job["params_json"]
        sensor = params["sensor"]
        if sensor not in {"S1", "S2"}:
            raise ValueError(f"不支持的遥感传感器: {sensor}")
        d0, d1 = (
            date.fromisoformat(params["date_from"]),
            date.fromisoformat(params["date_to"]),
        )
        patch_job(job_id, {"status": "running", "touch_started": True})
        lands = _load_lands(params["land_ids"], sensor, params.get("force", False))
        # 任务排队期间边界可能更新；按HTTP最新边界重新求范围，保证完整覆盖。
        union = unary_union([land["geom"] for land in lands])
        grid = compute_target_grid(union.bounds, union)
        if sensor == "S1":
            scenes = search_s1_scenes(mapping(union), d0, d1, dedupe_week=False)
        else:
            extra_assets = {"SCL": SCL_STAC_ASSETS}
            if decloud_enabled():
                extra_assets.update(decloud_s2_extra_assets())
            scenes = search_scenes_for_defs(
                mapping(union),
                d0,
                d1,
                agri_optical_index_defs(),
                index_label="satellite_batch",
                max_cloud_cover=decloud_stac_cloud_max_pct()
                if decloud_enabled()
                else None,
                extra_assets=extra_assets,
                cloud_dedupe="none",
                max_items=2000,
            )
        progress = {
            "scenes_total": len(scenes),
            "scenes_done": 0,
            "products_published": 0,
            "failed": 0,
            "published_products": [],
        }
        for scene in scenes:
            selected = _scene_lands(scene, lands, sensor)
            if selected:
                try:
                    shared_bands, scl = _download_scene(scene, sensor, grid)
                except Exception as exc:
                    progress["failed"] += len(selected)
                    logger.error(
                        "satellite_batch_download_failed",
                        job_id=job_id,
                        scene_id=scene["id"],
                        error=str(exc),
                    )
                else:
                    for land in selected:
                        try:
                            if _publish_land(
                                scene,
                                sensor,
                                land,
                                shared_bands,
                                scl,
                                grid,
                                mq_task_id or job_id,
                            ):
                                progress["products_published"] += 1
                                # 完成下载不等于MQ结果已入库；API按这些地块/日期确认后才生成每日快照。
                                progress["published_products"].append(
                                    {
                                        "land_id": land["meta"]["land_id"],
                                        "date": scene["date"].isoformat(),
                                    }
                                )
                                # 同日优先景成功后跳过后续重复景，避免重下载和同一消息编号覆盖。
                                land["existing"].add(scene["date"])
                            else:
                                progress["failed"] += 1
                        except Exception as exc:
                            progress["failed"] += 1
                            logger.error(
                                "satellite_batch_land_failed",
                                job_id=job_id,
                                land_id=land["meta"]["land_id"],
                                error=str(exc),
                            )
            progress["scenes_done"] += 1
            patch_job(job_id, {"progress_json": progress})
        if sensor == "S2" and decloud_enabled():
            from app.tasks.decloud_uncrtaints import schedule_decloud_after_raw

            for land in lands:
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
        try:
            patch_job(
                job_id, {"status": "failed", "touch_finished": True, "error": str(exc)}
            )
        except Exception:
            logger.exception("satellite_batch_failure_report_failed", job_id=job_id)
        raise
