"""S2 下载前的地块级晴空预判（只读 SCL 20 m 窗口）。

季外且整景 ``eo:cloud_cover`` 超过阈值的 S2 景原本直接跳过；但整景云量与地块是否
被云覆盖关系不大（例如 61257 2026-10-04 整景 63% 云，地块 91% 晴空）。下载前用一次
小窗口 SCL 读取估算每个地块的晴空比例，超过 ``S2_PARCEL_CLEAR_KEEP_PCT``（默认 60）
即保留该地块的这一景。

- 晴空 = SCL 4/5/6/7（植被、裸土、水体、未分类）；分母为 SCL 1–11 的有效像元，
  0（无数据）与窗口外像元不计入。
- 每景只读一次：窗口取所有待判地块边界并集的外包框（处理窗口 ≤10 km，20 m 下
  约 500×500 像元、uint8），再对每个地块单独掩膜。
- 任何异常（网络、无 SCL、投影、空掩膜）都返回 None，调用方退回原有规则。
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger()

DEFAULT_KEEP_PCT = 60.0
CLEAR_CLASSES = (4, 5, 6, 7)


def parcel_clear_keep_pct() -> float | None:
    """保留阈值（%）；``S2_PARCEL_CLEAR_KEEP_PCT`` 为 off/none/false 或 ≤0 时关闭（None）。"""
    raw = os.getenv("S2_PARCEL_CLEAR_KEEP_PCT", "").strip().lower()
    if not raw:
        return DEFAULT_KEEP_PCT
    if raw in {"off", "none", "false", "no", "disabled"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_KEEP_PCT
    if not math.isfinite(value):
        return DEFAULT_KEEP_PCT
    if value <= 0:
        return None
    return min(value, 100.0)


def clear_pct_from_scl(values: np.ndarray) -> float | None:
    """SCL 样本（已裁到地块）中的晴空百分比；无有效样本时 None。"""
    arr = np.asarray(values)
    if arr.size == 0:
        return None
    codes = np.rint(arr[np.isfinite(arr)]).astype(np.int16) if arr.dtype.kind == "f" else arr
    valid = (codes >= 1) & (codes <= 11)
    n = int(valid.sum())
    if n == 0:
        return None
    clear = int(np.isin(codes[valid], CLEAR_CLASSES).sum())
    return round(100.0 * clear / n, 2)


def probe_parcel_clear(
    scl_href: str | None,
    geoms_wgs84: Mapping[str, Any],
) -> dict[str, float | None]:
    """读取一次 SCL 窗口，返回 ``{land_id: 晴空% 或 None}``；整体失败时返回 {}。"""
    if not scl_href or not geoms_wgs84:
        return {}
    try:
        import rasterio
        from rasterio.features import geometry_mask
        from rasterio.warp import transform_geom
        from rasterio.windows import Window, from_bounds
        from shapely.geometry import mapping, shape
        from shapely.ops import unary_union

        from app.core.band_parallel import gdal_read_slot

        with gdal_read_slot(), rasterio.Env(), rasterio.open(scl_href) as ds:
            projected = {
                land_id: shape(transform_geom("EPSG:4326", ds.crs, mapping(geom)))
                for land_id, geom in geoms_wgs84.items()
            }
            minx, miny, maxx, maxy = unary_union(list(projected.values())).bounds
            px = abs(ds.transform.a)
            window = from_bounds(
                minx - px, miny - px, maxx + px, maxy + px, ds.transform
            ).round_offsets().round_lengths()
            window = window.intersection(Window(0, 0, ds.width, ds.height))
            if window.width <= 0 or window.height <= 0:
                return {land_id: None for land_id in geoms_wgs84}
            scl = ds.read(1, window=window)
            transform = ds.window_transform(window)
    except Exception as exc:  # 网络/资产/投影等任何失败：退回原规则
        logger.warning(
            "s2_parcel_clear_probe_failed", scl_href=str(scl_href)[:200], error=str(exc)[:300]
        )
        return {}

    out: dict[str, float | None] = {}
    for land_id, geom in projected.items():
        try:
            inside = ~geometry_mask(
                [mapping(geom)], out_shape=scl.shape, transform=transform
            )
            if not inside.any():
                # 小地块可能不含任何像元中心，退回与边界相交的像元。
                inside = ~geometry_mask(
                    [mapping(geom)],
                    out_shape=scl.shape,
                    transform=transform,
                    all_touched=True,
                )
            out[land_id] = clear_pct_from_scl(scl[inside]) if inside.any() else None
        except Exception as exc:
            logger.warning(
                "s2_parcel_clear_mask_failed", land_id=land_id, error=str(exc)[:200]
            )
            out[land_id] = None
    return out


__all__ = [
    "CLEAR_CLASSES",
    "DEFAULT_KEEP_PCT",
    "clear_pct_from_scl",
    "parcel_clear_keep_pct",
    "probe_parcel_clear",
]
