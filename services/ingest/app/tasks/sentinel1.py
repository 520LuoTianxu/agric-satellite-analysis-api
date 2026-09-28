"""Sentinel-1 GRD → agric_satellite.parcel_scene_products lonlat_v1 (VV_db/VH_db).

Searches Microsoft Planetary Computer ``sentinel-1-grd`` (VV/VH on Azure Blob,
SAS-signed via ``planetary_computer``), converts amplitude DN using each product's
Sigma0 LUT when available, samples the parcel polygon, and upserts
``sensor='S1'`` lonlat_v1 rows.

Override catalog with ``S1_STAC_API_URL`` if needed. Optical S2 still uses
``STAC_API_URL`` (Element84 by default).

Index ``vv.tif`` / ``vh.tif`` COGs are **not** uploaded for canonical parcels
unless ``WRITE_INDEX_COGS=1``.
"""

from __future__ import annotations

import math
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import rasterio
import structlog
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.control import GroundControlPoint
from rasterio.transform import GCPTransformer, xy
from rasterio.warp import Resampling, reproject, transform as warp_xy, transform_geom
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles
from shapely.geometry import mapping

from app.core.band_parallel import (
    BandReadResult,
    band_max_workers,
    gdal_read_slot,
    run_parallel_band_jobs,
)
from app.core.config import settings, scene_max_workers
from app.core.geo import geojson_to_shape
from app.core.index_cogs import write_index_cogs_enabled
from app.core.job_progress_redis import (
    flush_to_job,
    incr_done,
    mark_scene_progress,
    set_total,
)
from app.core.processing_window import (
    build_complete_processing_window,
    resolve_processing_window_km,
)
from app.core.agri_classify import parse_s1_relative_orbit
from app.core.s1_stac import (
    S1_STAC_COLLECTION,
    open_s1_stac_client,
    s1_access_hint,
    s1_gdal_env,
    s1_open_path,
    s1_stac_api_url,
    stac_asset_href,
)
from app.core.storage import get_storage
from app.tasks.storage_tasks import upload_file_via_storage
from app.tasks.pipeline import (
    RETRY_DELAYS,
    complete_step,
    compute_zonal_stats,
    analysis_crs_for_bounds,
    compute_target_grid,
    describe_target_grid,
    existing_agri_scene_dates,
    filter_scenes_skip_existing,
    get_db_session,
    update_job_progress,
)
from app.worker import celery_app

from agric_satellite_analysis_common.scheduled_land_filter import (
    is_scheduled_land_allowed,
)
from agric_satellite_analysis_common.quality_metrics import PARCEL_VALID_FRACTION_V1
from agric_satellite_analysis_common.date_chunks import split_inclusive_date_range
logger = structlog.get_logger()

STAC_S1_COLLECTION = S1_STAC_COLLECTION
AGRI_S1_ALGORITHM_VERSION = "stac-s1-lonlat-v5"
# Nominal IW GRDH amplitude calibration scale so DN→dB lands near typical σ⁰.
# 仅供缺少定标资产的非标准目录回退；Planetary Computer GRD默认使用逐景Sigma0 LUT。
_S1_DN_CAL = 1000.0
_S1_EPS = 1e-10
_S1_CALIBRATION_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class S1CalibrationLUT:
    """Sentinel-1产品提供的Aσ幅度定标因子，按方位行和距离像元索引。"""

    lines: np.ndarray
    pixels: tuple[np.ndarray, ...]
    sigma_nought: tuple[np.ndarray, ...]


def _xml_child_text(node: ElementTree.Element, name: str) -> str | None:
    """读取不依赖XML命名空间前缀的直接子节点文本。"""
    for child in node:
        if child.tag.rsplit("}", 1)[-1] == name:
            return child.text
    return None


def _parse_lut_vector(text: str) -> np.ndarray:
    """逐项解析定标向量，避免fromstring遇到坏尾项时静默截断。"""
    return np.asarray([float(value) for value in text.split()], dtype=np.float64)


def _parse_s1_sigma0_lut(xml_bytes: bytes) -> S1CalibrationLUT:
    """解析ESA Sigma0 LUT；坏或不完整资产必须失败，不能悄悄套近似常数。"""
    root = ElementTree.fromstring(xml_bytes)
    rows: list[float] = []
    pixel_vectors: list[np.ndarray] = []
    sigma_vectors: list[np.ndarray] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "calibrationVector":
            continue
        line_text = _xml_child_text(node, "line")
        pixel_text = _xml_child_text(node, "pixel")
        sigma_text = _xml_child_text(node, "sigmaNought")
        if not line_text or not pixel_text or not sigma_text:
            continue
        try:
            line = float(line_text)
            pixels = _parse_lut_vector(pixel_text)
            sigma = _parse_lut_vector(sigma_text)
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            not math.isfinite(line)
            or pixels.size < 2
            or pixels.size != sigma.size
            or not np.all(np.isfinite(pixels))
            or not np.all(np.isfinite(sigma))
            or np.any(np.diff(pixels) <= 0)
            or np.any(sigma <= 0)
        ):
            continue
        rows.append(line)
        pixel_vectors.append(pixels)
        sigma_vectors.append(sigma)
    if len(rows) < 2:
        raise ValueError("Sentinel-1 calibration asset has fewer than two valid Sigma0 vectors")

    order = np.argsort(np.asarray(rows, dtype=np.float64))
    lines = np.asarray(rows, dtype=np.float64)[order]
    if np.any(np.diff(lines) <= 0):
        raise ValueError("Sentinel-1 calibration lines are not strictly increasing")
    return S1CalibrationLUT(
        lines=lines,
        pixels=tuple(pixel_vectors[int(index)] for index in order),
        sigma_nought=tuple(sigma_vectors[int(index)] for index in order),
    )


@lru_cache(maxsize=64)
def _load_s1_sigma0_lut(calibration_href: str) -> S1CalibrationLUT:
    """有界下载并缓存每个VV/VH定标XML，减小同一下载机重复读取开销。"""
    chunks: list[bytes] = []
    received = 0
    with httpx.stream(
        "GET",
        calibration_href,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    ) as response:
        response.raise_for_status()
        declared_size = response.headers.get("content-length")
        if declared_size and int(declared_size) > _S1_CALIBRATION_MAX_BYTES:
            raise ValueError("Sentinel-1 calibration asset exceeds the 2 MiB limit")
        for chunk in response.iter_bytes():
            received += len(chunk)
            if received > _S1_CALIBRATION_MAX_BYTES:
                raise ValueError("Sentinel-1 calibration asset exceeds the 2 MiB limit")
            chunks.append(chunk)
    return _parse_s1_sigma0_lut(b"".join(chunks))


