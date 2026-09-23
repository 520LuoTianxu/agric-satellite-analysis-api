"""项目区级压缩像素资产：一次下载，多地块裁剪复用。

资产采用“JSON manifest + base64 压缩像素块”的格式：manifest 保留 CRS、
Affine 网格、日期、波段和缩放信息，像素块使用 gzip 后的 float32 原始数组。
这样既满足像素 JSON 可追溯，又不会把百万像素展开成极其膨胀的 JSON 数字列表。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import re
from datetime import date
from typing import Any

import numpy as np
from rasterio.transform import Affine

from agric_satellite_analysis_common.internal_api import upsert_virtual_area_asset
from agric_satellite_analysis_common.storage import get_storage

import structlog

logger = structlog.get_logger()

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_component(value: str) -> str:
    value = _SAFE_COMPONENT.sub("_", str(value)).strip("._")
    return value[:120] or "unknown"


def _encode_array(array: np.ndarray) -> dict[str, Any]:
    normalized = np.asarray(array, dtype=np.float32, order="C")
    raw = gzip.compress(normalized.tobytes(order="C"), compresslevel=6)
    return {
        "dtype": "float32",
        "shape": list(normalized.shape),
        "encoding": "gzip+base64+raw",
        "data": base64.b64encode(raw).decode("ascii"),
    }


def _decode_array(value: dict[str, Any]) -> np.ndarray:
    if value.get("encoding") != "gzip+base64+raw" or value.get("dtype") != "float32":
        raise ValueError("不支持的项目区像素编码")
    shape = tuple(int(item) for item in value.get("shape") or ())
    if len(shape) != 2 or any(item <= 0 for item in shape):
        raise ValueError("项目区像素网格尺寸无效")
    raw = gzip.decompress(base64.b64decode(str(value["data"])))
    expected = int(np.prod(shape)) * np.dtype("float32").itemsize
    if len(raw) != expected:
        raise ValueError("项目区像素块大小与网格不一致")
    return np.frombuffer(raw, dtype=np.float32).reshape(shape).copy()


def _grid_manifest(
    grid: tuple[Any, tuple[int, int], Any, tuple[float, float, float, float]],
) -> dict[str, Any]:
    target_transform, target_shape, _field_mask, bounds = grid
    return {
        "crs": "EPSG:4326",
        "transform": [float(value) for value in target_transform[:6]],
        "height": int(target_shape[0]),
        "width": int(target_shape[1]),
        "bounds": [float(value) for value in bounds],
        "resolution": [float(abs(target_transform.a)), float(abs(target_transform.e))],
    }


def _scene_date(scene: dict[str, Any]) -> str:
    value = scene.get("date")
    return value.isoformat() if hasattr(value, "isoformat") else str(value)[:10]


def _cache_arrays(
    sensor: str, bands: dict[str, np.ndarray], scl: np.ndarray | None
) -> dict[str, np.ndarray]:
    arrays = {
        str(key): np.asarray(value, dtype=np.float32) for key, value in bands.items()
    }
    if sensor == "S1":
        return {key: arrays[key] for key in ("vv", "vh") if key in arrays}
    if scl is not None:
        arrays["SCL"] = np.asarray(scl, dtype=np.float32)
    # 把派生指数也放入项目区资产，便于后续服务按指标直接裁剪；原始波段
    # 同时保留，保证旧的公式、云掩膜和重算逻辑不会失去来源数据。
    from app.tasks.agri_lonlat import INDEX_KEY_TO_PIXEL, agri_optical_index_defs

    for definition in agri_optical_index_defs():
        if not all(key in arrays for key in definition.bands):
            continue
        values = definition.formula({key: arrays[key] for key in definition.bands})
        values = np.asarray(values, dtype=np.float32)
        values[~np.isfinite(values)] = np.nan
        arrays[INDEX_KEY_TO_PIXEL[definition.key]] = values
    return arrays


def _preview_png(sensor: str, arrays: dict[str, np.ndarray]) -> bytes | None:
    """生成项目区概览图；分析使用的真实数据仍以像素 JSON 为准。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    if sensor == "S2":
        keys = ("B04", "B03", "B02")
    else:
        keys = ("vv", "vh", "vv")
    if not all(key in arrays for key in keys):
        return None
    rgb = np.stack([arrays[key] for key in keys], axis=-1).astype(np.float32)
    output = np.zeros(rgb.shape, dtype=np.uint8)
    for index in range(3):
        channel = rgb[..., index]
        finite = channel[np.isfinite(channel)]
        if finite.size == 0:
            continue
        low, high = np.percentile(finite, [2, 98])
        if high <= low:
            high = low + 1.0
        output[..., index] = np.clip((channel - low) / (high - low) * 255, 0, 255)[
            ...,
        ].astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(output, mode="RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def upload_virtual_area_scene(
    *,
    tile_id: str,
    sensor: str,
    scene: dict[str, Any],
    grid: tuple[Any, tuple[int, int], Any, tuple[float, float, float, float]],
    bands: dict[str, np.ndarray],
    scl: np.ndarray | None = None,
) -> dict[str, Any]:
    """上传项目区像素 JSON/PNG 并回报元数据；失败不会影响地块结果入库。"""
    sensor = str(sensor).upper()
    date_str = _scene_date(scene)
    scene_id = str(scene.get("id") or "unknown")
    arrays = _cache_arrays(sensor, bands, scl)
    if not arrays:
        raise ValueError("项目区资产没有可保存的像素波段")
    manifest = {
        "schema_version": 1,
        "asset_kind": "pixel_json",
        "tile_id": tile_id,
        "sensor": sensor,
        "scene_id": scene_id,
        "scene_date": date_str,
        # 保留复用地块结果所需的 STAC 元数据；像素数组本身仍只存一份。
        # band_hrefs 让开启 decloud 的缓存命中路径仍能使用原有质量缓存逻辑。
        "scene_meta": {
            "cloud_cover": scene.get("cloud_cover"),
            "geometry": scene.get("geometry"),
            "relative_orbit": scene.get("relative_orbit"),
            "band_hrefs": {
                str(key): str(value)
                for key, value in (scene.get("band_hrefs") or {}).items()
                if value
            },
        },
        "grid": _grid_manifest(grid),
        "bands": {key: _encode_array(value) for key, value in sorted(arrays.items())},
    }
    raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    compressed = gzip.compress(raw, compresslevel=6)
    digest = hashlib.sha256(compressed).hexdigest()
    base = (
        f"virtual_project_area/{_safe_component(tile_id)}/{sensor}/"
        f"{date_str}/{_safe_component(scene_id)}"
    )
    pixel_key = f"{base}.pixel.json.gz"
    storage = get_storage()
    storage.put_bytes(pixel_key, compressed, content_type="application/json+gzip")
    grid_json = {
        **manifest["grid"],
        "band_names": sorted(arrays),
        "schema_version": manifest["schema_version"],
    }
    try:
        upsert_virtual_area_asset(
            tile_id,
            sensor=sensor,
            scene_date=date_str,
            scene_id=scene_id,
            asset_kind="pixel_json",
            oss_key=pixel_key,
            grid_json=grid_json,
            checksum=digest,
            byte_size=len(compressed),
        )
    except Exception as exc:  # OSS 已成功，元数据可由补偿任务重试登记。
        logger.warning(
            "virtual_area_asset_metadata_failed", tile_id=tile_id, error=str(exc)
        )

    preview = _preview_png(sensor, arrays)
    preview_key = None
    if preview:
        preview_key = f"{base}.preview.png"
        storage.put_bytes(preview_key, preview, content_type="image/png")
        try:
            upsert_virtual_area_asset(
                tile_id,
                sensor=sensor,
                scene_date=date_str,
                scene_id=scene_id,
                asset_kind="preview_png",
                oss_key=preview_key,
                grid_json=grid_json,
                checksum=hashlib.sha256(preview).hexdigest(),
                byte_size=len(preview),
                format="png",
                compression="none",
            )
        except Exception as exc:
            logger.warning(
                "virtual_area_preview_metadata_failed", tile_id=tile_id, error=str(exc)
            )
    return {
        "pixel_oss_key": pixel_key,
        "preview_oss_key": preview_key,
        "grid": grid_json,
        "checksum": digest,
        "byte_size": len(compressed),
    }


