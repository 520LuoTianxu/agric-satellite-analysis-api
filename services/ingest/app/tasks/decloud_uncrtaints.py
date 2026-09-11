"""Celery task: parcel-window UnCRtainTS decloud after agri optical ingest.

Runs only when ``DECLOUD_ENABLED=1`` and the scene/parcel cloud is above
30% (see ``DECLOUD_CLOUD_MIN_PCT``). Writes an additive lonlat_v1 product
(``scene_id`` suffix ``_decloud``, ``source=uncrtaints_decloud``). Raw S2
rows are never overwritten.

Official drought / timeseries / land metrics must use quality ``good`` only
(see ``app.core.decloud.score_decloud`` and ``is_official_optical_product``).
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
    decloud_cloud_min_pct,
    decloud_enabled,
    decloud_input_t,
    decloud_oss_sensor,
    decloud_scene_id,
    decloud_use_sar,
    score_decloud,
    should_trigger_decloud,
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

S2_LOOKBACK_DAYS = 45
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
    """Enqueue decloud when the flag is on and the scene is cloudy enough."""
    if not decloud_enabled():
        return False
    if not should_trigger_decloud(
        cloud_cover_over_30=cloud_over_30,
        parcel_cloud_cover_pct=parcel_cloud,
        cloud_cover=stac_cloud,
        cloud_min_pct=decloud_cloud_min_pct(),
    ):
        return False
    process_parcel_decloud.delay(
        field_id,
        str(land_id),
        date_str,
        mq_task_id,
        raw_scene_id,
        stac_cloud,
        parcel_cloud,
        cloud_over_30,
    )
    logger.info(
        "decloud_enqueued",
        field_id=field_id,
        land_id=str(land_id),
        date=date_str,
        stac_cloud=stac_cloud,
        parcel_cloud=parcel_cloud,
    )
    return True


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
    if not scenes:
        return []
    target_scene = None
    others: list[dict[str, Any]] = []
    for sc in scenes:
        if sc["date"] == target:
            target_scene = sc
        else:
            others.append(sc)
    if target_scene is None:
        # Closest available date (force / backfill edge).
        target_scene = min(scenes, key=lambda s: abs((s["date"] - target).days))
        others = [s for s in scenes if s is not target_scene]
    others.sort(key=lambda s: abs((s["date"] - target).days))
    picked = others[: max(0, input_t - 1)]
    picked.sort(key=lambda s: s["date"])
    # Keep chronological order with target last (reconstruct that step).
    before = [s for s in picked if s["date"] < target_scene["date"]]
    after = [s for s in picked if s["date"] >= target_scene["date"]]
    ordered = before + [target_scene] + after
    while len(ordered) < input_t:
        ordered.insert(0, ordered[0])
    return ordered[:input_t]


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
    field_mask: np.ndarray,
) -> dict[str, np.ndarray]:
    """Recompute agri optical indices from reconstructed 13-band [0, 1] S2."""
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
            "d0": (target - timedelta(days=S2_LOOKBACK_DAYS)).isoformat(),
            "d1": (target + timedelta(days=S2_LOOKBACK_DAYS)).isoformat(),
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
) -> dict[str, Any] | None:
    from app.tasks.agri_lonlat import publish_optical_lonlat_to_oss_mq
    from app.tasks.bridge_stac_cogs_to_agri_lonlat import (
        _round6,
        _sample_lonlat,
        _stats,
    )

    pixels = _sample_lonlat(geom4326, index_arrays, transform, "EPSG:4326")
    if not pixels:
        logger.info(
            "decloud_no_pixels",
            land_id=meta.get("land_id"),
            date=date_str,
        )
        return None

    def _avg_triple(pix_key: str):
        if pix_key not in index_arrays:
            return None, None, None
        return _stats(index_arrays[pix_key])

    official = quality.is_official
    pixel_data = {
        "format": "lonlat_v1",
        "source": DECLOUD_SOURCE,
        "decloud_quality": quality.quality,
        "decloud_score": quality.score,
        "decloud_reasons": quality.reasons,
        "raw_scene_id": raw_scene_id,
        "pixels": pixels,
    }
    row = {
        "land_id": meta["land_id"],
        "tile_id": meta["tile_id"],
        "date": date_str,
        "scene_id": decloud_scene_id(date_str),
        "land_name": meta["land_name"],
        "cloud_cover": stac_cloud,
        # fair/bad stay excluded from existing cloud>30 drought filters.
        "cloud_cover_over_30": False if official else True,
        "parcel_cloud_cover_pct": (
            0.0
            if official
            else (_round6(parcel_cloud) if parcel_cloud is not None else 100.0)
        ),
        "pixel_count": len(pixels),
        "generated_at_shanghai": datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S%z"
        ),
        "pixel_data_url": f"decloud://field/{field_id_str}/{date_str}",
        "ndvi_avg": _avg_triple("NDVI")[0],
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


@celery_app.task(
    name="app.tasks.decloud_uncrtaints.process_parcel_decloud",
    bind=True,
    max_retries=2,
    time_limit=1800,
    soft_time_limit=1500,
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
    """Reconstruct one cloudy parcel window and publish an additive product."""
    from app.models.tables import Field
    from app.tasks.agri_lonlat import _load_agri_meta

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
        field = session.get(Field, field_id)
        if field is None or field.geom is None:
            return {"status": "error", "detail": "field_missing"}
        agri_meta = _load_agri_meta(session, field)
        field_geom = to_shape(field.geom)
        field_geom_geojson = mapping(field_geom)
        target_transform, target_shape, field_mask, bounds = compute_target_grid(
            field_geom.bounds, field_geom
        )

        window_from = target - timedelta(days=S2_LOOKBACK_DAYS)
        window_to = target + timedelta(days=S2_LOOKBACK_DAYS)
        scenes = _search_s2_l2a_windows(
            field_geom_geojson, window_from, window_to, max_cloud=100.0
        )
        picked = _pick_temporal_scenes(scenes, target, decloud_input_t())
        if not picked:
            logger.info("decloud_no_s2_context", land_id=land_id, date=date_str)
            return {"status": "skipped", "reason": "no_s2_context"}

        s2_list: list[np.ndarray] = []
        for sc in picked:
            bands = read_bands_windowed_parallel(
                sc["band_hrefs"],
                bounds,
                target_shape,
                target_transform,
            )
            s2_list.append(stack_s2_13(bands))
        s2_stack = np.stack(s2_list, axis=0)

        s1_stack = None
        if decloud_use_sar():
            s1_bands = _read_s1_for_dates(
                field_geom_geojson,
                [sc["date"] for sc in picked],
                bounds,
                target_shape,
                target_transform,
            )
            if s1_bands:
                s1_stack = np.stack(s1_bands, axis=0)

        try:
            inferencer = get_inferencer()
            rec_01 = inferencer.reconstruct(
                s2_stack,
                s1_stack,
                [sc["date"].toordinal() for sc in picked],
            )
        except DecloudUnavailable as exc:
            logger.warning(
                "decloud_unavailable",
                land_id=land_id,
                date=date_str,
                error=str(exc),
            )
            return {"status": "skipped", "reason": "unavailable", "detail": str(exc)}

        index_arrays = _index_arrays_from_reflectance(rec_01 * 10000.0, field_mask)
        rgb_mean, rgb_raw, rgb_std, rgb_std_raw = _rgb_stats(
            rec_01, s2_stack[-1], field_mask
        )
        ndvi_stats = compute_zonal_stats(index_arrays.get("NDVI", rec_01[7]))
        neighbor = _neighbor_ndvi(session, land_id, target)
        quality = score_decloud(
            DecloudQualityInputs(
                rgb_mean=rgb_mean,
                rgb_mean_raw=rgb_raw,
                rgb_std=rgb_std,
                rgb_std_raw=rgb_std_raw,
                ndvi_mean=float(ndvi_stats.get("mean") or 0.0),
                neighbor_ndvi_mean=neighbor,
            )
        )
        published = _publish_decloud_product(
            meta=agri_meta,
            date_str=target.isoformat(),
            field_id_str=str(field.id),
            index_arrays=index_arrays,
            transform=target_transform,
            geom4326=field_geom_geojson,
            quality=quality,
            raw_scene_id=raw_scene_id,
            stac_cloud=stac_cloud,
            parcel_cloud=parcel_cloud,
            mq_task_id=mq_task_id,
        )
        logger.info(
            "decloud_published",
            land_id=land_id,
            date=date_str,
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
    except Exception as exc:
        logger.error("decloud_failed", land_id=land_id, date=date_str, error=str(exc))
        retry_num = self.request.retries
        if retry_num < len(RETRY_DELAYS):
            raise self.retry(exc=exc, countdown=RETRY_DELAYS[retry_num])
        raise
    finally:
        session.close()
