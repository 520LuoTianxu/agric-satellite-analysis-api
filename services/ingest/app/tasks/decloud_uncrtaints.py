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
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import structlog
from geoalchemy2.shape import to_shape
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
from app.core.decloud_cache import (
    list_cached_s2,
    neighbor_counts_for_dates,
    put_window_meta,
    read_window_array,
    write_window_array,
)
from app.core.uncrtaints import (
    S2_L2A_ASSET_MAP,
    S2_L2A_NO_B10,
    DecloudUnavailable,
    get_inferencer,
    stack_s2_13,
)
from app.tasks.agri_lonlat import AGRI_OPTICAL_INDEX_KEYS, INDEX_KEY_TO_PIXEL
from app.tasks.indices import get_index
from app.tasks.pipeline import (
    RETRY_DELAYS,
    compute_target_grid,
    compute_zonal_stats,
    get_db_session,
    read_bands_windowed_parallel,
)
from app.worker import celery_app

logger = structlog.get_logger()

S1_MATCH_DAYS = 6


def enqueue_parcel_decloud(
    *,
    field_id: str,
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
            field_id,
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
        field_id=field_id,
        land_id=str(land_id),
        date=date_str,
        stac_cloud=stac_cloud,
        parcel_cloud=parcel_cloud,
        mode="per_scene",
    )
    return True


def enqueue_decloud_parcel_batch(
    *,
    field_id: str,
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
            field_id,
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
        field_id=field_id,
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
) -> None:
    """Store a parcel-window S2 stack from the optical download (not a full scene)."""
    hrefs = {k: v for k, v in (band_hrefs or {}).items() if k != "SCL" and v}
    put_window_meta(
        land_id=str(land_id),
        date_str=date_str,
        sensor="S2",
        cloud_cover=cloud_cover,
        stac_id=stac_id,
        band_hrefs=hrefs,
    )
    if not bands:
        return
    from app.core.uncrtaints import stack_s2_13

    stack = stack_s2_13(bands)
    try:
        write_window_array(str(land_id), date_str, "S2", stack=stack)
    except OSError as exc:
        logger.warning(
            "decloud_cache_array_write_failed",
            land_id=str(land_id),
            date=date_str,
            error=str(exc),
        )