def _sigma0_calibration_factor_at_pixels(
    lut: S1CalibrationLUT, source_rows: np.ndarray, source_cols: np.ndarray
) -> np.ndarray:
    """在原始源像元坐标插值ESA给出的Sigma0幅度定标因子Aσ。"""
    rows, cols = np.broadcast_arrays(
        np.asarray(source_rows, dtype=np.float64),
        np.asarray(source_cols, dtype=np.float64),
    )
    flat_rows = rows.ravel()
    flat_cols = cols.ravel()
    upper = np.searchsorted(lut.lines, flat_rows, side="right")
    lower = np.clip(upper - 1, 0, len(lut.lines) - 1)
    upper = np.clip(upper, 0, len(lut.lines) - 1)
    row_span = lut.lines[upper] - lut.lines[lower]
    row_weight = np.zeros(flat_rows.shape, dtype=np.float64)
    different = row_span > 0
    row_weight[different] = (
        (flat_rows[different] - lut.lines[lower[different]])
        / row_span[different]
    )
    row_weight = np.clip(row_weight, 0.0, 1.0)

    result = np.full(flat_rows.shape, np.nan, dtype=np.float64)
    in_line_range = (flat_rows >= lut.lines[0]) & (flat_rows <= lut.lines[-1])
    pair_ids = lower * len(lut.lines) + upper
    for pair_id in np.unique(pair_ids):
        selected = np.flatnonzero(pair_ids == pair_id)
        lo = int(pair_id) // len(lut.lines)
        hi = int(pair_id) % len(lut.lines)
        lo_values = np.interp(
            flat_cols[selected],
            lut.pixels[lo],
            lut.sigma_nought[lo],
            left=np.nan,
            right=np.nan,
        )
        if lo == hi:
            result[selected] = lo_values
            continue
        hi_values = np.interp(
            flat_cols[selected],
            lut.pixels[hi],
            lut.sigma_nought[hi],
            left=np.nan,
            right=np.nan,
        )
        result[selected] = lo_values + (
            hi_values - lo_values
        ) * row_weight[selected]
    result[~in_line_range | ~np.isfinite(flat_rows) | ~np.isfinite(flat_cols)] = np.nan
    return result.reshape(rows.shape)


def _source_window_for_target_grid(
    src,
    target_shape: tuple[int, int],
    target_transform,
    target_crs: str,
    source_gcps: list,
    gcp_crs,
):
    """反算目标网格范围对应的原始COG窗口，避免整景读取。"""
    height, width = target_shape
    edge = np.linspace(0.0, 1.0, num=33, dtype=np.float64)
    edge_cols = np.concatenate(
        (
            edge * width,
            edge * width,
            np.zeros_like(edge) * width,
            np.ones_like(edge) * width,
        )
    )
    edge_rows = np.concatenate(
        (
            np.zeros_like(edge) * height,
            np.ones_like(edge) * height,
            edge * height,
            edge * height,
        )
    )
    target_x = (
        target_transform.c
        + edge_cols * target_transform.a
        + edge_rows * target_transform.b
    )
    target_y = (
        target_transform.f
        + edge_cols * target_transform.d
        + edge_rows * target_transform.e
    )

    use_gcps = bool(source_gcps and gcp_crs is not None)
    source_crs = gcp_crs if use_gcps else src.crs
    if source_crs is None:
        raise ValueError("Sentinel-1 source raster has no usable georeferencing")
    target_to_source = Transformer.from_crs(
        target_crs, source_crs, always_xy=True
    )
    source_x, source_y = target_to_source.transform(target_x, target_y)
    if use_gcps:
        inverse_gcps = GCPTransformer(source_gcps)
        try:
            rows, cols = inverse_gcps.rowcol(
                np.asarray(source_x),
                np.asarray(source_y),
                op=lambda value: value,
            )
        finally:
            inverse_gcps.close()
    else:
        cols, rows = (~src.transform) * (np.asarray(source_x), np.asarray(source_y))

    rows = np.asarray(rows, dtype=np.float64)
    cols = np.asarray(cols, dtype=np.float64)
    finite = np.isfinite(rows) & np.isfinite(cols)
    if not np.any(finite):
        return None

    # 边界采样加8个源像元余量，覆盖GCP曲率和双线性重采样所需邻域。
    padding = 8
    row_start = max(0, math.floor(float(np.min(rows[finite]))) - padding)
    row_stop = min(src.height, math.ceil(float(np.max(rows[finite]))) + padding + 1)
    col_start = max(0, math.floor(float(np.min(cols[finite]))) - padding)
    col_stop = min(src.width, math.ceil(float(np.max(cols[finite]))) + padding + 1)
    if row_stop <= row_start or col_stop <= col_start:
        return None
    return rasterio.windows.Window(
        col_start, row_start, col_stop - col_start, row_stop - row_start
    )


def _shift_gcps_for_window(source_gcps: list, window) -> list[GroundControlPoint]:
    """把全景GCP坐标平移到裁剪窗口的局部像素原点。"""
    row_offset = int(window.row_off)
    col_offset = int(window.col_off)
    return [
        GroundControlPoint(
            row=float(gcp.row) - row_offset,
            col=float(gcp.col) - col_offset,
            x=gcp.x,
            y=gcp.y,
            z=gcp.z,
            id=gcp.id,
            info=gcp.info,
        )
        for gcp in source_gcps
    ]


def _calibrate_source_window_sigma0(
    amplitude: np.ndarray,
    lut: S1CalibrationLUT,
    row_offset: int,
    col_offset: int,
) -> np.ndarray:
    """先在COG原生像元上应用Aσ LUT，输出可安全重采样的线性功率。"""
    height, width = amplitude.shape
    source_cols = np.arange(width, dtype=np.float64)[None, :] + col_offset + 0.5
    source_power = np.full(amplitude.shape, np.nan, dtype=np.float32)
    # LUT插值和浮点运算按128行分块，避免大窗口同时复制多份全尺寸float64数组。
    for start in range(0, height, 128):
        stop = min(start + 128, height)
        source_rows = (
            np.arange(start, stop, dtype=np.float64)[:, None] + row_offset + 0.5
        )
        sigma0_factor = _sigma0_calibration_factor_at_pixels(
            lut, source_rows, source_cols
        )
        amplitude_block = amplitude[start:stop].astype(np.float64, copy=False)
        valid = (
            np.isfinite(amplitude_block)
            & (amplitude_block > 0)
            & np.isfinite(sigma0_factor)
            & (sigma0_factor > 0)
        )
        power_block = source_power[start:stop]
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            power_block[valid] = np.square(
                amplitude_block[valid] / sigma0_factor[valid]
            ).astype(np.float32)
    return source_power