def load_virtual_area_scene(
    asset: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, np.ndarray],
    tuple[Any, tuple[int, int], None, tuple[float, float, float, float]],
]:
    """从项目区 OSS 资产恢复 scene、波段和共享网格，供后续裁剪。"""
    raw = get_storage().get_bytes(str(asset["oss_key"]))
    manifest = json.loads(gzip.decompress(raw).decode("utf-8"))
    grid = manifest.get("grid") or {}
    height, width = int(grid["height"]), int(grid["width"])
    transform = Affine(*[float(value) for value in grid["transform"]])
    bounds = tuple(float(value) for value in grid["bounds"])
    arrays = {
        key: _decode_array(value)
        for key, value in (manifest.get("bands") or {}).items()
    }
    scene = {
        "id": str(manifest.get("scene_id") or asset.get("scene_id")),
        "date": date.fromisoformat(
            str(manifest.get("scene_date") or asset["scene_date"])[:10]
        ),
        **{
            key: value
            for key, value in (manifest.get("scene_meta") or {}).items()
            if key in {"cloud_cover", "geometry", "relative_orbit", "band_hrefs"}
        },
        "band_hrefs": dict((manifest.get("scene_meta") or {}).get("band_hrefs") or {}),
    }
    return scene, arrays, (transform, (height, width), None, bounds)


__all__ = ["load_virtual_area_scene", "upload_virtual_area_scene"]