def schedule_decloud_after_raw(
    *,
    field_id: str,
    land_id: str,
    date_from: str,
    date_to: str,
    raw_results: list[dict[str, Any]] | None,
    mq_task_id: str | None = None,
    season_months: tuple[int, ...] | list[int] | None = None,
    crop_type: str | None = None,
) -> dict[str, Any]:
    """Apply ``plan_decloud_after_raw`` and enqueue the chosen path."""
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
            field_id=field_id,
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
            field_id=field_id,
            land_id=str(land_id),
            date_from=date_from,
            date_to=date_to,
            targets=list(plan.batch_targets),
            mq_task_id=mq_task_id,
            season_months=months,
        )
    logger.info(
        "decloud_scheduled_after_raw",
        field_id=field_id,
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
    field_geom_geojson: dict,
    date_from: date,
    date_to: date,
    *,
    max_cloud: float = 100.0,
) -> list[dict[str, Any]]:
    """STAC search for full L2A band HREFs (parcel windows only)."""
    import os

    from pystac_client import Client as STACClient

    from app.tasks.pipeline import STAC_API_URL, STAC_COLLECTION

    catalog = STACClient.open(os.environ.get("STAC_API_URL", STAC_API_URL))
    search = catalog.search(
        collections=[STAC_COLLECTION],
        intersects=field_geom_geojson,
        datetime=f"{date_from.isoformat()}/{date_to.isoformat()}",
        query={"eo:cloud_cover": {"lt": max_cloud}},
        max_items=80,
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
        cloud = float(item.properties.get("eo:cloud_cover", 100) or 100)
        prev = by_date.get(item_date)
        if prev is None or cloud < prev["cloud_cover"]:
            by_date[item_date] = {
                "id": item.id,
                "date": item_date,
                "cloud_cover": cloud,
                "band_hrefs": hrefs,
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
    field_geom_geojson: dict,
    dates: list[date],
    bounds: tuple,
    target_shape: tuple,
    target_transform,
) -> list[np.ndarray]:
    """Nearest S1 VV/VH (dB) per S2 date; zeros when none within S1_MATCH_DAYS."""
    from app.tasks.sentinel1 import _read_band_windowed_db, search_s1_scenes

    if not dates:
        return []
    d0 = min(dates) - timedelta(days=S1_MATCH_DAYS)
    d1 = max(dates) + timedelta(days=S1_MATCH_DAYS)
    s1_scenes = search_s1_scenes(field_geom_geojson, d0, d1)
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
        vv = _read_band_windowed_db(
            best["vv_href"], bounds, target_shape, target_transform
        )
        vh = _read_band_windowed_db(
            best["vh_href"], bounds, target_shape, target_transform
        )
        out.append(np.stack([vv, vh], axis=0))
    return out


def _index_arrays_from_reflectance(
    rec_01: np.ndarray,
    field_mask: np.ndarray | None,
) -> dict[str, np.ndarray]:
    """Recompute agri optical indices from reconstructed 13-band S2 (DN).

    ``field_mask`` is optional. Publishing samples the polygon itself; masking
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
        if field_mask is not None:
            arr[~field_mask] = np.nan
        index_arrays[INDEX_KEY_TO_PIXEL[key]] = arr
    return index_arrays


def _rgb_stats(
    rec_01: np.ndarray, raw_dn: np.ndarray, field_mask: np.ndarray
) -> tuple[float, float, float, float]:
    """Return (rgb_mean, rgb_mean_raw, rgb_std, rgb_std_raw) in 0-1 units."""
    rec_rgb = rec_01[[1, 2, 3]]  # B02, B03, B04
    raw_rgb = raw_dn[[1, 2, 3]] / 10000.0
    mask = field_mask & np.isfinite(rec_rgb[0])
    if not np.any(mask):
        mask = np.isfinite(rec_rgb[0])
    rec_vals = rec_rgb[:, mask]
    raw_vals = raw_rgb[:, mask]
    rec_mean = float(np.nanmean(rec_vals)) if rec_vals.size else 0.0
    raw_mean = float(np.nanmean(raw_vals)) if raw_vals.size else 0.0
    rec_std = float(np.nanstd(rec_vals)) if rec_vals.size else 0.0
    raw_std = float(np.nanstd(raw_vals)) if raw_vals.size else 0.0
    return rec_mean, raw_mean, rec_std, raw_std


def _neighbor_ndvi(session, land_id: str, target: date) -> float | None:
    """Mean NDVI of official-clear S2 neighbors in a +/- 45 day window."""
    from app.core.agri_classify import official_s2_sql

    row = session.execute(
        text(
            f"""
            SELECT avg(ndvi_avg)::float AS m
            FROM agri.parcel_scene_products s
            WHERE s.land_id = :lid
              AND s.sensor = 'S2'
              AND s.date BETWEEN CAST(:d0 AS date) AND CAST(:d1 AS date)
              AND s.date <> CAST(:target AS date)
              AND s.ndvi_avg IS NOT NULL
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
    field_id_str: str,
    index_arrays: dict[str, np.ndarray],
    transform,
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

    pixels = _sample_lonlat(geom4326, index_arrays, transform, "EPSG:4326")
    if not pixels:
        pixels = _sample_lonlat(
            geom4326,
            index_arrays,
            transform,
            "EPSG:4326",
            require_finite_ndvi=False,
        )

    def _avg_triple(pix_key: str):
        if pix_key not in index_arrays:
            return None, None, None
        return _stats(index_arrays[pix_key])

    index_avgs = {key: _avg_triple(key)[0] for key in EMIT_PIXEL_KEYS}
    if not pixels:
        centroid = geojson_ring_centroid(geom4326)
        lon, lat = centroid if centroid else (None, None)
        pixels = fallback_lonlat_pixels(
            pixels=pixels,
            index_avgs=index_avgs,
            lon=lon,
            lat=lat,
            allow_zero_stub=True,
        )
        logger.info(
            "decloud_pixels_fallback",
            land_id=meta.get("land_id"),
            date=date_str,
            pixels=len(pixels),
            quality=quality.quality,
        )

    # Prefer array stats; if reconstruction collapsed to stub pixels, fill avgs from pixels
    # so UI alt series (fair/bad) is not dropped for null ndvi_avg.
    if pixels:
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
        "pixel_data_url": f"decloud://field/{field_id_str}/{date_str}",
        "ndvi_avg": (
            float(quality_inputs.ndvi_mean)
            if quality_inputs is not None
            else (
                index_avgs.get("NDVI")
                if index_avgs.get("NDVI") is not None
                else _avg_triple("NDVI")[0]
            )
        ),
        "ndvi_min": _avg_triple("NDVI")[1],
        "ndvi_max": _avg_triple("NDVI")[2],
        "evi_avg": _avg_triple("EVI")[0],
        "evi_min": _avg_triple("EVI")[1],
        "evi_max": _avg_triple("EVI")[2],
        "ndmi_avg": _avg_triple("NDMI")[0],
        "ndmi_min": _avg_triple("NDMI")[1],
        "ndmi_max": _avg_triple("NDMI")[2],
        "ndre_avg": _avg_triple("NDRE")[0],
        "ndre_min": _avg_triple("NDRE")[1],
        "ndre_max": _avg_triple("NDRE")[2],
        "cire_avg": _avg_triple("CIre")[0],
        "cire_min": _avg_triple("CIre")[1],
        "cire_max": _avg_triple("CIre")[2],
        "mndwi_avg": _avg_triple("MNDWI")[0],
        "mndwi_min": _avg_triple("MNDWI")[1],
        "mndwi_max": _avg_triple("MNDWI")[2],
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
        field_id=field_id_str,
        oss_sensor=decloud_oss_sensor(),
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
    force_read: bool = False,
) -> np.ndarray | None:
    """Return a cached (13,H,W) stack, windowing from HREFs if needed."""
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
    )
    if not force_read:
        cached = read_window_array(str(land_id), iso, "S2")
        if cached and cached.get("stack") is not None:
            return cached["stack"]
    if not hrefs:
        return None
    bands = read_bands_windowed_parallel(hrefs, bounds, target_shape, target_transform)
    stack = stack_s2_13(bands)
    write_window_array(str(land_id), iso, "S2", stack=stack)
    return stack