UPSERT_S1_SQL = """
INSERT INTO agric_satellite.parcel_scene_products (
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
  json_oss_key = COALESCE(EXCLUDED.json_oss_key, agric_satellite.parcel_scene_products.json_oss_key),
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
    """按项目现有幅度DN标定近似换算σ⁰分贝，非完整地形校正流程。"""
    amp = dn.astype(np.float32)
    amp[amp <= 0] = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        power = (amp / _S1_DN_CAL) ** 2
        db = 10.0 * np.log10(power + _S1_EPS)
    db[~np.isfinite(db)] = np.nan
    return db


def search_s1_scenes(
    land_geom_geojson: dict, date_from: date, date_to: date, *, dedupe_week: bool = True
) -> list[dict]:
    """默认每周优选IW双极化景；聚合窗口保留全部景以覆盖不同轨道的地块。"""
    t0 = time.perf_counter()
    catalog = open_s1_stac_client()
    search = catalog.search(
        collections=[STAC_S1_COLLECTION],
        intersects=land_geom_geojson,
        datetime=f"{date_from.isoformat()}/{date_to.isoformat()}",
        max_items=200,
    )
    items = list(search.items())
    skipped_no_vvvh = 0
    skipped_incomplete_calibration = 0
    logger.info(
        "s1_stac_search_results",
        count=len(items),
        date_from=str(date_from),
        date_to=str(date_to),
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        stac_api=s1_stac_api_url(),
        provider="planetary_computer",
    )
    if not items:
        return []

    weekly: dict[str, Any] = {}
    for item in items:
        assets = item.assets or {}
        vv = assets.get("vv") or assets.get("VV")
        vh = assets.get("vh") or assets.get("VH")
        vv_href = stac_asset_href(vv)
        vh_href = stac_asset_href(vh)
        if not vv_href or not vh_href:
            skipped_no_vvvh += 1
            logger.info(
                "s1_scene_skipped",
                reason="missing_vv_vh_assets",
                scene_id=item.id,
                asset_keys=sorted(assets.keys()),
            )
            continue
        vv_calibration_href = stac_asset_href(
            assets.get("schema-calibration-vv")
        )
        vh_calibration_href = stac_asset_href(
            assets.get("schema-calibration-vh")
        )
        # VV/VH必须使用同一辐射口径；只提供单通道LUT的目录项不能入选，
        # 先跳过候选以便同周仍可选其他完整场景，避免处理阶段整景失败。
        if bool(vv_calibration_href) != bool(vh_calibration_href):
            skipped_incomplete_calibration += 1
            logger.info(
                "s1_scene_skipped",
                reason="incomplete_sigma_nought_lut_pair",
                scene_id=item.id,
                has_vv_calibration=bool(vv_calibration_href),
                has_vh_calibration=bool(vh_calibration_href),
            )
            continue
        item_date = item.datetime.date() if item.datetime else date_from
        week_key = item_date.isocalendar()[:2]
        week_str = f"{week_key[0]}-W{week_key[1]:02d}" if dedupe_week else item.id
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
        # 同周候选中优先选有VV/VH定标资产的产品，避免不必要地回退到固定幅度比例。
        if vv_calibration_href and vh_calibration_href:
            score += 1
        entry = weekly.get(week_str)
        if entry is None or score > entry["score"]:
            rel_orbit = props.get("sat:relative_orbit") or props.get("relative_orbit")
            weekly[week_str] = {
                "item": item,
                "date": item_date,
                "score": score,
                "vv_href": vv_href,
                "vh_href": vh_href,
                "vv_calibration_href": vv_calibration_href,
                "vh_calibration_href": vh_calibration_href,
                "relative_orbit": rel_orbit,
            }
    if skipped_no_vvvh:
        logger.info(
            "s1_stac_skipped_no_vvvh",
            skipped=skipped_no_vvvh,
            kept_weeks=len(weekly),
        )
    if skipped_incomplete_calibration:
        logger.info(
            "s1_stac_skipped_incomplete_calibration",
            skipped=skipped_incomplete_calibration,
            kept_weeks=len(weekly),
        )

    scenes = []
    for week_str in sorted(weekly.keys()):
        e = weekly[week_str]
        scenes.append(
            {
                "id": e["item"].id,
                "date": e["date"],
                "vv_href": e["vv_href"],
                "vh_href": e["vh_href"],
                "vv_calibration_href": e.get("vv_calibration_href"),
                "vh_calibration_href": e.get("vh_calibration_href"),
                "relative_orbit": e.get("relative_orbit"),
                "geometry": e["item"].geometry,
            }
        )
    return scenes


def _read_band_windowed_db_profiled(
    href: str,
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    target_crs: str = "EPSG:4326",
    calibration_href: str | None = None,
) -> BandReadResult[np.ndarray]:
    """读取 GRD COG 窗口，并拆分远端 I/O 与目标网格重投影耗时。

    S1 GRD常以GCP而非常规仿射CRS定位；先反算原生像元窗口，避免整景下载和先重采样DN。
    """
    t_io = time.perf_counter()
    calibration_lut = (
        _load_s1_sigma0_lut(calibration_href) if calibration_href else None
    )
    s3_path = s1_open_path(href)
    dst = np.full(target_shape, np.nan, dtype=np.float32)
    with gdal_read_slot(), rasterio.Env(**s1_gdal_env()):
        with rasterio.open(s3_path) as src:
            source_gcps, gcp_crs = src.gcps
            window = _source_window_for_target_grid(
                src,
                target_shape,
                target_transform,
                target_crs,
                source_gcps,
                gcp_crs,
            )
            if window is None:
                logger.info(
                    "s1_scene_skipped",
                    reason="empty_source_window",
                    href=href[:160],
                    bounds=list(bounds),
                )
                io_ms = int((time.perf_counter() - t_io) * 1000)
                return BandReadResult(_dn_to_db(dst), io_ms=io_ms, reproject_ms=0)

            # 直接从云优化GeoTIFF读取地块覆盖的原始像元，并保留源NoData掩膜。
            data = src.read(1, window=window, masked=True)
            data = np.asarray(data.astype(np.float32).filled(np.nan))
            if source_gcps and gcp_crs is not None:
                source_gcps = _shift_gcps_for_window(source_gcps, window)
                source_crs = gcp_crs
                source_georef = {"gcps": source_gcps}
            else:
                source_crs = src.crs
                source_transform = rasterio.windows.transform(window, src.transform)
                source_georef = {"src_transform": source_transform}
        io_ms = int((time.perf_counter() - t_io) * 1000)
        t_reproject = time.perf_counter()
        if calibration_lut is not None:
            # ESA要求逐源像元先由DN²/Aσ²得到Sigma0，再重采样功率，最后转dB。
            # 这样不会先平均原始幅度再平方，避免双重插值改变地块回散射统计。
            source_values = _calibrate_source_window_sigma0(
                data,
                calibration_lut,
                int(window.row_off),
                int(window.col_off),
            )
            destination_power = np.full(target_shape, np.nan, dtype=np.float32)
            reproject(
                source=source_values,
                destination=destination_power,
                src_crs=source_crs,
                **source_georef,
                dst_transform=target_transform,
                dst_crs=target_crs,
                src_nodata=np.nan,
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
            value = np.full(target_shape, np.nan, dtype=np.float32)
            valid = np.isfinite(destination_power) & (destination_power > 0)
            value[valid] = 10.0 * np.log10(destination_power[valid])
        else:
            # 兼容不提供校准XML的目录；产品元数据会明确标出该近似回退口径。
            reproject(
                source=data,
                destination=dst,
                src_crs=source_crs,
                **source_georef,
                dst_transform=target_transform,
                dst_crs=target_crs,
                src_nodata=np.nan,
                dst_nodata=np.nan,
                resampling=Resampling.bilinear,
            )
            value = _dn_to_db(dst)
        reproject_ms = int((time.perf_counter() - t_reproject) * 1000)
    return BandReadResult(value, io_ms=io_ms, reproject_ms=reproject_ms)


def _read_band_windowed_db(
    href: str, bounds: tuple, target_shape: tuple, target_transform
) -> np.ndarray:
    """兼容旧调用方，返回窗口化后的 Sentinel-1 dB 数组。"""
    return _read_band_windowed_db_profiled(
        href, bounds, target_shape, target_transform
    ).value


def _s1_radiometric_calibration(scene: dict[str, Any]) -> dict[str, Any]:
    """生成随产品返回的S1定标口径，并拒绝只校准一个极化通道的混合产品。"""
    vv_lut = scene.get("vv_calibration_href")
    vh_lut = scene.get("vh_calibration_href")
    if bool(vv_lut) != bool(vh_lut):
        raise ValueError("Sentinel-1 VV/VH calibration assets must be provided together")
    if vv_lut and vh_lut:
        method = "esa_sigma_nought_lut"
        scale = None
    else:
        method = "fixed_amplitude_scale_approximation"
        scale = _S1_DN_CAL
    return {
        "method": method,
        "coefficient": "sigma0",
        "units": "dB",
        "polarizations": {"VV": method, "VH": method},
        "fallback_scale": scale,
        # 记录本代码路径做过的操作；不推断STAC数据提供方是否已做上游噪声处理。
        "thermal_noise_correction": "not_performed_by_this_pipeline",
    }


def _write_index_cog(
    data: np.ndarray,
    transform,
    target_crs: str,
    org_id: str,
    land_id: str,
    scene_date: date,
    stem: str,
) -> str:
    """Write float32 COG to active storage; return storage URI."""
    object_key = f"cogs/{org_id}/{land_id}/{scene_date.isoformat()}/{stem}.tif"
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
            "crs": target_crs,
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
    geom4326: dict,
    vv: np.ndarray,
    vh: np.ndarray,
    transform,
    target_crs: str = "EPSG:4326",
) -> list[dict[str, Any]]:
    """按地块掩膜采样VV/VH并记录像元中心经纬度；VH缺测不丢弃有效VV。"""
    h, w = vv.shape
    # 多边形先投影到分析网格坐标系，像元中心再反投影为lonlat_v1坐标。
    geom_target = (
        geom4326
        if target_crs.upper() in {"EPSG:4326", "OGC:CRS84"}
        else transform_geom("EPSG:4326", target_crs, geom4326)
    )
    inside = ~geometry_mask(
        [geom_target],
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
    if target_crs.upper() not in {"EPSG:4326", "OGC:CRS84"}:
        lons, lats = warp_xy(target_crs, "EPSG:4326", xs.tolist(), ys.tolist())
        xs = np.asarray(lons, dtype=np.float64)
        ys = np.asarray(lats, dtype=np.float64)

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


def _resolve_land_meta(
    session,
    land_id: str,
    remote: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read metadata from the canonical parcel row without identity translation."""
    if remote is not None:
        if str(remote.get("land_id") or "") != str(land_id):
            raise RuntimeError("internal land response does not match requested land_id")
        return {
            "land_id": str(land_id),
            "tile_id": remote.get("tile_id"),
            "land_name": remote.get("land_name") or str(land_id),
        }

    if session is None:
        return None
    from app.models.tables import LandParcel

    land = session.get(LandParcel, land_id)
    if not land or land.deleted_at is not None:
        return None
    return {
        "land_id": land.land_id,
        "tile_id": land.tile_id,
        "land_name": land.land_name or land.land_id,
    }


