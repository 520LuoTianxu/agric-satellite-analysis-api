"""Celery tasks: parcel-window UnCRtainTS decloud after agri optical ingest.

Default path (``DECLOUD_MODE=batch``): the optical job stores raw lonlat and
caches parcel windows; this module then buffers neighbor S2 (+ S1) windows
and declouds every cloudy target in the job range. Per-scene enqueue is a
fallback only when neighbors are already cached (``DECLOUD_MODE=per_scene``).

Runs only when ``DECLOUD_ENABLED=1`` and the scene/parcel cloud is above
30% (see ``DECLOUD_CLOUD_MIN_PCT``). Writes an additive lonlat_v1 product
(``scene_id`` suffix ``_decloud``, ``source=uncrtaints_decloud``). Raw S2
rows are never overwritten.

Official drought / timeseries / land metrics must use quality ``good`` only
(see ``app.core.decloud.score_decloud`` and ``is_official_optical_product``).
Fair and bad reconstructions are still written to OSS/MQ for audit.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import structlog
from shapely.geometry import mapping
from sqlalchemy import text

from app.core.decloud import (
    DECLOUD_SOURCE,
    DecloudQualityInputs,
    DecloudQualityResult,
    batch_neighbors_ready,
    cloudy_targets_from_raw,
    decloud_cloud_min_pct,
    decloud_drought_exclusion_flags,
    decloud_enabled,
    decloud_input_t,
    decloud_lookback_days,
    decloud_mode,
    decloud_oss_sensor,
    decloud_pixel_payload,
    decloud_quality_metrics,
    decloud_scene_id,
    decloud_use_sar,
    fallback_lonlat_pixels,
    geojson_ring_centroid,
    neighbor_window,
    pick_temporal_scenes,
    plan_decloud_after_raw,
    score_decloud,
    should_persist_decloud_product,
    should_trigger_decloud,
)
from app.core.geo import geojson_to_shape
from app.core.decloud_cache import (
    list_cached_s2,
    neighbor_counts_for_dates,
    put_window_meta,
    read_window_array,
    write_window_array,
)
from app.core.band_parallel import run_parallel_band_jobs
from app.core.uncrtaints import (
    S2_L2A_ASSET_MAP,
    S2_L2A_NO_B10,
    DecloudUnavailable,
    get_inferencer,
    stack_s2_13,
)
from app.tasks.agri_lonlat import (
    AGRI_OPTICAL_INDEX_KEYS,
    INDEX_KEY_TO_PIXEL,
    agri_optical_index_defs,
)
from app.tasks.indices import get_index
from app.tasks.pipeline import (
    RETRY_DELAYS,
    _resolve_band_radiometry,
    compute_target_grid,
    get_db_session,
    read_bands_windowed_parallel,
)
from app.worker import celery_app

logger = structlog.get_logger()

S1_MATCH_DAYS = 6
DECLOUD_ALGORITHM_VERSION = "uncrtaints-decloud-v7"


def _ndvi_from_raw_window(
    bands: dict[str, np.ndarray],
    band_radiometry: dict[str, dict[str, float]] | None,
) -> np.ndarray | None:
    """用原始窗口和场景定标缓存物理NDVI，供去云质量回退读取。"""
    if not band_radiometry or not {"B04", "B08"}.issubset(bands):
        return None
    if not {"B04", "B08"}.issubset(band_radiometry):
        return None
    ndvi = get_index("ndvi").formula(
        {"B04": bands["B04"], "B08": bands["B08"]},
        band_radiometry=band_radiometry,
    )
    ndvi[~np.isfinite(ndvi)] = np.nan
    return ndvi


def _database_url_configured() -> bool:
    """Detect an explicitly configured DB URL without opening a connection."""
    if any(
        (os.environ.get(name) or "").strip()
        for name in ("DATABASE_URL", "DATABASE_URL_SYNC")
    ):
        return True
    try:
        from agric_satellite_analysis_common.settings import settings

        # CommonSettings uses a development db default; model_fields_set lets us
        # distinguish that fallback from a non-empty value loaded from .env.
        fields_set = getattr(settings, "model_fields_set", set())
        return bool({"database_url", "database_url_sync"} & set(fields_set))
    except (ImportError, TypeError):
        return False


def _decloud_http_only() -> bool:
    """Return whether decloud must use the API data plane instead of SyncSession.

    Download-host ``.env`` files intentionally leave both database URLs blank.
    Shared settings have a development ``db`` fallback, so checking the
    configured environment before creating a session is required here.
    """
    # 没有显式数据库 URL 时无论 Internal HTTP 是否配置完整，都不能回退到
    # Settings 的默认 db；配置错误应暴露为 HTTP 配置错误，而不是 PG 连接错误。
    if not _database_url_configured():
        return True

    try:
        from agric_satellite_analysis_common.internal_api import internal_api_enabled
    except ImportError:
        return False

    if not internal_api_enabled():
        return False

    # 已显式切到 HTTP-only 的下载机继续复用统一判定；claim/空 URL 场景还要
    # 额外避开 Settings 的默认 db 主机名，不能让 get_db_session 先被创建。
    try:
        from app.core.http_mode import ingest_http_only

        if ingest_http_only():
            return True
    except ImportError:
        pass

    return False


def enqueue_parcel_decloud(
    *,
    land_id: str,
    date_str: str,
    mq_task_id: str | None = None,
    raw_scene_id: str | None = None,
    stac_cloud: float | None = None,
    parcel_cloud: float | None = None,
    cloud_over_30: bool | None = None,
) -> bool:
    """Enqueue one-date decloud (retrigger / per_scene fallback).

    The default optical path uses ``enqueue_decloud_parcel_batch`` instead.
    """
    if not decloud_enabled():
        return False
    if not should_trigger_decloud(
        cloud_cover_over_30=cloud_over_30,
        parcel_cloud_cover_pct=parcel_cloud,
        cloud_cover=stac_cloud,
        cloud_min_pct=decloud_cloud_min_pct(),
    ):
        return False
    process_parcel_decloud.apply_async(
        args=(
            str(land_id),
            date_str,
            mq_task_id,
            raw_scene_id,
            stac_cloud,
            parcel_cloud,
            cloud_over_30,
        ),
        queue="decloud",
    )
    logger.info(
        "decloud_enqueued",
        land_id=str(land_id),
        date=date_str,
        stac_cloud=stac_cloud,
        parcel_cloud=parcel_cloud,
        mode="per_scene",
    )
    return True


def enqueue_decloud_parcel_batch(
    *,
    land_id: str,
    date_from: str,
    date_to: str,
    targets: list[dict[str, Any]] | None = None,
    mq_task_id: str | None = None,
    season_months: list[int] | tuple[int, ...] | None = None,
) -> bool:
    """Enqueue job-level buffer-then-decloud after raw lonlat is stored."""
    if not decloud_enabled():
        return False
    decloud_parcel_batch.apply_async(
        args=(
            str(land_id),
            str(date_from)[:10],
            str(date_to)[:10],
            list(targets or []),
            mq_task_id,
            list(season_months) if season_months is not None else None,
        ),
        queue="decloud",
    )
    logger.info(
        "decloud_batch_enqueued",
        land_id=str(land_id),
        date_from=str(date_from)[:10],
        date_to=str(date_to)[:10],
        targets=len(targets or []),
    )
    return True


def cache_optical_s2_window(
    *,
    land_id: str,
    date_str: str,
    bands: dict[str, Any],
    band_hrefs: dict[str, str] | None,
    cloud_cover: float | None,
    stac_id: str | None,
    target_shape: tuple[int, int],
    target_transform,
    target_crs: str,
    field_mask: np.ndarray,
    band_radiometry: dict[str, dict[str, float]] | None = None,
    band_radiometry_source: str | None = None,
    band_radiometry_sources: dict[str, str] | None = None,
) -> None:
    """Store a parcel-window S2 stack from the optical download (not a full scene).

    原始光学下载已完成时直接复用对齐后的13波段窗口和定标元数据，避免后续去云重复拉取同一景；
    缓存键同时绑定地块掩膜与目标网格，避免边界变化后把旧网格数组当成新数据使用。
    """
    hrefs = {k: v for k, v in (band_hrefs or {}).items() if k != "SCL" and v}
    radiometry_meta = {
        key: value
        for key, value in {
            "band_radiometry": band_radiometry,
            "band_radiometry_source": band_radiometry_source,
            "band_radiometry_sources": band_radiometry_sources,
        }.items()
        if value is not None
    }
    put_window_meta(
        land_id=str(land_id),
        date_str=date_str,
        sensor="S2",
        cloud_cover=cloud_cover,
        stac_id=stac_id,
        band_hrefs=hrefs,
        extra=radiometry_meta,
    )
    if not bands:
        return
    from app.core.uncrtaints import stack_s2_13
    from app.core.decloud_cache import window_grid_key

    stack = stack_s2_13(bands)
    # 质量回退需要按反射率计算历史NDVI，缓存时从未裁剪的原始数组预先生成。
    ndvi = _ndvi_from_raw_window(bands, band_radiometry)
    grid_key = window_grid_key(
        target_crs, target_shape, target_transform, field_mask
    )
    try:
        write_window_array(
            str(land_id),
            date_str,
            "S2",
            grid_key=grid_key,
            stack=stack,
            field_mask=field_mask,
            ndvi=ndvi,
        )
    except OSError as exc:
        logger.warning(
            "decloud_cache_array_write_failed",
            land_id=str(land_id),
            date=date_str,
            error=str(exc),
        )


def schedule_decloud_after_raw(
    *,
    land_id: str,
    date_from: str,
    date_to: str,
    raw_results: list[dict[str, Any]] | None,
    mq_task_id: str | None = None,
    season_months: tuple[int, ...] | list[int] | None = None,
    crop_type: str | None = None,
) -> dict[str, Any]:
    """Apply ``plan_decloud_after_raw`` and enqueue the chosen path.

    根据同地块原始结果和邻景缓存覆盖率，在逐景补云与批量缓冲之间选择；
    邻景不够时暂缓正式去云，避免把样本不足的重建误标成可用于旱情分析的官方产品。
    """
    if not decloud_enabled():
        return {"enabled": False, "batch": False, "per_scene": []}
    from app.core.decloud import decloud_season_months

    months = (
        season_months
        if season_months is not None
        else decloud_season_months(crop_type)
    )
    dates = [
        str(r["date"])[:10]
        for r in (raw_results or [])
        if r and r.get("date")
    ]
    plan = plan_decloud_after_raw(
        enabled=True,
        mode=decloud_mode(),
        raw_results=raw_results,
        cached_neighbor_counts=neighbor_counts_for_dates(str(land_id), dates),
        input_t=decloud_input_t(),
        season_months=months,
    )
    by_date = {
        str(r["date"])[:10]: r for r in (raw_results or []) if r and r.get("date")
    }
    for date_str in plan.per_scene_dates:
        row = by_date.get(date_str) or {}
        enqueue_parcel_decloud(
            land_id=str(land_id),
            date_str=date_str,
            mq_task_id=mq_task_id,
            raw_scene_id=row.get("scene_id"),
            stac_cloud=row.get("cloud_cover"),
            parcel_cloud=row.get("parcel_cloud_cover_pct"),
            cloud_over_30=row.get("cloud_cover_over_30"),
        )
    if plan.batch:
        enqueue_decloud_parcel_batch(
            land_id=str(land_id),
            date_from=date_from,
            date_to=date_to,
            targets=list(plan.batch_targets),
            mq_task_id=mq_task_id,
            season_months=months,
        )
    logger.info(
        "decloud_scheduled_after_raw",
        land_id=str(land_id),
        mode=decloud_mode(),
        season_months=list(months),
        per_scene=list(plan.per_scene_dates),
        batch=plan.batch,
        hold=list(plan.hold_decloud_dates),
        raw_stored=plan.store_raw,
    )
    return {
        "enabled": True,
        "mode": decloud_mode(),
        "store_raw": plan.store_raw,
        "per_scene": list(plan.per_scene_dates),
        "batch": plan.batch,
        "hold_decloud_dates": list(plan.hold_decloud_dates),
        "targets": len(plan.batch_targets),
        "season_months": list(months),
    }


def _search_s2_l2a_windows(
    land_geom_geojson: dict,
    date_from: date,
    date_to: date,
    *,
    max_cloud: float = 100.0,
) -> list[dict[str, Any]]:
    """STAC search for full L2A band HREFs (parcel windows only)."""
    import os

    from app.tasks.pipeline import STAC_API_URL, STAC_COLLECTION
    from app.core.stac_client import open_stac_client

    catalog = open_stac_client(os.environ.get("STAC_API_URL", STAC_API_URL))
    search = catalog.search(
        collections=[STAC_COLLECTION],
        intersects=land_geom_geojson,
        datetime=f"{date_from.isoformat()}/{date_to.isoformat()}",
        # lte so DECLOUD_STAC_CLOUD_MAX_PCT=100 includes 100.0% scenes
        query={"eo:cloud_cover": {"lte": max_cloud}},
        # 邻景查询也要完整分页，避免长时序只保留接口先返回的80景。
        max_items=None,
    )
    items = list(search.items())
    by_date: dict[date, dict[str, Any]] = {}
    for item in items:
        item_date = item.datetime.date() if item.datetime else None
        if item_date is None:
            continue
        hrefs: dict[str, str] = {}
        ok = True
        for band in S2_L2A_NO_B10:
            href = None
            for asset_name in S2_L2A_ASSET_MAP.get(band, (band,)):
                asset = item.assets.get(asset_name)
                if asset:
                    href = asset.href
                    break
            if not href:
                ok = False
                break
            hrefs[band] = href
        if not ok:
            continue
        (
            band_radiometry,
            band_radiometry_source,
            band_radiometry_sources,
            radiometry_error,
        ) = _resolve_band_radiometry(item, agri_optical_index_defs())
        if band_radiometry is None:
            # 模型缓冲景也要保留可核验的反射率定标，避免质量回退混用DN口径。
            logger.warning(
                "decloud_s2_missing_radiometry",
                scene_id=item.id,
                processing_baseline=(item.properties or {}).get(
                    "s2:processing_baseline"
                ),
                reason=radiometry_error,
            )
            continue
        cloud = float(item.properties.get("eo:cloud_cover", 100) or 100)
        prev = by_date.get(item_date)
        if prev is None or cloud < prev["cloud_cover"]:
            by_date[item_date] = {
                "id": item.id,
                "date": item_date,
                "cloud_cover": cloud,
                "band_hrefs": hrefs,
                "band_radiometry": band_radiometry,
                "band_radiometry_source": band_radiometry_source,
                "band_radiometry_sources": band_radiometry_sources,
            }
    return [by_date[d] for d in sorted(by_date)]


def _pick_temporal_scenes(
    scenes: list[dict[str, Any]],
    target: date,
    input_t: int,
) -> list[dict[str, Any]]:
    """Target plus nearest other S2 dates, length ``input_t`` (repeat if needed)."""
    return pick_temporal_scenes(scenes, target, input_t)


def _read_s1_for_dates(
    land_geom_geojson: dict,
    dates: list[date],
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    target_crs: str,
) -> list[np.ndarray]:
    """为每个S2日期匹配最近的S1双极化特征；匹配窗口内无场景时填零特征。"""
    from app.tasks.sentinel1 import (
        _read_band_windowed_db_profiled,
        _s1_radiometric_calibration,
        search_s1_scenes,
    )

    if not dates:
        return []
    d0 = min(dates) - timedelta(days=S1_MATCH_DAYS)
    d1 = max(dates) + timedelta(days=S1_MATCH_DAYS)
    s1_scenes = search_s1_scenes(land_geom_geojson, d0, d1)
    out: list[np.ndarray] = []
    h, w = target_shape
    for d in dates:
        best = None
        best_dt = S1_MATCH_DAYS + 1
        for sc in s1_scenes:
            delta = abs((sc["date"] - d).days)
            if delta <= S1_MATCH_DAYS and delta < best_dt:
                best = sc
                best_dt = delta
        if best is None:
            out.append(np.zeros((2, h, w), dtype=np.float32))
            continue
        # 去云模型的S1辅助特征必须沿用单景发布的Sigma0定标，避免训练/推理特征尺度漂移。
        _s1_radiometric_calibration(best)
        pol = run_parallel_band_jobs(
            {"vv": best["vv_href"], "vh": best["vh_href"]},
            lambda band, href: _read_band_windowed_db_profiled(
                href,
                bounds,
                target_shape,
                target_transform,
                target_crs,
                calibration_href=best.get(f"{band}_calibration_href"),
            ),
            scene_workers=1,
            log_context={
                "scene_id": best.get("id"),
                "date": best["date"].isoformat(),
                "sensor": "S1",
            },
        )
        out.append(np.stack([pol["vv"], pol["vh"]], axis=0))
    return out


def _index_arrays_from_reflectance(
    rec_01: np.ndarray,
    land_mask: np.ndarray | None,
) -> dict[str, np.ndarray]:
    """从已还原到0–1反射率单位的13波段S2数据重算农业指数。

    ``land_mask`` is optional. Publishing samples the polygon itself; masking
    first can wipe every cell on a small parcel and yield ``no_pixels``.
    """
    name_to_idx = {
        "B01": 0,
        "B02": 1,
        "B03": 2,
        "B04": 3,
        "B05": 4,
        "B06": 5,
        "B07": 6,
        "B08": 7,
        "B8A": 8,
        "B09": 9,
        "B11": 11,
        "B12": 12,
    }
    bands = {name: rec_01[i] for name, i in name_to_idx.items()}
    index_arrays: dict[str, np.ndarray] = {}
    for key in AGRI_OPTICAL_INDEX_KEYS:
        index_def = get_index(key)
        needed = {b: bands[b] for b in index_def.bands if b in bands}
        arr = index_def.formula(needed)
        arr[~np.isfinite(arr)] = np.nan
        if land_mask is not None:
            arr[~land_mask] = np.nan
        index_arrays[INDEX_KEY_TO_PIXEL[key]] = arr
    return index_arrays


def _rgb_stats(
    rec_01: np.ndarray, raw_dn: np.ndarray, land_mask: np.ndarray
) -> tuple[float, float, float, float]:
    """只用地块内重建值与原始值都有效的RGB样本计算质量统计。"""
    rec_rgb = rec_01[[1, 2, 3]]  # B02, B03, B04
    raw_rgb = raw_dn[[1, 2, 3]] / 10000.0
    mask = (
        np.asarray(land_mask, dtype=bool)
        & np.all(np.isfinite(rec_rgb), axis=0)
        & np.all(np.isfinite(raw_rgb), axis=0)
    )
    if not np.any(mask):
        # 没有地块内配对样本时返回非有限值，让质量门判为bad；不能拿窗口外背景替代。
        return (float("nan"),) * 4
    rec_vals = rec_rgb[:, mask]
    raw_vals = raw_rgb[:, mask]
    rec_mean = float(np.nanmean(rec_vals)) if rec_vals.size else 0.0
    raw_mean = float(np.nanmean(raw_vals)) if raw_vals.size else 0.0
    rec_std = float(np.nanstd(rec_vals)) if rec_vals.size else 0.0
    raw_std = float(np.nanstd(raw_vals)) if raw_vals.size else 0.0
    return rec_mean, raw_mean, rec_std, raw_std


def _neighbor_ndvi_http(land_id: str, target: date) -> float | None:
    """Read official S2 neighbor NDVI through the API internal data plane."""
    from agric_satellite_analysis_common.internal_api import season_growth_inputs

    window_from, window_to = neighbor_window(target)
    try:
        bundle = season_growth_inputs(
            str(land_id),
            date_from=window_from.isoformat(),
            date_to=window_to.isoformat(),
        )
    except Exception as exc:
        # 邻居 NDVI 只用于质量评分；HTTP 暂时不可用时仍允许本地缓存和
        # 当前重建结果继续完成，不能退回下载机直连业务库。
        logger.warning(
            "decloud_neighbor_ndvi_http_failed",
            land_id=str(land_id),
            target=target.isoformat(),
            error=str(exc),
        )
        return None

    values: list[float] = []
    for row in bundle.get("s2_rows") or []:
        if str(row.get("date") or "")[:10] == target.isoformat():
            continue
        # 旧产品的NDVI未必含基线加性偏移；质量比较只接收明确记载定标口径的结果。
        if row.get("radiometry_method") != "scale_offset_to_reflectance":
            continue
        if not row.get("official"):
            continue
        try:
            value = float(row.get("ndvi_avg"))
        except (TypeError, ValueError):
            continue
        if np.isfinite(value):
            values.append(value)
    if not values:
        return None
    return float(sum(values) / len(values))


def _neighbor_ndvi(session, land_id: str, target: date) -> float | None:
    """Mean NDVI of official-clear S2 neighbors in a +/- 45 day window."""
    if session is None:
        return _neighbor_ndvi_http(land_id, target)

    from app.core.agri_classify import official_s2_sql

    row = session.execute(
        text(
            f"""
            SELECT avg(ndvi_avg)::float AS m
            FROM agric_satellite.parcel_scene_products s
            WHERE s.land_id = :lid
              AND s.sensor = 'S2'
              AND s.date BETWEEN CAST(:d0 AS date) AND CAST(:d1 AS date)
              AND s.date <> CAST(:target AS date)
              AND s.ndvi_avg IS NOT NULL
              AND s.pixel_data->'radiometry'->>'method' = 'scale_offset_to_reflectance'
              AND {official_s2_sql("s")}
            """
        ),
        {
            "lid": str(land_id),
            "d0": neighbor_window(target)[0].isoformat(),
            "d1": neighbor_window(target)[1].isoformat(),
            "target": target.isoformat(),
            "cloud_max": 30.0,
        },
    ).first()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _publish_decloud_product(
    *,
    meta: dict[str, Any],
    date_str: str,
    land_id_str: str,
    index_arrays: dict[str, np.ndarray],
    field_mask: np.ndarray,
    transform,
    target_crs: str,
    geom4326: dict,
    quality: DecloudQualityResult,
    raw_scene_id: str | None,
    stac_cloud: float | None,
    parcel_cloud: float | None,
    mq_task_id: str | None,
    quality_inputs: DecloudQualityInputs | None = None,
) -> dict[str, Any] | None:
    from app.tasks.agri_lonlat import publish_optical_lonlat_to_oss_mq
    from app.tasks.bridge_stac_cogs_to_agri_lonlat import (
        EMIT_PIXEL_KEYS,
        _round6,
        _sample_lonlat,
        _stats,
    )

    pixels = _sample_lonlat(geom4326, index_arrays, transform, target_crs)
    if not pixels:
        pixels = _sample_lonlat(
            geom4326,
            index_arrays,
            transform,
            target_crs,
            require_finite_ndvi=False,
        )

    def _avg_triple(pix_key: str):
        if pix_key not in index_arrays:
            return None, None, None
        data = index_arrays[pix_key]
        if tuple(data.shape) != tuple(field_mask.shape):
            raise ValueError("decloud index array and land mask dimensions differ")
        # 像元发布仍按几何采样；地块均值只能统计掩膜内网格，不能混入外接矩形背景。
        return _stats(data[field_mask])

    # 每个指数只扫描一次地块掩膜；发布均值和分位统计复用同一结果，避免大栅格重复遍历。
    index_stats = {key: _avg_triple(key) for key in EMIT_PIXEL_KEYS}
    index_avgs = {key: stats[0] for key, stats in index_stats.items()}
    sampled_pixels = bool(pixels)
    if not pixels:
        centroid = geojson_ring_centroid(geom4326)
        lon, lat = centroid if centroid else (None, None)
        pixels = fallback_lonlat_pixels(
            pixels=pixels,
            index_avgs=index_avgs,
            lon=lon,
            lat=lat,
        )
        logger.info(
            "decloud_pixels_fallback",
            land_id=meta.get("land_id"),
            date=date_str,
            pixels=len(pixels),
            quality=quality.quality,
        )

    # 仅真实地块采样点可补充稀疏网格统计；质心占位点的0只表示缺测，不能回填指数均值。
    if sampled_pixels:
        for key in EMIT_PIXEL_KEYS:
            if index_avgs.get(key) is not None:
                continue
            vals = [
                float(px[key])
                for px in pixels
                if isinstance(px, dict) and px.get(key) is not None
            ]
            if not vals:
                continue
            index_avgs[key] = float(round(sum(vals) / len(vals), 6))

    has_finite_index = any(v is not None for v in index_avgs.values())
    if not should_persist_decloud_product(
        has_reconstruction=True,
        pixel_count=len(pixels),
        has_finite_index=has_finite_index,
    ):
        logger.info(
            "decloud_no_pixels",
            land_id=meta.get("land_id"),
            date=date_str,
        )
        return None

    official = quality.is_official
    over_30, parcel_excl = decloud_drought_exclusion_flags(official)
    metrics = (
        decloud_quality_metrics(quality_inputs) if quality_inputs is not None else None
    )
    pixel_data = decloud_pixel_payload(
        quality=quality.quality,
        score=quality.score,
        reasons=quality.reasons,
        raw_scene_id=raw_scene_id,
        pixels=pixels,
        metrics=metrics,
    )
    # 网格投影和缓存掩膜口径变更后升级版本，便于把新重建结果与历史结果区分。
    pixel_data["algorithm_version"] = DECLOUD_ALGORITHM_VERSION
    from app.tasks.pipeline import describe_target_grid

    pixel_data["analysis_grid"] = describe_target_grid(
        transform, index_arrays["NDVI"].shape, target_crs
    )
    # 质量统计已明确无有效NDVI时保持NULL；不能用零值伪装成真实的低绿度。
    quality_ndvi = (
        quality_inputs.ndvi_mean if quality_inputs is not None else None
    )
    if quality_inputs is not None:
        ndvi_avg = float(quality_ndvi) if quality_ndvi is not None else None
    else:
        ndvi_avg = (
            index_avgs.get("NDVI")
            if index_avgs.get("NDVI") is not None
            else index_stats["NDVI"][0]
        )
    row = {
        "land_id": meta["land_id"],
        "tile_id": meta["tile_id"],
        "date": date_str,
        "scene_id": decloud_scene_id(date_str),
        "land_name": meta["land_name"],
        "cloud_cover": stac_cloud,
        # fair/bad stay excluded from existing cloud>30 drought filters.
        "cloud_cover_over_30": over_30,
        "parcel_cloud_cover_pct": (
            _round6(parcel_cloud)
            if parcel_cloud is not None
            else (None if official else parcel_excl)
        ),
        "pixel_count": len(pixels),
        "generated_at_shanghai": datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S%z"
        ),
        "pixel_data_url": f"decloud://land/{land_id_str}/{date_str}",
        "ndvi_avg": ndvi_avg,
        "ndvi_min": index_stats["NDVI"][1],
        "ndvi_max": index_stats["NDVI"][2],
        "evi_avg": index_stats["EVI"][0],
        "evi_min": index_stats["EVI"][1],
        "evi_max": index_stats["EVI"][2],
        "ndmi_avg": index_stats["NDMI"][0],
        "ndmi_min": index_stats["NDMI"][1],
        "ndmi_max": index_stats["NDMI"][2],
        "ndre_avg": index_stats["NDRE"][0],
        "ndre_min": index_stats["NDRE"][1],
        "ndre_max": index_stats["NDRE"][2],
        "cire_avg": index_stats["CIre"][0],
        "cire_min": index_stats["CIre"][1],
        "cire_max": index_stats["CIre"][2],
        "mndwi_avg": index_stats["MNDWI"][0],
        "mndwi_min": index_stats["MNDWI"][1],
        "mndwi_max": index_stats["MNDWI"][2],
        "pixel_data": json.dumps(pixel_data, separators=(",", ":")),
        "json_oss_key": None,
        "_pixel_data_obj": pixel_data,
        "_source": DECLOUD_SOURCE,
        "_decloud_quality": quality.quality,
        "_decloud_score": quality.score,
    }
    json_url = publish_optical_lonlat_to_oss_mq(
        row,
        mq_task_id=mq_task_id,
        oss_sensor=decloud_oss_sensor(),
        result_delivery="http" if _decloud_http_only() else "mq",
        extra_extras={
            "source": DECLOUD_SOURCE,
            "decloud_quality": quality.quality,
            "decloud_score": quality.score,
            "decloud_reasons": quality.reasons,
            "official": official,
        },
    )
    return {
        "date": date_str,
        "pixels": len(pixels),
        "json_url": json_url,
        "quality": quality.quality,
        "score": quality.score,
        "official": official,
    }


def _cache_s2_scene(
    land_id: str,
    scene: dict[str, Any],
    *,
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    target_crs: str,
    field_mask: np.ndarray,
    force_read: bool = False,
) -> np.ndarray | None:
    """读取与网格指纹匹配的13波段缓存；缺缓存时按HREF补读有限窗口。"""
    sc_date = scene["date"]
    if isinstance(sc_date, str):
        sc_date = date.fromisoformat(sc_date[:10])
    iso = sc_date.isoformat()
    hrefs = {k: v for k, v in (scene.get("band_hrefs") or {}).items() if k != "SCL" and v}
    put_window_meta(
        land_id=str(land_id),
        date_str=iso,
        sensor="S2",
        cloud_cover=scene.get("cloud_cover"),
        stac_id=scene.get("id") or scene.get("stac_id"),
        band_hrefs=hrefs or None,
        extra={
            key: scene[key]
            for key in (
                "band_radiometry",
                "band_radiometry_source",
                "band_radiometry_sources",
            )
            if scene.get(key) is not None
        },
    )
    from app.core.decloud_cache import window_grid_key

    grid_key = window_grid_key(target_crs, target_shape, target_transform, field_mask)
    if not force_read:
        cached = read_window_array(
            str(land_id), iso, "S2", expected_grid_key=grid_key
        )
        if cached and cached.get("stack") is not None:
            return cached["stack"]
    if not hrefs:
        return None
    bands = read_bands_windowed_parallel(
        hrefs,
        bounds,
        target_shape,
        target_transform,
        target_crs=target_crs,
        log_context={
            "scene_id": scene.get("id") or scene.get("stac_id"),
            "date": iso,
            "sensor": "S2",
            "land_id": str(land_id),
        },
    )
    stack = stack_s2_13(bands)
    ndvi = _ndvi_from_raw_window(bands, scene.get("band_radiometry"))
    write_window_array(
        str(land_id),
        iso,
        "S2",
        grid_key=grid_key,
        stack=stack,
        field_mask=field_mask,
        ndvi=ndvi,
    )
    return stack


def _buffer_s2_windows(
    *,
    land_id: str,
    land_geom_geojson: dict,
    date_from: date,
    date_to: date,
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    target_crs: str,
    land_mask: np.ndarray,
) -> list[dict[str, Any]]:
    """STAC-search the pad range once and window any missing parcel stacks."""
    scenes = _search_s2_l2a_windows(
        land_geom_geojson, date_from, date_to, max_cloud=100.0
    )
    buffered: list[dict[str, Any]] = []
    from app.core.decloud_cache import window_grid_key

    grid_key = window_grid_key(target_crs, target_shape, target_transform, land_mask)
    for sc in scenes:
        stack = _cache_s2_scene(
            land_id,
            sc,
            bounds=bounds,
            target_shape=target_shape,
            target_transform=target_transform,
            target_crs=target_crs,
            field_mask=land_mask,
        )
        if stack is None:
            continue
        row = dict(sc)
        row["stack"] = stack
        buffered.append(row)
    # Also keep catalog-only dates already on disk (prior chunks).
    seen = {sc["date"] if isinstance(sc["date"], date) else date.fromisoformat(str(sc["date"])[:10]) for sc in buffered}
    for cached in list_cached_s2(str(land_id), date_from, date_to):
        if cached["date"] in seen:
            continue
        arr = read_window_array(
            str(land_id), cached["date"], "S2", expected_grid_key=grid_key
        )
        if arr and arr.get("stack") is not None:
            buffered.append(
                {
                    "id": cached.get("stac_id"),
                    "date": cached["date"],
                    "cloud_cover": cached.get("cloud_cover"),
                    "band_hrefs": cached.get("band_hrefs") or {},
                    "band_radiometry": cached.get("band_radiometry"),
                    "band_radiometry_source": cached.get(
                        "band_radiometry_source"
                    ),
                    "band_radiometry_sources": cached.get(
                        "band_radiometry_sources"
                    ),
                    "stack": arr["stack"],
                }
            )
            seen.add(cached["date"])
    buffered.sort(key=lambda s: s["date"] if isinstance(s["date"], date) else date.fromisoformat(str(s["date"])[:10]))
    logger.info(
        "decloud_s2_buffered",
        land_id=str(land_id),
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        windows=len(buffered),
    )
    return buffered


def _buffer_s1_for_dates(
    *,
    land_id: str,
    land_geom_geojson: dict,
    dates: list[date],
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    target_crs: str,
    land_mask: np.ndarray,
) -> dict[date, np.ndarray]:
    """Window nearest S1 VV/VH per S2 date and cache on scratch."""
    out: dict[date, np.ndarray] = {}
    from app.core.decloud_cache import window_grid_key

    grid_key = window_grid_key(target_crs, target_shape, target_transform, land_mask)
    if not dates:
        return out
    need: list[date] = []
    for d in dates:
        cached = read_window_array(
            str(land_id), d, "S1", expected_grid_key=grid_key
        )
        if cached and cached.get("stack") is not None:
            out[d] = cached["stack"]
        else:
            need.append(d)
    if not need:
        return out
    loaded = _read_s1_for_dates(
        land_geom_geojson, need, bounds, target_shape, target_transform, target_crs
    )
    for d, arr in zip(need, loaded):
        out[d] = arr
        write_window_array(
            str(land_id), d, "S1", grid_key=grid_key, stack=arr
        )
        put_window_meta(land_id=str(land_id), date_str=d, sensor="S1", has_array=True)
    logger.info(
        "decloud_s1_buffered",
        land_id=str(land_id),
        dates=len(dates),
        downloaded=len(need),
    )
    return out


def _neighbor_ndvi_from_cache(
    land_id: str, target: date, *, expected_grid_key: str
) -> float | None:
    """Mean NDVI from cached clear-ish S2 windows when PG is not yet written."""
    window_from, window_to = neighbor_window(target)
    vals: list[float] = []
    for sc in list_cached_s2(str(land_id), window_from, window_to):
        if sc["date"] == target:
            continue
        cloud = sc.get("cloud_cover")
        try:
            cloud_f = float(cloud) if cloud is not None else None
        except (TypeError, ValueError):
            cloud_f = None
        if cloud_f is not None and cloud_f > decloud_cloud_min_pct():
            continue
        arr = read_window_array(
            str(land_id), sc["date"], "S2", expected_grid_key=expected_grid_key
        )
        stack = None if arr is None else arr.get("stack")
        if stack is None:
            continue
        land_mask = None if arr is None else arr.get("field_mask")
        if land_mask is None or tuple(land_mask.shape) != tuple(stack.shape[-2:]):
            continue
        ndvi = None if arr is None else arr.get("ndvi")
        if ndvi is None:
            # 旧缓存的DN栈可能已被模型输入裁剪，缺少原始定标NDVI时宁可不作质量比较。
            continue
        if tuple(ndvi.shape) != tuple(land_mask.shape):
            continue
        valid = np.asarray(land_mask, dtype=bool) & np.isfinite(ndvi)
        finite = ndvi[valid]
        if finite.size:
            vals.append(float(np.mean(finite)))
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _decloud_one_from_buffer(
    *,
    session,
    land_meta: dict[str, Any],
    land_geom_geojson: dict,
    target_transform,
    target_crs: str,
    land_mask: np.ndarray,
    target: date,
    buffered_s2: list[dict[str, Any]],
    s1_by_date: dict[date, np.ndarray],
    raw_scene_id: str | None,
    stac_cloud: float | None,
    parcel_cloud: float | None,
    mq_task_id: str | None,
) -> dict[str, Any]:
    """Run UnCRtainTS on one cloudy date using already-buffered windows.

    只消费调用方已按同一地块网格缓冲的S2/S1窗口，不在每个目标日期重复检索和重投影；
    先检查可用邻景数量，再构造固定长度时序输入，质量门决定结果能否进入官方序列。
    """
    land_id = str(land_meta["land_id"])
    from app.core.decloud_cache import window_grid_key

    grid_key = window_grid_key(
        target_crs, land_mask.shape, target_transform, land_mask
    )
    input_t = decloud_input_t()
    if not batch_neighbors_ready(len(buffered_s2), input_t):
        logger.info(
            "decloud_neighbors_not_ready",
            land_id=land_id,
            date=target.isoformat(),
            usable=len(buffered_s2),
            input_t=input_t,
        )
        return {
            "status": "skipped",
            "reason": "neighbors_not_ready",
            "usable": len(buffered_s2),
            "input_t": input_t,
        }

    picked = _pick_temporal_scenes(buffered_s2, target, input_t)
    if not picked:
        return {"status": "skipped", "reason": "no_s2_context"}

    s2_list = []
    for sc in picked:
        stack = sc.get("stack")
        if stack is None:
            cached = read_window_array(
                land_id, sc["date"], "S2", expected_grid_key=grid_key
            )
            stack = None if cached is None else cached.get("stack")
        if stack is None:
            return {"status": "skipped", "reason": "window_missing", "date": str(sc["date"])}
        s2_list.append(stack)
    s2_stack = np.stack(s2_list, axis=0)

    s1_stack = None
    if decloud_use_sar():
        h, w = s2_stack.shape[-2], s2_stack.shape[-1]
        s1_bands = []
        for sc in picked:
            sc_date = sc["date"] if isinstance(sc["date"], date) else date.fromisoformat(str(sc["date"])[:10])
            arr = s1_by_date.get(sc_date)
            if arr is None:
                arr = np.zeros((2, h, w), dtype=np.float32)
            s1_bands.append(arr)
        if s1_bands:
            s1_stack = np.stack(s1_bands, axis=0)

    try:
        inferencer = get_inferencer()
        rec_01 = inferencer.reconstruct(
            s2_stack,
            s1_stack,
            [
                (sc["date"] if isinstance(sc["date"], date) else date.fromisoformat(str(sc["date"])[:10])).toordinal()
                for sc in picked
            ],
        )
    except DecloudUnavailable as exc:
        logger.warning(
            "decloud_unavailable",
            land_id=land_id,
            date=target.isoformat(),
            error=str(exc),
        )
        return {"status": "skipped", "reason": "unavailable", "detail": str(exc)}

    # Sample/publish without pre-masking so weak reconstructions still store.
    # 重建结果已是物理反射率，直接传入可避免把EVI/SAVI常数放大一万倍。
    index_arrays = _index_arrays_from_reflectance(rec_01, None)
    rgb_mean, rgb_raw, rgb_std, rgb_std_raw = _rgb_stats(
        rec_01, s2_stack[-1], land_mask
    )
    ndvi_for_quality = index_arrays.get("NDVI")
    ndvi_q = ndvi_for_quality if ndvi_for_quality is not None else rec_01[7]
    if tuple(ndvi_q.shape) != tuple(land_mask.shape):
        raise ValueError("decloud NDVI and land mask dimensions differ")
    # 质量门只需地块内NDVI均值；避免复制整幅栅格或为未使用的分位数做排序计算。
    valid_ndvi = np.isfinite(ndvi_q)
    valid_ndvi &= np.asarray(land_mask, dtype=bool)
    valid_count = int(np.count_nonzero(valid_ndvi))
    ndvi_mean = (
        float(np.sum(ndvi_q, where=valid_ndvi, dtype=np.float64) / valid_count)
        if valid_count
        else None
    )
    neighbor = _neighbor_ndvi(session, land_id, target)
    if neighbor is None:
        neighbor = _neighbor_ndvi_from_cache(
            land_id, target, expected_grid_key=grid_key
        )
    quality_inputs = DecloudQualityInputs(
        rgb_mean=rgb_mean,
        rgb_mean_raw=rgb_raw,
        rgb_std=rgb_std,
        rgb_std_raw=rgb_std_raw,
        ndvi_mean=ndvi_mean,
        neighbor_ndvi_mean=neighbor,
    )
    quality = score_decloud(quality_inputs)
    published = _publish_decloud_product(
        meta=land_meta,
        date_str=target.isoformat(),
        land_id_str=land_id,
        index_arrays=index_arrays,
        field_mask=land_mask,
        transform=target_transform,
        target_crs=target_crs,
        geom4326=land_geom_geojson,
        quality=quality,
        raw_scene_id=raw_scene_id,
        stac_cloud=stac_cloud,
        parcel_cloud=parcel_cloud,
        mq_task_id=mq_task_id,
        quality_inputs=quality_inputs,
    )
    logger.info(
        "decloud_published",
        land_id=land_id,
        date=target.isoformat(),
        quality=quality.quality,
        score=quality.score,
        reasons=quality.reasons,
        official=quality.is_official,
        pixels=(published or {}).get("pixels"),
    )
    return {
        "status": "ok" if published else "no_pixels",
        "quality": quality.quality,
        "score": quality.score,
        "reasons": quality.reasons,
        "official": quality.is_official,
        "published": published,
    }


def _land_context(session, land_id: str):
    if _decloud_http_only():
        from app.core.http_mode import resolve_land_http

        # claim 下载机不创建 SQLAlchemy session；canonical 地块几何和元数据
        # 均由 API 从业务库读取后通过 Internal HTTP 返回。
        remote = resolve_land_http(str(land_id))
        if str(remote.get("land_id") or "") != str(land_id):
            raise RuntimeError("internal land response does not match requested land_id")
        land_geom_geojson = remote.get("boundary_geojson")
        land_geom = geojson_to_shape(land_geom_geojson)
        land_meta = {
            "land_id": str(remote.get("land_id") or land_id),
            "tile_id": remote.get("tile_id"),
            "land_name": remote.get("land_name") or str(land_id),
        }
    else:
        from app.models.tables import LandParcel

        if session is None:
            raise RuntimeError("decloud database session is required outside HTTP mode")
        land = session.get(LandParcel, str(land_id))
        if land is None or land.deleted_at is not None:
            return None
        land_geom_geojson = land.boundary_geojson
        land_geom = geojson_to_shape(land_geom_geojson)
        land_meta = {
            "land_id": land.land_id,
            "tile_id": land.tile_id,
            "land_name": land.land_name or land.land_id,
        }

    if land_geom is None:
        return None
    land_geom_geojson = mapping(land_geom)
    from app.tasks.pipeline import analysis_crs_for_bounds

    target_crs = analysis_crs_for_bounds(land_geom.bounds)
    target_transform, target_shape, land_mask, bounds = compute_target_grid(
        land_geom.bounds, land_geom, target_crs=target_crs
    )
    return {
        "land_meta": land_meta,
        "land_geom_geojson": land_geom_geojson,
        "target_transform": target_transform,
        "target_crs": target_crs,
        "target_shape": target_shape,
        "land_mask": land_mask,
        "bounds": bounds,
    }


@celery_app.task(
    name="app.tasks.decloud_uncrtaints.process_parcel_decloud",
    bind=True,
    max_retries=2,
    time_limit=1800,
    soft_time_limit=1500,
    queue="decloud",
)
def process_parcel_decloud(
    self,
    land_id: str,
    date_str: str,
    mq_task_id: str | None = None,
    raw_scene_id: str | None = None,
    stac_cloud: float | None = None,
    parcel_cloud: float | None = None,
    cloud_over_30: bool | None = None,
) -> dict[str, Any]:
    """Reconstruct one cloudy parcel window (cache first, STAC only if needed)."""
    if not decloud_enabled():
        return {"status": "skipped", "reason": "decloud_disabled"}
    if not should_trigger_decloud(
        cloud_cover_over_30=cloud_over_30,
        parcel_cloud_cover_pct=parcel_cloud,
        cloud_cover=stac_cloud,
        cloud_min_pct=decloud_cloud_min_pct(),
    ):
        return {"status": "skipped", "reason": "below_cloud_threshold"}

    target = date.fromisoformat(str(date_str)[:10])
    session = None
    try:
        if not _decloud_http_only():
            session = get_db_session()
        ctx = _land_context(session, land_id)
        if ctx is None:
            return {"status": "error", "detail": "land_missing"}
        window_from, window_to = neighbor_window(target)
        buffered = _buffer_s2_windows(
            land_id=str(land_id),
            land_geom_geojson=ctx["land_geom_geojson"],
            date_from=window_from,
            date_to=window_to,
            bounds=ctx["bounds"],
            target_shape=ctx["target_shape"],
            target_transform=ctx["target_transform"],
            target_crs=ctx["target_crs"],
            land_mask=ctx["land_mask"],
        )
        s1_by_date: dict[date, np.ndarray] = {}
        if decloud_use_sar():
            s1_by_date = _buffer_s1_for_dates(
                land_id=str(land_id),
                land_geom_geojson=ctx["land_geom_geojson"],
                dates=[sc["date"] for sc in buffered],
                bounds=ctx["bounds"],
                target_shape=ctx["target_shape"],
                target_transform=ctx["target_transform"],
                target_crs=ctx["target_crs"],
                land_mask=ctx["land_mask"],
            )
        return _decloud_one_from_buffer(
            session=session,
            land_meta=ctx["land_meta"],
            land_geom_geojson=ctx["land_geom_geojson"],
            target_transform=ctx["target_transform"],
            target_crs=ctx["target_crs"],
            land_mask=ctx["land_mask"],
            target=target,
            buffered_s2=buffered,
            s1_by_date=s1_by_date,
            raw_scene_id=raw_scene_id,
            stac_cloud=stac_cloud,
            parcel_cloud=parcel_cloud,
            mq_task_id=mq_task_id,
        )
    except Exception as exc:
        logger.error("decloud_failed", land_id=land_id, date=date_str, error=str(exc))
        retry_num = self.request.retries
        if retry_num < len(RETRY_DELAYS):
            raise self.retry(exc=exc, countdown=RETRY_DELAYS[retry_num])
        raise
    finally:
        if session is not None:
            session.close()


@celery_app.task(
    name="app.tasks.decloud_uncrtaints.decloud_parcel_batch",
    bind=True,
    max_retries=2,
    time_limit=1800,
    soft_time_limit=1500,
    queue="decloud",
)
def decloud_parcel_batch(
    self,
    land_id: str,
    date_from: str,
    date_to: str,
    targets: list[dict[str, Any]] | None = None,
    mq_task_id: str | None = None,
    season_months: list[int] | None = None,
) -> dict[str, Any]:
    """Buffer S2 (+ S1) parcel windows for the job, then decloud cloudy dates.

    先为整个任务窗口一次性准备并缓存邻景，再逐目标日重建，以复用STAC读取和空间对齐成本；
    邻景未就绪时保留跳过/非官方状态，不能仅因模型有输出就提升成官方观测。

    Official cloudy-date OSS/MQ is published only after neighbors are in the
    local cache. Fair/bad products are stored and flagged non-official.
    """
    if not decloud_enabled():
        return {"status": "skipped", "reason": "decloud_disabled"}

    from app.core.decloud import date_in_decloud_season, normalize_season_months

    season_months = normalize_season_months(season_months=season_months)
    if targets:
        targets = [
            t
            for t in targets
            if date_in_decloud_season(
                str((t or {}).get("date") or "")[:10], season_months
            )
        ]
        if not targets:
            return {
                "status": "skipped",
                "reason": "no_in_season_cloudy_targets",
                "season_months": list(season_months),
            }

    start = date.fromisoformat(str(date_from)[:10])
    end = date.fromisoformat(str(date_to)[:10])
    if start > end:
        start, end = end, start
    pad = decloud_lookback_days()
    session = None
    try:
        if not _decloud_http_only():
            session = get_db_session()
        ctx = _land_context(session, land_id)
        if ctx is None:
            return {"status": "error", "detail": "land_missing"}

        buffered = _buffer_s2_windows(
            land_id=str(land_id),
            land_geom_geojson=ctx["land_geom_geojson"],
            date_from=start - timedelta(days=pad),
            date_to=end + timedelta(days=pad),
            bounds=ctx["bounds"],
            target_shape=ctx["target_shape"],
            target_transform=ctx["target_transform"],
            target_crs=ctx["target_crs"],
            land_mask=ctx["land_mask"],
        )
        s1_by_date: dict[date, np.ndarray] = {}
        if decloud_use_sar():
            s1_by_date = _buffer_s1_for_dates(
                land_id=str(land_id),
                land_geom_geojson=ctx["land_geom_geojson"],
                dates=[sc["date"] for sc in buffered],
                bounds=ctx["bounds"],
                target_shape=ctx["target_shape"],
                target_transform=ctx["target_transform"],
                target_crs=ctx["target_crs"],
                land_mask=ctx["land_mask"],
            )

        wanted = list(targets or [])
        if not wanted:
            wanted = cloudy_targets_from_raw(
                [
                    {
                        "date": sc["date"],
                        "scene_id": sc.get("id") or sc.get("stac_id"),
                        "cloud_cover": sc.get("cloud_cover"),
                        "parcel_cloud_cover_pct": sc.get("parcel_cloud"),
                        "cloud_cover_over_30": None,
                    }
                    for sc in buffered
                    if start
                    <= (
                        sc["date"]
                        if isinstance(sc["date"], date)
                        else date.fromisoformat(str(sc["date"])[:10])
                    )
                    <= end
                ]
            )

        results: list[dict[str, Any]] = []
        for item in wanted:
            target = date.fromisoformat(str(item.get("date"))[:10])
            if target < start or target > end:
                continue
            if not should_trigger_decloud(
                cloud_cover_over_30=item.get("cloud_over_30"),
                parcel_cloud_cover_pct=item.get("parcel_cloud"),
                cloud_cover=item.get("stac_cloud"),
                cloud_min_pct=decloud_cloud_min_pct(),
            ):
                results.append(
                    {"date": target.isoformat(), "status": "skipped", "reason": "below_cloud_threshold"}
                )
                continue
            one = _decloud_one_from_buffer(
                session=session,
                land_meta=ctx["land_meta"],
                land_geom_geojson=ctx["land_geom_geojson"],
                target_transform=ctx["target_transform"],
                target_crs=ctx["target_crs"],
                land_mask=ctx["land_mask"],
                target=target,
                buffered_s2=buffered,
                s1_by_date=s1_by_date,
                raw_scene_id=item.get("raw_scene_id"),
                stac_cloud=item.get("stac_cloud"),
                parcel_cloud=item.get("parcel_cloud"),
                mq_task_id=mq_task_id,
            )
            one["date"] = target.isoformat()
            results.append(one)

        published = [r for r in results if r.get("status") == "ok"]
        held = [r for r in results if r.get("reason") == "neighbors_not_ready"]
        logger.info(
            "decloud_batch_done",
            land_id=str(land_id),
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            buffered=len(buffered),
            targets=len(results),
            published=len(published),
            held=len(held),
        )
        return {
            "status": "ok",
            "buffered": len(buffered),
            "targets": len(results),
            "published": len(published),
            "held": len(held),
            "results": results,
        }
    except Exception as exc:
        logger.error(
            "decloud_batch_failed",
            land_id=land_id,
            date_from=str(date_from),
            date_to=str(date_to),
            error=str(exc),
        )
        retry_num = self.request.retries
        if retry_num < len(RETRY_DELAYS):
            raise self.retry(exc=exc, countdown=RETRY_DELAYS[retry_num])
        raise
    finally:
        if session is not None:
            session.close()