def _buffer_s2_windows(
    *,
    land_id: str,
    field_geom_geojson: dict,
    date_from: date,
    date_to: date,
    bounds: tuple,
    target_shape: tuple,
    target_transform,
) -> list[dict[str, Any]]:
    """STAC-search the pad range once and window any missing parcel stacks."""
    scenes = _search_s2_l2a_windows(
        field_geom_geojson, date_from, date_to, max_cloud=100.0
    )
    buffered: list[dict[str, Any]] = []
    for sc in scenes:
        stack = _cache_s2_scene(
            land_id,
            sc,
            bounds=bounds,
            target_shape=target_shape,
            target_transform=target_transform,
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
        arr = read_window_array(str(land_id), cached["date"], "S2")
        if arr and arr.get("stack") is not None:
            buffered.append(
                {
                    "id": cached.get("stac_id"),
                    "date": cached["date"],
                    "cloud_cover": cached.get("cloud_cover"),
                    "band_hrefs": cached.get("band_hrefs") or {},
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
    field_geom_geojson: dict,
    dates: list[date],
    bounds: tuple,
    target_shape: tuple,
    target_transform,
) -> dict[date, np.ndarray]:
    """Window nearest S1 VV/VH per S2 date and cache on scratch."""
    out: dict[date, np.ndarray] = {}
    if not dates:
        return out
    need: list[date] = []
    for d in dates:
        cached = read_window_array(str(land_id), d, "S1")
        if cached and cached.get("stack") is not None:
            out[d] = cached["stack"]
        else:
            need.append(d)
    if not need:
        return out
    loaded = _read_s1_for_dates(
        field_geom_geojson, need, bounds, target_shape, target_transform
    )
    for d, arr in zip(need, loaded):
        out[d] = arr
        write_window_array(str(land_id), d, "S1", stack=arr)
        put_window_meta(land_id=str(land_id), date_str=d, sensor="S1", has_array=True)
    logger.info(
        "decloud_s1_buffered",
        land_id=str(land_id),
        dates=len(dates),
        downloaded=len(need),
    )
    return out


def _neighbor_ndvi_from_cache(land_id: str, target: date) -> float | None:
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
        arr = read_window_array(str(land_id), sc["date"], "S2")
        stack = None if arr is None else arr.get("stack")
        if stack is None:
            continue
        # B08=7, B04=3 in the 13-band DN stack (0-10000).
        nir = stack[7].astype("float64")
        red = stack[3].astype("float64")
        denom = nir + red
        ok = denom != 0
        if not ok.any():
            continue
        ndvi = (nir[ok] - red[ok]) / denom[ok]
        finite = ndvi[np.isfinite(ndvi)]
        if finite.size:
            vals.append(float(np.mean(finite)))
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _decloud_one_from_buffer(
    *,
    session,
    agri_meta: dict[str, Any],
    field_id_str: str,
    field_geom_geojson: dict,
    target_transform,
    field_mask: np.ndarray,
    target: date,
    buffered_s2: list[dict[str, Any]],
    s1_by_date: dict[date, np.ndarray],
    raw_scene_id: str | None,
    stac_cloud: float | None,
    parcel_cloud: float | None,
    mq_task_id: str | None,
) -> dict[str, Any]:
    """Run UnCRtainTS on one cloudy date using already-buffered windows."""
    land_id = str(agri_meta["land_id"])
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
            cached = read_window_array(land_id, sc["date"], "S2")
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
    index_arrays = _index_arrays_from_reflectance(rec_01 * 10000.0, None)
    rgb_mean, rgb_raw, rgb_std, rgb_std_raw = _rgb_stats(
        rec_01, s2_stack[-1], field_mask
    )
    ndvi_for_quality = index_arrays.get("NDVI")
    if ndvi_for_quality is not None:
        ndvi_q = np.array(ndvi_for_quality, copy=True)
        ndvi_q[~field_mask] = np.nan
    else:
        ndvi_q = rec_01[7]
    ndvi_stats = compute_zonal_stats(ndvi_q)
    neighbor = _neighbor_ndvi(session, land_id, target)
    if neighbor is None:
        neighbor = _neighbor_ndvi_from_cache(land_id, target)
    quality_inputs = DecloudQualityInputs(
        rgb_mean=rgb_mean,
        rgb_mean_raw=rgb_raw,
        rgb_std=rgb_std,
        rgb_std_raw=rgb_std_raw,
        ndvi_mean=float(ndvi_stats.get("mean") or 0.0),
        neighbor_ndvi_mean=neighbor,
    )
    quality = score_decloud(quality_inputs)
    published = _publish_decloud_product(
        meta=agri_meta,
        date_str=target.isoformat(),
        field_id_str=field_id_str,
        index_arrays=index_arrays,
        transform=target_transform,
        geom4326=field_geom_geojson,
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


def _field_context(session, field_id: str):
    from app.models.tables import Field
    from app.tasks.agri_lonlat import _load_agri_meta

    field = session.get(Field, field_id)
    if field is None or field.geom is None:
        return None
    agri_meta = _load_agri_meta(session, field)
    field_geom = to_shape(field.geom)
    field_geom_geojson = mapping(field_geom)
    target_transform, target_shape, field_mask, bounds = compute_target_grid(
        field_geom.bounds, field_geom
    )
    return {
        "field": field,
        "agri_meta": agri_meta,
        "field_geom_geojson": field_geom_geojson,
        "target_transform": target_transform,
        "target_shape": target_shape,
        "field_mask": field_mask,
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
    field_id: str,
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
    session = get_db_session()
    try:
        ctx = _field_context(session, field_id)
        if ctx is None:
            return {"status": "error", "detail": "field_missing"}
        window_from, window_to = neighbor_window(target)
        buffered = _buffer_s2_windows(
            land_id=str(land_id),
            field_geom_geojson=ctx["field_geom_geojson"],
            date_from=window_from,
            date_to=window_to,
            bounds=ctx["bounds"],
            target_shape=ctx["target_shape"],
            target_transform=ctx["target_transform"],
        )
        s1_by_date: dict[date, np.ndarray] = {}
        if decloud_use_sar():
            s1_by_date = _buffer_s1_for_dates(
                land_id=str(land_id),
                field_geom_geojson=ctx["field_geom_geojson"],
                dates=[sc["date"] for sc in buffered],
                bounds=ctx["bounds"],
                target_shape=ctx["target_shape"],
                target_transform=ctx["target_transform"],
            )
        return _decloud_one_from_buffer(
            session=session,
            agri_meta=ctx["agri_meta"],
            field_id_str=str(ctx["field"].id),
            field_geom_geojson=ctx["field_geom_geojson"],
            target_transform=ctx["target_transform"],
            field_mask=ctx["field_mask"],
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
    field_id: str,
    land_id: str,
    date_from: str,
    date_to: str,
    targets: list[dict[str, Any]] | None = None,
    mq_task_id: str | None = None,
    season_months: list[int] | None = None,
) -> dict[str, Any]:
    """Buffer S2 (+ S1) parcel windows for the job, then decloud cloudy dates.

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
    session = get_db_session()
    try:
        ctx = _field_context(session, field_id)
        if ctx is None:
            return {"status": "error", "detail": "field_missing"}

        buffered = _buffer_s2_windows(
            land_id=str(land_id),
            field_geom_geojson=ctx["field_geom_geojson"],
            date_from=start - timedelta(days=pad),
            date_to=end + timedelta(days=pad),
            bounds=ctx["bounds"],
            target_shape=ctx["target_shape"],
            target_transform=ctx["target_transform"],
        )
        s1_by_date: dict[date, np.ndarray] = {}
        if decloud_use_sar():
            s1_by_date = _buffer_s1_for_dates(
                land_id=str(land_id),
                field_geom_geojson=ctx["field_geom_geojson"],
                dates=[sc["date"] for sc in buffered],
                bounds=ctx["bounds"],
                target_shape=ctx["target_shape"],
                target_transform=ctx["target_transform"],
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
                agri_meta=ctx["agri_meta"],
                field_id_str=str(ctx["field"].id),
                field_geom_geojson=ctx["field_geom_geojson"],
                target_transform=ctx["target_transform"],
                field_mask=ctx["field_mask"],
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
        session.close()