def _upsert_agri_s1(
    session,
    meta: dict,
    scene_date: date,
    scene_id: str,
    land_id: str,
    pixels: list,
    vv_stats: dict,
    vh_stats: dict,
    mq_task_id: str | None = None,
    relative_orbit: int | None = None,
    stac_item_id: str | None = None,
    analysis_grid: dict[str, Any] | None = None,
    processing_window_km: float | None = None,
    processing_window_bounds: tuple[float, float, float, float] | None = None,
    result_delivery: str = "mq",
    radiometric_calibration: dict[str, Any] | None = None,
) -> str | None:
    """上传S1经纬度像元并经选定通道发布，同时保留来源与网格元数据。

    ``result_delivery='http'`` is used by the daily satellite batch: only a
    small OSS callback is sent to the API, which queues it in Redis before the
    API-side PostgreSQL upsert. Legacy callers keep the MQ path by default.

    ``scene_id`` 为兼容旧流程保留；原始 STAC item ID 单独存入产品元数据，
    ``analysis_grid`` 记录输出网格而非声称传感器原生分辨率。

    Returns public JSON URL when upload+delivery succeed.
    """
    if result_delivery not in {"mq", "http"}:
        raise ValueError(f"unsupported S1 result delivery: {result_delivery}")
    if result_delivery == "mq":
        # 下载机关闭本地 PG 写入时，历史调用方也必须切到 API HTTP/Redis，不能再发结果 MQ。
        from app.core.http_mode import ingest_http_only

        if ingest_http_only():
            result_delivery = "http"
    date_str = scene_date.isoformat()
    rel = parse_s1_relative_orbit(scene_id, relative_orbit)
    pixel_data = {
        "format": "lonlat_v1",
        "source": "stac_s1_direct",
        "algorithm_version": AGRI_S1_ALGORITHM_VERSION,
        "pixels": pixels,
    }
    # S1质量分同样是地块掩膜内有效像元比例，随产品写入口径标识便于后续解释。
    pixel_data["quality_metrics"] = {
        "VV": {
            "valid_fraction": vv_stats.get("quality_score"),
            "method": PARCEL_VALID_FRACTION_V1,
        },
        "VH": {
            "valid_fraction": vh_stats.get("quality_score"),
            "method": PARCEL_VALID_FRACTION_V1,
        },
    }
    if radiometric_calibration is not None:
        pixel_data["radiometric_calibration"] = radiometric_calibration
    if stac_item_id:
        # scene_id 保留兼容后缀；原始 STAC item ID 独立持久化，便于追踪轨道与资产。
        pixel_data["stac_item_id"] = str(stac_item_id)
    if analysis_grid is not None:
        pixel_data["analysis_grid"] = analysis_grid
    if processing_window_km is not None:
        # 与光学产品保持一致，记录 S1 实际检索/读取的周边矩形范围。
        pixel_data["processing_window"] = {
            "shape": "square",
            "side_km": processing_window_km,
            "bounds": list(processing_window_bounds or ()),
        }
    if rel is not None:
        pixel_data["relative_orbit"] = rel
    json_oss_key = None
    json_url = None
    json_upload_ms = 0
    from agric_satellite_analysis_common.mq_results import (
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
        "pixel_data_url": f"stac-s1://land/{land_id}/{date_str}",
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
        raise RuntimeError("S1 agri path requires OSS scene JSON upload before delivery")

    label = f"{date_str}_S1"
    parent = (mq_task_id or "").strip() or None
    extras = {
        "kind": "parcel_scene_product",
        "land_id": str(meta["land_id"]),
        "sensor": "S1",
        "date": date_str,
        "scene_id": scene_id,
        "parent_mq_task_id": parent,
        "json_oss_key": json_oss_key,
    }
    if result_delivery == "http":
        # HTTP 只传 OSS 地址，API 侧从 Redis 队列异步取出并入库，避免并发下载结果堵塞 API。
        from app.core.http_mode import cache_scene_result_http

        t_callback = time.perf_counter()
        cache_scene_result_http(label=label, json_url=json_url, extras=extras)
        callback_ms = int((time.perf_counter() - t_callback) * 1000)
        logger.info(
            "lonlat_write_timing",
            land_id=meta.get("land_id"),
            date=date_str,
            sensor="S1",
            json_upload_ms=json_upload_ms,
            db_upsert_ms=0,
            callback_ms=callback_ms,
            mq_publish_ms=0,
            uploaded_json=True,
            path="oss_http_redis",
        )
        logger.info(
            "lonlat_oss_http_cached",
            land_id=meta.get("land_id"),
            date=date_str,
            sensor="S1",
            json_url=json_url,
            label=label,
        )
        return json_url

    from agric_satellite_analysis_common.mq_results import publish_task_result

    result_task_id = (
        f"{parent}:{label}" if parent else f"agri-scene:{meta['land_id']}:{label}"
    )
    t_mq = time.perf_counter()
    publish_task_result(
        task_id=result_task_id,
        status="success",
        land_id=str(meta["land_id"]),
        oss_urls={label: json_url},
        collect_parcel_urls=False,
        upload_summary_if_empty=False,
        extras=extras,
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


def _maybe_s1_progress(
    job_id: str,
    step: str,
    *,
    scene: int | None = None,
    total_scenes: int | None = None,
    scene_id: str | None = None,
) -> None:
    """Redis hot-path progress; never touches Postgres from scene workers."""
    try:
        mark_scene_progress(
            job_id,
            step,
            scene=scene,
            total_scenes=total_scenes,
            scene_id=scene_id,
        )
    except Exception as e:
        logger.warning(
            "s1_job_progress_failed",
            step=step,
            error=str(e),
        )


def _process_one_s1_scene(
    *,
    job_id: str,
    scene: dict,
    idx: int,
    total_scenes: int,
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    target_crs: str,
    field_mask: np.ndarray,
    org_id_str: str,
    land_id_str: str,
    land_id,
    date_from: date,
    date_to: date,
    agri_meta: dict | None,
    land_geom_geojson: dict,
    processing_window_km: float | None = None,
    scene_workers: int = 1,
    mq_task_id: str | None = None,
) -> bool:
    """Download S1 bands, optionally write COGs, publish agri lonlat OSS+MQ.

    Returns True only when a product was published (OSS+MQ) or classic COGs
    were written. Empty samples / missing agri meta / Job-only races do not
    count as processed.

    入库统计和像元采样都使用先应用地块掩膜后的线性数组，避免邻近地块像元污染结果。
    """
    from app.models.tables import RasterLayer
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.core.http_mode import ingest_http_only

    http_only = ingest_http_only()
    session = None if http_only else get_db_session()
    scene_id = scene.get("id")
    published = False
    try:
        _maybe_s1_progress(
            job_id,
            "download_bands",
            scene=idx + 1,
            total_scenes=total_scenes,
            scene_id=scene_id,
        )
        t_scene = time.perf_counter()
        t0 = time.perf_counter()
        radiometric_calibration = _s1_radiometric_calibration(scene)
        try:
            pol = run_parallel_band_jobs(
                {"vv": scene["vv_href"], "vh": scene["vh_href"]},
                lambda key, href: _read_band_windowed_db_profiled(
                    href,
                    bounds,
                    target_shape,
                    target_transform,
                    target_crs,
                    calibration_href=scene.get(f"{key}_calibration_href"),
                ),
                scene_workers=scene_workers,
                log_context={
                    "job_id": job_id,
                    "scene_id": scene_id,
                    "date": scene["date"].isoformat(),
                    "sensor": "S1",
                },
            )
        except Exception as e:
            err = str(e)
            extra: dict[str, Any] = {}
            low = err.lower()
            if (
                "403" in err
                or "404" in err
                or "access denied" in low
                or "forbidden" in low
                or "not found" in low
            ):
                extra["hint"] = s1_access_hint()
                extra["stac_api"] = s1_stac_api_url()
            logger.error(
                "s1_scene_skipped",
                reason="band_read_failed",
                scene_id=scene_id,
                error=err,
                **extra,
            )
            incr_done(job_id, failed=True)
            return False
        vv = pol["vv"]
        vh = pol["vh"]
        vv[~field_mask] = np.nan
        vh[~field_mask] = np.nan
        download_ms = int((time.perf_counter() - t0) * 1000)

        write_cogs = write_index_cogs_enabled()
        t0 = time.perf_counter()
        vv_stats = compute_zonal_stats(vv, expected_mask=field_mask)
        vh_stats = compute_zonal_stats(vh, expected_mask=field_mask)
        stats_ms = int((time.perf_counter() - t0) * 1000)

        write_cog_ms = 0
        if write_cogs and session is not None:
            _maybe_s1_progress(
                job_id,
                "write_cog",
                scene=idx + 1,
                total_scenes=total_scenes,
                scene_id=scene_id,
            )
            t0 = time.perf_counter()
            vv_uri = _write_index_cog(
                vv, target_transform, target_crs, org_id_str, land_id_str, scene["date"], "vv"
            )
            vh_uri = _write_index_cog(
                vh, target_transform, target_crs, org_id_str, land_id_str, scene["date"], "vh"
            )
            write_cog_ms = int((time.perf_counter() - t0) * 1000)
            logger.info(
                "cog_uploaded",
                object_key=f"cogs/{org_id_str}/{land_id_str}/{scene['date'].isoformat()}/vv.tif",
                index="s1",
            )

            for label, uri, stats in (
                ("VV", vv_uri, vv_stats),
                ("VH", vh_uri, vh_stats),
            ):
                layer_values = dict(
                    land_id=land_id,
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
                        "pipeline_version": "s1-4.0.0",
                        "algorithm_version": AGRI_S1_ALGORITHM_VERSION,
                        "quality_score_method": PARCEL_VALID_FRACTION_V1,
                        "radiometric_calibration": radiometric_calibration,
                    },
                )
                stmt = (
                    pg_insert(RasterLayer)
                    .values(**layer_values)
                    .on_conflict_do_update(
                        constraint="uq_raster_land_date_type",
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
            published = True
        elif write_cogs and session is None:
            logger.info(
                "cog_upload_skipped",
                object_key=f"cogs/{org_id_str}/{land_id_str}/{scene['date'].isoformat()}/vv.tif",
                index="s1",
                reason="http_only_no_pg",
            )
        else:
            logger.info(
                "cog_upload_skipped",
                object_key=f"cogs/{org_id_str}/{land_id_str}/{scene['date'].isoformat()}/vv.tif",
                index="s1",
            )

        write_lonlat_ms = 0
        pixels_n = 0
        if agri_meta is None:
            if not published:
                logger.info(
                    "s1_scene_skipped",
                    reason="agri_meta_missing",
                    scene_id=scene_id,
                    date=str(scene.get("date")),
                    land_id=land_id_str,
                )
        else:
            t0 = time.perf_counter()
            pixels = _sample_s1_lonlat(
                land_geom_geojson, vv, vh, target_transform, target_crs
            )
            if not pixels:
                logger.info(
                    "s1_scene_skipped",
                    reason="empty_pixels",
                    scene_id=scene_id,
                    date=str(scene.get("date")),
                    land_id=agri_meta.get("land_id"),
                    finite_vv=int(np.isfinite(vv).sum()),
                )
            else:
                pixels_n = len(pixels)
                json_url = _upsert_agri_s1(
                    session,
                    agri_meta,
                    scene["date"],
                    f"{scene['id']}_stac",
                    land_id_str,
                    pixels,
                    vv_stats,
                    vh_stats,
                    mq_task_id=mq_task_id,
                    relative_orbit=scene.get("relative_orbit"),
                    stac_item_id=str(scene_id) if scene_id else None,
                    analysis_grid=describe_target_grid(
                        target_transform, target_shape, target_crs
                    ),
                    processing_window_km=processing_window_km,
                    processing_window_bounds=bounds,
                    radiometric_calibration=radiometric_calibration,
                )
                published = True
                logger.info(
                    "lonlat_upserted",
                    land_id=agri_meta.get("land_id"),
                    date=str(scene["date"]),
                    sensor="S1",
                    pixels=len(pixels),
                    json_url=json_url,
                )
            write_lonlat_ms = int((time.perf_counter() - t0) * 1000)
        incr_done(job_id, failed=False)
        logger.info(
            "scene_timing",
            sensor="S1",
            job_id=job_id,
            scene_id=scene_id,
            date=str(scene.get("date")),
            download_ms=download_ms,
            stats_ms=stats_ms,
            write_cog_ms=write_cog_ms,
            write_lonlat_ms=write_lonlat_ms,
            total_ms=int((time.perf_counter() - t_scene) * 1000),
            pixels=pixels_n,
            published=published,
        )
        return published
    except Exception as e:
        logger.error(
            "s1_scene_failed",
            scene_id=scene_id,
            error=str(e),
            stac_api=s1_stac_api_url(),
        )
        incr_done(job_id, failed=True)
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        if session is not None:
            session.close()


def _process_s1_scenes_parallel(
    *,
    job_id: str,
    scenes: list[dict],
    bounds: tuple,
    target_shape: tuple,
    target_transform,
    target_crs: str,
    field_mask: np.ndarray,
    org_id_str: str,
    land_id_str: str,
    land_id,
    date_from: date,
    date_to: date,
    agri_meta: dict | None,
    land_geom_geojson: dict,
    processing_window_km: float | None = None,
    mq_task_id: str | None = None,
    on_chunk=None,
) -> int:
    """Process S1 scenes concurrently. Returns the processed count.

    ``on_chunk(completed_n, processed)`` is invoked every ``workers`` completions
    so the parent can flush Redis progress into Postgres.

    每个线程独立打开数据库会话；波段读取与场景级并发受worker上限控制，避免共享Session跨线程使用。
    """
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
    completed_n = 0
    flush_every = max(1, workers)
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
                target_crs=target_crs,
                field_mask=field_mask,
                org_id_str=org_id_str,
                land_id_str=land_id_str,
                land_id=land_id,
                date_from=date_from,
                date_to=date_to,
                agri_meta=agri_meta,
                land_geom_geojson=land_geom_geojson,
                processing_window_km=processing_window_km,
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
                completed_n += 1
                if on_chunk and completed_n % flush_every == 0:
                    on_chunk(completed_n, processed)
                continue
            completed_n += 1
            if ok:
                processed += 1
            if on_chunk and completed_n % flush_every == 0:
                on_chunk(completed_n, processed)

    logger.info(
        "scene_parallel_done",
        job_id=job_id,
        index="s1",
        layers_created=processed,
        total_scenes=total,
        workers=workers,
        wall_ms=int((time.perf_counter() - t_process) * 1000),
    )
    return processed



def _process_s1_http_only(
    *,
    job_id: str | None,
    land_id: str | None,
    date_from: str | None,
    date_to: str | None,
    force: bool,
    mq_task_id: str | None,
    processing_window_km: float | None,
) -> dict:
    """下载机无SyncSession的S1分块流程，经Internal HTTP取地块/任务并提交结果。"""
    from shapely.geometry import shape as shapely_shape

    from app.core.http_mode import (
        land_geom_http,
        get_job_http,
        patch_job_http,
        resolve_land_http,
    )
    from app.tasks.pipeline import existing_agri_scene_dates, filter_scenes_skip_existing

    params: dict[str, Any] = {}
    if job_id and (not land_id or not date_from or not date_to):
        remote = get_job_http(job_id) or {}
        params = dict(remote.get("params_json") or {})
        land_id = land_id or (
            str(remote["land_id"]) if remote.get("land_id") else None
        )
        date_from = date_from or params.get("date_from")
        date_to = date_to or params.get("date_to")
        force = bool(force or params.get("force"))
        if mq_task_id is None and params.get("mq_task_id"):
            mq_task_id = str(params["mq_task_id"])
        processing_window_km = processing_window_km or params.get("processing_window_km")

    if not land_id or not date_from or not date_to:
        return {
            "job_id": job_id,
            "land_id": land_id,
            "status": "error",
            "detail": "land_id/date_from/date_to required",
            "http_only": True,
        }

    resolved = resolve_land_http(land_id)
    if not is_scheduled_land_allowed(
        resolved.get("base_id"), resolved.get("land_area_mu")
    ):
        patch_job_http(
            job_id,
            {
                "status": "cancelled",
                "error": "定时任务地块过滤：基地被排除或地块面积超过5000亩",
                "progress_json": {
                    "current_step": "filtered",
                    "message": "地块不参与自动化遥感任务",
                },
            },
        )
        return {
            "job_id": job_id,
            "land_id": str(land_id),
            "status": "cancelled",
            "reason": "land_filtered",
            "http_only": True,
        }
    geom_payload = land_geom_http(land_id, include_geojson=True)
    geojson = geom_payload.get("geojson")
    if not geojson:
        return {
            "job_id": job_id,
            "land_id": land_id,
            "status": "failed",
            "detail": "Land parcel geometry missing via HTTP",
            "http_only": True,
        }

    land_geom = shapely_shape(geojson)
    land_geom_geojson = mapping(land_geom)
    processing_window_km = resolve_processing_window_km(processing_window_km)
    processing_geom, _ = build_complete_processing_window(
        land_geom, processing_window_km
    )
    processing_geom_geojson = mapping(processing_geom)
    d0 = date.fromisoformat(str(date_from)[:10])
    d1 = date.fromisoformat(str(date_to)[:10])
    org_id_str = "default"
    land_id_str = str(land_id)
    synthetic_job_id = job_id or f"http-s1-{land_id_str}-{d0}"

    logger.info(
        "s1_http_only_start",
        land_id=land_id_str,
        job_id=job_id,
        date_from=str(d0),
        date_to=str(d1),
    )

    t_search = time.perf_counter()
    scenes = search_s1_scenes(processing_geom_geojson, d0, d1)
    skipped_existing = 0
    agri_meta = _resolve_land_meta(None, land_id, remote=resolved)
    if agri_meta is None:
        logger.warning(
            "s1_agri_meta_unresolved",
            land_id=land_id_str,
        )
    if not force and agri_meta is not None:
        existing = existing_agri_scene_dates(None, agri_meta["land_id"], "S1")
        before = len(scenes)
        scenes = filter_scenes_skip_existing(
            scenes, existing, force=False, land_id=land_id_str, index="s1"
        )
        skipped_existing = before - len(scenes)

    logger.info(
        "job_phase_timing",
        phase="scene_search",
        sensor="S1",
        job_id=job_id,
        elapsed_ms=int((time.perf_counter() - t_search) * 1000),
        scenes=len(scenes),
        skipped_existing=skipped_existing,
        date_from=str(d0),
        date_to=str(d1),
        http_only=True,
    )

    if not scenes:
        return {
            "job_id": job_id,
            "land_id": land_id_str,
            "status": "completed",
            "scenes": 0,
            "skipped_existing": skipped_existing,
            "http_only": True,
        }

    bounds = processing_geom.bounds
    target_crs = analysis_crs_for_bounds(bounds)
    target_transform, target_shape, field_mask, bounds = compute_target_grid(
        bounds, land_geom, padding_degrees=0.0, target_crs=target_crs
    )

    workers = min(scene_max_workers(), len(scenes))
    set_total(synthetic_job_id, len(scenes), workers=workers)
    processed = _process_s1_scenes_parallel(
        job_id=synthetic_job_id,
        scenes=scenes,
        bounds=bounds,
        target_shape=target_shape,
        target_transform=target_transform,
        target_crs=target_crs,
        field_mask=field_mask,
        org_id_str=org_id_str,
        land_id_str=land_id_str,
        land_id=land_id,
        date_from=d0,
        date_to=d1,
        agri_meta=agri_meta,
        land_geom_geojson=land_geom_geojson,
        processing_window_km=processing_window_km,
        mq_task_id=mq_task_id,
        on_chunk=None,
    )
    return {
        "job_id": job_id,
        "land_id": land_id_str,
        "status": "completed",
        "scenes": len(scenes),
        "processed": processed,
        "skipped_existing": skipped_existing,
        "http_only": True,
    }


@celery_app.task(
    name="app.tasks.sentinel1.process_s1_backfill",
    bind=True,
    max_retries=3,
    time_limit=1800,
    soft_time_limit=1500,
)
def process_s1_backfill(
    self,
    job_id: str | None = None,
    land_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    force: bool = False,
    mq_task_id: str | None = None,
    processing_window_km: float | None = None,
    is_backfill: bool = True,
) -> dict:
    """Celery entry: search S1 GRD, upsert agri lonlat_v1 (COGs only if enabled).

    HTTP-only download hosts may pass ``land_id`` + date kwargs instead of a
    local Job id (orchestration fans out without SyncSession Job rows).

    强制HTTP模式时直接复用地块和日期参数，不访问下载机本地Job表。
    """
    from app.core.http_mode import ingest_http_only

    if ingest_http_only() or (land_id and not job_id):
        return _process_s1_http_only(
            job_id=job_id,
            land_id=land_id,
            date_from=date_from,
            date_to=date_to,
            force=force,
            mq_task_id=mq_task_id,
            processing_window_km=processing_window_km,
        )

    from app.models.tables import Job, LandParcel

    if not job_id:
        return {"status": "error", "detail": "job_id or land_id required"}

    session = get_db_session()
    try:
        job = session.get(Job, uuid.UUID(job_id))
        if not job:
            return {"job_id": job_id, "status": "error", "detail": "Job not found"}

        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        job.progress_json = {"current_step": "scene_search", "steps": {}}
        session.commit()

        land = session.get(LandParcel, job.land_id)
        if not land or land.deleted_at is not None:
            job.status = "failed"
            job.error = "Land parcel not found"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"job_id": job_id, "status": "failed"}
        if not is_scheduled_land_allowed(land.base_id, land.land_area_mu):
            job.status = "cancelled"
            job.error = "定时任务地块过滤：基地被排除或地块面积超过5000亩"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"job_id": job_id, "status": "cancelled", "reason": "land_filtered"}

        land_geom = geojson_to_shape(land.boundary_geojson)
        if land_geom is None:
            job.status = "failed"
            job.error = "Land parcel boundary is missing or invalid"
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
            return {"job_id": job_id, "status": "failed"}
        land_geom_geojson = mapping(land_geom)
        params = job.params_json or {}
        mq_task_id = params.get("mq_task_id")
        if mq_task_id is not None:
            mq_task_id = str(mq_task_id)
        date_from = date.fromisoformat(params["date_from"])
        date_to = date.fromisoformat(params["date_to"])
        processing_window_km = resolve_processing_window_km(
            params.get("processing_window_km")
        )
        processing_geom, _ = build_complete_processing_window(
            land_geom, processing_window_km
        )
        processing_geom_geojson = mapping(processing_geom)
        org_id_str = "default"  # STORAGE_TENANT; auth/orgs removed
        land_id_str = str(job.land_id)

        update_job_progress(session, job, "scene_search")
        t_search = time.perf_counter()
        scenes = search_s1_scenes(processing_geom_geojson, date_from, date_to)
        force = bool(params.get("force") or False)
        skipped_existing = 0
        agri_meta = _resolve_land_meta(session, land.land_id)
        if agri_meta is None:
            logger.warning(
                "s1_agri_meta_unresolved",
                land_id=land_id_str,
            )
        if not force:
            existing = existing_agri_scene_dates(session, land.land_id, "S1")
            before = len(scenes)
            scenes = filter_scenes_skip_existing(
                scenes, existing, force=False, land_id=land_id_str, index="s1"
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

        bounds = processing_geom.bounds
        target_crs = analysis_crs_for_bounds(bounds)
        target_transform, target_shape, field_mask, bounds = compute_target_grid(
            bounds, land_geom, padding_degrees=0.0, target_crs=target_crs
        )

        workers = min(scene_max_workers(), len(scenes))
        set_total(job_id, len(scenes), workers=workers)
        update_job_progress(
            session,
            job,
            "process_scenes",
            {"total_scenes": len(scenes), "workers": workers},
        )
        flush_to_job(
            session,
            job,
            extra={"total_scenes": len(scenes), "workers": workers},
            current_step="process_scenes",
        )
        land_id = job.land_id

        def _chunk_flush(_completed_n: int, _processed: int) -> None:
            flush_to_job(session, job, current_step="process_scenes")

        processed = _process_s1_scenes_parallel(
            job_id=job_id,
            scenes=scenes,
            bounds=bounds,
            target_shape=target_shape,
            target_transform=target_transform,
            target_crs=target_crs,
            field_mask=field_mask,
            org_id_str=org_id_str,
            land_id_str=land_id_str,
            land_id=land_id,
            date_from=date_from,
            date_to=date_to,
            agri_meta=agri_meta,
            land_geom_geojson=land_geom_geojson,
            processing_window_km=processing_window_km,
            mq_task_id=mq_task_id,
            on_chunk=_chunk_flush,
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
            {"layers_created": processed, "scenes_published": processed, "workers": workers},
        )
        job.status = "completed"
        job.finished_at = datetime.now(timezone.utc)
        flush_to_job(
            session,
            job,
            extra={
                "current_step": "complete",
                "layers_created": processed,
                "scenes_published": processed,
                "total_scenes": len(scenes),
                "scene_workers": workers,
                "workers": workers,
            },
            current_step="complete",
            complete_process_scenes=True,
        )
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
                try:
                    flush_to_job(
                        session,
                        job,
                        extra={"error": str(e)[:500]},
                        current_step="failed",
                    )
                except Exception:
                    session.commit()
        except Exception:
            session.rollback()
        raise
    finally:
        session.close()


@celery_app.task(
    name="app.tasks.sentinel1.backfill_s1_for_land",
    bind=True,
    max_retries=1,
    time_limit=120,
    soft_time_limit=90,
)
def backfill_s1_for_land(
    self,
    land_id: str,
    months: int | None = None,
    force: bool = False,
    mq_task_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    processing_window_km: float | None = None,
) -> dict:
    """按配置回溯窗口拆分S1日期段并派发子任务，限制单个任务的时间范围。"""
    from app.core.http_mode import ingest_http_only

    months = months or settings.index_backfill_months
    chunk_days = settings.index_backfill_chunk_days

    end_date = date.fromisoformat(date_to) if date_to else date.today()
    if date_from:
        start_date = date.fromisoformat(date_from)
    else:
        start_date = end_date - timedelta(days=months * 30)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    # S1直接回填也复用有界闭区间分片，保护绕过光学编排器的调用路径。
    chunks = split_inclusive_date_range(start_date, end_date, chunk_days)

    if ingest_http_only():
        # Fan-out Celery kwargs — no SyncSession / Job rows on download host.
        try:
            from app.core.http_mode import resolve_land_http

            resolve_land_http(land_id)
        except Exception as e:
            logger.error("s1_orchestration_failed", land_id=land_id, error=str(e))
            raise

        dispatched = 0
        for chunk_idx, (chunk_start, chunk_end) in enumerate(chunks):
            celery_app.send_task(
                "app.tasks.sentinel1.process_s1_backfill",
                kwargs={
                    "land_id": land_id,
                    "date_from": chunk_start.isoformat(),
                    "date_to": chunk_end.isoformat(),
                    "force": bool(force),
                    "mq_task_id": mq_task_id,
                    "processing_window_km": processing_window_km,
                    "is_backfill": True,
                },
                countdown=chunk_idx * 30,
            )
            dispatched += 1
        logger.info(
            "s1_orchestration_complete",
            land_id=land_id,
            jobs=dispatched,
            http_only=True,
        )
        return {
            "land_id": land_id,
            "status": "dispatched",
            "jobs": dispatched,
            "months": months,
            "force": force,
            "date_from": start_date.isoformat(),
            "date_to": end_date.isoformat(),
            "http_only": True,
        }

    from app.models.tables import LandParcel, Job

    session = get_db_session()
    try:
        land = session.get(LandParcel, land_id)
        if not land or land.deleted_at is not None:
            return {
                "land_id": land_id,
                "status": "error",
                "detail": "Land parcel not found",
            }

        # Always dispatch chunks; process_s1_backfill skips dates already present
        # unless force=True (coarse chunk skip left gaps unfilled).
        pending_sends: list[tuple[str, int]] = []
        dispatched = 0
        for chunk_idx, (chunk_start, chunk_end) in enumerate(chunks):
            job = Job(
                land_id=land.land_id,
                type="s1",
                status="pending",
                params_json={
                    "date_from": chunk_start.isoformat(),
                    "date_to": chunk_end.isoformat(),
                    "is_backfill": True,
                    "sensor": "S1",
                    "force": bool(force),
                    "processing_window_km": processing_window_km,
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
            "land_id": land_id,
            "status": "dispatched",
            "jobs": dispatched,
            "months": months,
            "force": force,
            "date_from": start_date.isoformat(),
            "date_to": end_date.isoformat(),
        }
    except Exception as e:
        session.rollback()
        logger.error("s1_orchestration_failed", land_id=land_id, error=str(e))
        raise
    finally:
        session.close()
