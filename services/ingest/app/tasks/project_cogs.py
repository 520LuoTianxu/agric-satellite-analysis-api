"""项目级全波段 S2 L2A COG：按 group 计算块 × 采集日生成、上传 OSS 并回执 API 落元数据。

触发：
- 自动：``process_satellite_batch``（S2）结束后，若 ``PROJECT_SCENE_COGS_ENABLED`` 开启，
  按本批地块与已发布日期派发 :func:`build_project_cogs_for_lands`；
- 手动补数：API ``POST /v1/internal/agri/project-cogs/backfill`` 派发 :func:`build_project_cogs`；
- 本地：``python -m app.tasks.project_cogs --parcels-json ... --local-dir ...``（不连 API/OSS）。

幂等：OSS 键确定（见 ``core.project_cogs.cog_key``）；键已存在且旁车 JSON 中的块指纹
（地块集合/几何哈希/范围/尺寸/外扩距离，见 ``block_fingerprint``）与当前块一致时只重放回执，不重新下载；
指纹不一致（项目新增/删除地块、边界改动、块范围变化）则重读原始影像重写该块该日。
旧旁车无指纹：land_ids 与宽高一致则复用并回填指纹，否则重建。
整块无有效像元（且无景读取失败）时写“空标记”旁车（``empty=true`` + 指纹，无 COG），并删除旧 COG；
同指纹再次运行直接跳过、不重读影像；指纹变化后重新尝试。
因此项目新增地块后，用补数接口（force=False）对历史日期区间重跑即可只重建受影响的块。
内存：单块像元数受 ``PROJECT_SCENE_COGS_MAX_BLOCK_PIXELS`` 限制，逐景读取、即时转 uint16。
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import structlog
from rasterio.enums import Resampling

from app.core.project_cogs import (
    BAND_NAMES,
    COG_FORMAT_VERSION,
    PRODUCT,
    S2_L2A_BANDS,
    SCL_BAND,
    Block,
    band_radiometry,
    block_fingerprint,
    build_blocks,
    cog_key,
    fingerprint_status,
    mask_buffer_m,
    mosaic_fill,
    parcel_mask,
    project_cogs_enabled,
    sidecar_key,
    to_uint16_reflectance,
    to_uint16_scl,
    write_cog,
)

from app.core.oss_robust import retrying_report, robust

logger = structlog.get_logger()
UPLOAD_WORKERS = max(1, int(os.environ.get("PROJECT_SCENE_COGS_UPLOAD_WORKERS") or 4))

NATIVE_10M_BANDS = ("B02", "B03", "B04", "B08")
CACHE_SENSOR = "S2L2A_COG_SNAP"  # 与指数链路的窗口缓存分开（波段集合不同）


class LocalDirStorage:
    """开发/测试用的“伪 OSS”：把对象写到本地目录。"""

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _p(self, key: str) -> Path:
        return self.root / key

    def exists(self, key: str) -> bool:
        return self._p(key).exists()

    def upload_file(self, key: str, file_path: str, content_type: str | None = None) -> str:
        dst = self._p(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        Path(file_path).replace(dst)
        return key

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        dst = self._p(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return key

    def get_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        return self._p(key).read_bytes()

    def delete(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)

    def download_file(self, key: str, file_path: str) -> None:
        Path(file_path).write_bytes(self._p(key).read_bytes())


def _storage():
    local = str(os.environ.get("PROJECT_SCENE_COGS_LOCAL_DIR") or "").strip()
    if local:
        return LocalDirStorage(local)
    from agric_satellite_analysis_common.storage import get_storage

    return robust(get_storage())


def _report_to_api(payload: dict[str, Any]) -> dict[str, Any]:
    from agric_satellite_analysis_common.internal_api import apply_results

    return apply_results(payload)


# ---------------------------------------------------------------- STAC
def search_items(bbox4326: Sequence[float], date_from: date, date_to: date) -> list[dict]:
    """Earth Search sentinel-2-l2a；返回 item dict（含 assets/properties）。"""
    from app.core.stac_client import open_stac_client
    from app.tasks.pipeline import STAC_API_URL

    client = open_stac_client(STAC_API_URL)
    search = client.search(
        collections=["sentinel-2-l2a"],
        bbox=list(bbox4326),
        datetime=f"{date_from.isoformat()}T00:00:00Z/{date_to.isoformat()}T23:59:59Z",
        max_items=2000,
    )
    return [it.to_dict() for it in search.items()]


def group_items_by_date(items: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """按采集日（UTC 日期，与 parcel_scene_products.date 一致）分组；同日按云量升序。"""
    out: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for it in items:
        dt = str((it.get("properties") or {}).get("datetime") or "")[:10]
        if dt:
            out[dt].append(it)
    for v in out.values():
        v.sort(key=lambda i: (float((i.get("properties") or {}).get("eo:cloud_cover") or 0.0), str(i.get("id"))))
    return dict(sorted(out.items()))


# ---------------------------------------------------------------- read
def _item_hrefs(item: Mapping[str, Any]) -> dict[str, str]:
    assets = item.get("assets") or {}
    hrefs: dict[str, str] = {}
    for band, asset in S2_L2A_BANDS + (SCL_BAND,):
        a = assets.get(asset) or assets.get(band) or assets.get(band.lower())
        if not a or not a.get("href"):
            raise KeyError(f"{item.get('id')}: missing asset {asset}")
        hrefs[band] = a["href"]
    return hrefs


def read_item_stack(item: Mapping[str, Any], block: Block) -> np.ndarray:
    """读取一景在块网格上的 13 波段 uint16 栈（逐波段转换，避免保留 float32 全栈）。"""
    from app.core.band_window_cache import read_scene_window, write_scene_window
    from app.tasks.pipeline import read_bands_windowed_parallel

    hrefs = _item_hrefs(item)
    shape = (block.height, block.width)
    # 10 m 波段与块网格同格对齐，用最近邻保持原值（与原型一致）；20/60 m 波段双线性，SCL 最近邻
    resampling = {b: Resampling.nearest for b in NATIVE_10M_BANDS}
    resampling["SCL"] = Resampling.nearest
    arrays = read_scene_window(
        scene_id=str(item.get("id") or ""),
        sensor=CACHE_SENSOR,
        target_shape=shape,
        target_transform=block.transform,
        target_crs=block.crs,
        band_hrefs=hrefs,
        resampling_by_band=resampling,
    )
    if arrays is None:
        arrays = read_bands_windowed_parallel(
            hrefs,
            block.bounds_4326(),
            shape,
            block.transform,
            target_crs=block.crs,
            resampling_by_band=resampling,
            log_context={"scene_id": item.get("id"), "sensor": CACHE_SENSOR},
            snap_window=True,
        )
        try:
            write_scene_window(
                scene_id=str(item.get("id") or ""),
                sensor=CACHE_SENSOR,
                target_shape=shape,
                target_transform=block.transform,
                target_crs=block.crs,
                band_hrefs=hrefs,
                arrays=arrays,
                resampling_by_band=resampling,
            )
        except Exception as exc:  # 缓存只是加速
            logger.warning("project_cog_cache_write_failed", error=str(exc))
    props = item.get("properties") or {}
    assets = item.get("assets") or {}
    stack = np.zeros((len(BAND_NAMES),) + shape, dtype=np.uint16)
    for i, (band, asset) in enumerate(S2_L2A_BANDS):
        scale, offset = band_radiometry(props, assets.get(asset) or {})
        stack[i] = to_uint16_reflectance(arrays.pop(band), scale, offset)
    stack[-1] = to_uint16_scl(arrays.pop("SCL"))
    # SCL=0（NO_DATA）处光谱也视为无数据
    stack[:, stack[-1] == 0] = 0
    return stack


# ---------------------------------------------------------------- build
def _cog_payload(*, group_id: str, block: Block, obs_date: str, key: str, used_ids: Sequence[str],
                 items: Sequence[Mapping[str, Any]], size: int, valid: int, mask_pixels: int) -> dict[str, Any]:
    by_id = {str(i.get("id")): i for i in items}
    used = [by_id[x] for x in used_ids if x in by_id]
    acquired = sorted(str((i.get("properties") or {}).get("datetime")) for i in used)
    w, s, e, n = block.bounds_4326()
    x0, y0, x1, y1 = block.bounds
    return {
        "kind": "project_scene_cog",
        "format_version": COG_FORMAT_VERSION,
        "product": PRODUCT,
        "sensor": "S2",
        "group_id": group_id,
        "block_key": block.key,
        "obs_date": obs_date,
        "acquired_at": acquired[0] if acquired else None,
        "oss_key": key,
        "size_bytes": size,
        "crs": block.crs,
        "transform": list(block.transform)[:6],
        "width": block.width,
        "height": block.height,
        "bounds": [x0, y0, x1, y1],
        "bounds_4326": [w, s, e, n],
        "bands": list(BAND_NAMES),
        "dtype": "uint16",
        "nodata": 0,
        "scale": 0.0001,
        "scene_ids": [str(x) for x in used_ids],
        "tiles": sorted({str((i.get("properties") or {}).get("grid:code") or "") for i in used} - {""}),
        "land_ids": list(block.land_ids),
        "parcel_count": len(block.land_ids),
        "valid_pixel_count": valid,
        "mask_pixel_count": mask_pixels,
        "mask_buffer_m": mask_buffer_m(),
        "fingerprint": block_fingerprint(block),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


class StaleCogError(Exception):
    """OSS 上的 COG 与当前块不一致（尺寸/范围变化），需要重建。"""


def rebuild_sidecar(*, group_id: str, block: Block, obs_date: str, key: str,
                    items: Sequence[Mapping[str, Any]], storage: Any, mask: np.ndarray) -> dict[str, Any]:
    """COG 已在 OSS 但旁车 JSON 缺失（上次上传后回执/旁车写失败）：从 COG 本身重建旁车并返回回执载荷。"""
    import rasterio

    fd, tmp = tempfile.mkstemp(suffix=".tif", prefix="project_cog_side_")
    os.close(fd)
    try:
        storage.download_file(key, tmp)
        size = os.path.getsize(tmp)
        with rasterio.open(tmp) as ds:
            if (ds.width, ds.height) != (block.width, block.height):
                raise StaleCogError(f"{key}: COG {ds.width}x{ds.height} != block {block.width}x{block.height}")
            tags = ds.tags()
            data = ds.read(list(range(1, ds.count)))  # 光谱波段（末波段为 SCL）
        valid = int((data != 0).any(axis=0).sum())
        del data
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    used_ids = [x for x in str(tags.get("scenes") or "").split(",") if x]
    payload = _cog_payload(group_id=group_id, block=block, obs_date=obs_date, key=key, used_ids=used_ids,
                           items=items, size=size, valid=valid, mask_pixels=int(mask.sum()))
    payload["sidecar_rebuilt"] = True
    storage.put_bytes(sidecar_key(key), json.dumps(payload, ensure_ascii=False).encode(), content_type="application/json")
    logger.info("project_cog_sidecar_rebuilt", key=key)
    return payload


def compose_block_date(
    *,
    group_id: str,
    block: Block,
    obs_date: str,
    items: Sequence[Mapping[str, Any]],
    reader: Callable[[Mapping[str, Any], Block], np.ndarray] = read_item_stack,
    mask: np.ndarray | None = None,
    info: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """读景、镶嵌并写本地 COG 临时文件；返回 (临时路径, 回执载荷)。整块无有效像元时返回 None。

    ``info`` 若给出，会写入 ``read_failed``（读取失败的景数），用于区分“真无数据”和“读失败”。
    """
    if info is not None:
        info["read_failed"] = 0
    key = cog_key(group_id, block.key, obs_date)
    mask = parcel_mask(block) if mask is None else mask
    acc: np.ndarray | None = None
    used: list[Mapping[str, Any]] = []
    for it in items:
        try:
            stack = reader(it, block)
        except Exception as exc:
            logger.warning("project_cog_item_read_failed", item=it.get("id"), exc_type=type(exc).__name__, error=str(exc)[:300])
            if info is not None:
                info["read_failed"] += 1
            continue
        stack[:, ~mask] = 0
        if not (stack[:-1] != 0).any():
            continue
        before = None if acc is None else int((acc[:-1] != 0).any(axis=0).sum())
        acc = mosaic_fill(acc, stack)
        del stack
        if before is None or int((acc[:-1] != 0).any(axis=0).sum()) > before:
            used.append(it)
        if int((acc[:-1] != 0).any(axis=0)[mask].sum()) >= int(mask.sum()):
            break  # 掩膜内已全部覆盖
    if acc is None:
        return None
    valid = int((acc[:-1] != 0).any(axis=0).sum())
    tags = {
        "product": PRODUCT,
        "group_id": group_id,
        "block": block.key,
        "date": obs_date,
        "scale": "0.0001",
        "scenes": ",".join(str(i.get("id")) for i in used),
    }
    fd, tmp = tempfile.mkstemp(suffix=".tif", prefix="project_cog_")
    os.close(fd)
    try:
        write_cog(tmp, acc, block, tags=tags)
    except Exception:
        os.remove(tmp)
        raise
    del acc
    payload = _cog_payload(group_id=group_id, block=block, obs_date=obs_date, key=key,
                           used_ids=[str(i.get("id")) for i in used], items=items,
                           size=os.path.getsize(tmp), valid=valid, mask_pixels=int(mask.sum()))
    return tmp, payload


def publish_block_date(tmp: str, payload: dict[str, Any], storage: Any) -> dict[str, Any]:
    """上传 COG 与旁车 JSON（旁车最后写：存在即代表 COG 完整）；总会删除临时文件。"""
    key = payload["oss_key"]
    try:
        storage.upload_file(key, tmp, content_type="image/tiff")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    storage.put_bytes(sidecar_key(key), json.dumps(payload, ensure_ascii=False).encode(), content_type="application/json")
    return payload


def _empty_marker(*, group_id: str, block: Block, obs_date: str, key: str, items: Sequence[Mapping[str, Any]],
                  fp: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "project_scene_cog_empty",
        "empty": True,
        "format_version": COG_FORMAT_VERSION,
        "product": PRODUCT,
        "group_id": group_id,
        "block_key": block.key,
        "obs_date": obs_date,
        "oss_key": key,
        "scene_ids": [str(i.get("id")) for i in items],
        "land_ids": list(block.land_ids),
        "width": block.width,
        "height": block.height,
        "fingerprint": dict(fp),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def mark_empty(*, group_id: str, block: Block, obs_date: str, items: Sequence[Mapping[str, Any]], storage: Any,
               fp: Mapping[str, Any] | None = None, cog_exists: bool | None = None) -> dict[str, Any]:
    """整块无有效像元：删除旧 COG（若有），写空标记旁车。删除失败只告警（标记照写，复用逻辑以标记为准）。"""
    key = cog_key(group_id, block.key, obs_date)
    fp = block_fingerprint(block) if fp is None else fp
    if cog_exists is None:
        cog_exists = storage.exists(key)
    if cog_exists:
        try:
            storage.delete(key)
            logger.info("project_cog_stale_deleted", key=key)
        except Exception as exc:
            logger.warning("project_cog_stale_delete_failed", key=key, exc_type=type(exc).__name__, error=str(exc)[:200])
    marker = _empty_marker(group_id=group_id, block=block, obs_date=obs_date, key=key, items=items, fp=fp)
    storage.put_bytes(sidecar_key(key), json.dumps(marker, ensure_ascii=False).encode(), content_type="application/json")
    return marker


def is_empty_marker(payload: Any) -> bool:
    return isinstance(payload, Mapping) and bool(payload.get("empty"))


def try_reuse(*, group_id: str, block: Block, obs_date: str, items: Sequence[Mapping[str, Any]],
              storage: Any, mask: np.ndarray, fp: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """尝试复用 OSS 上已有的块-日 COG，返回回执载荷；需要重建（不存在/指纹不符）时返回 None。

    - COG+旁车且指纹一致：重放旁车；
    - 旧旁车（无指纹）且 land_ids、宽高一致：复用并把指纹回填进旁车（不重读影像）；
    - 指纹不一致：返回 None（调用方重读影像并覆盖 COG+旁车，再回执）；
    - 指纹一致的空标记（``empty=true``）：原样返回标记，调用方按“无数据”处理，不读影像；
    - COG 在、旁车缺失/不可读：由 COG 重建旁车（尺寸与当前块不符则返回 None 重建）。
    """
    key = cog_key(group_id, block.key, obs_date)
    side = sidecar_key(key)
    fp = block_fingerprint(block) if fp is None else fp
    if not storage.exists(key):
        # 无 COG：仅当存在指纹一致的空标记时跳过，否则需构建
        if storage.exists(side):
            try:
                marker = json.loads(storage.get_bytes(side))
            except Exception:
                return None
            if is_empty_marker(marker) and fingerprint_status(marker, fp) == "match":
                return marker
        return None
    if storage.exists(side):
        try:
            payload = json.loads(storage.get_bytes(side))
        except Exception as exc:
            logger.warning("project_cog_sidecar_unreadable", key=side, exc_type=type(exc).__name__, error=str(exc)[:200])
            payload = None
        if payload is not None:
            status = fingerprint_status(payload, fp)
            if is_empty_marker(payload):
                if status == "match":
                    return payload  # 旧 COG 删除失败遗留：以空标记为准
                return None
            if status == "match":
                payload["reused"] = True
                return payload
            if status == "legacy_match":
                payload.pop("reused", None)
                payload["fingerprint"] = dict(fp)
                storage.put_bytes(side, json.dumps(payload, ensure_ascii=False).encode(), content_type="application/json")
                logger.info("project_cog_fingerprint_backfilled", key=key)
                payload["reused"] = True
                payload["fingerprint_backfilled"] = True
                return payload
            logger.info("project_cog_fingerprint_mismatch", key=key, group_id=group_id, block=block.key, date=obs_date,
                        old_land_ids=len(payload.get("land_ids") or []), new_land_ids=len(fp["land_ids"]))
            return None
    else:
        logger.info("project_cog_exists_without_sidecar", key=key)
    try:
        payload = rebuild_sidecar(group_id=group_id, block=block, obs_date=obs_date, key=key, items=items,
                                  storage=storage, mask=mask)
    except StaleCogError as exc:
        logger.info("project_cog_stale_without_sidecar", key=key, error=str(exc)[:200])
        return None
    payload["reused"] = True
    return payload


def build_block_date(
    *,
    group_id: str,
    block: Block,
    obs_date: str,
    items: Sequence[Mapping[str, Any]],
    storage: Any,
    force: bool = False,
    reader: Callable[[Mapping[str, Any], Block], np.ndarray] = read_item_stack,
    mask: np.ndarray | None = None,
) -> dict[str, Any] | None:
    """生成（或复用）一个块一天的 COG，返回回执载荷；整块无有效像元时返回 None。

    COG 已存在：指纹一致则重放旁车，指纹不符则重建；无旁车则由 COG 重建旁车（见 :func:`try_reuse`）。
    整块无有效像元（且无景读取失败）时写空标记并删除旧 COG；指纹一致的空标记直接返回 None、不读影像。
    """
    mask = parcel_mask(block) if mask is None else mask
    fp = block_fingerprint(block)
    if not force:
        payload = try_reuse(group_id=group_id, block=block, obs_date=obs_date, items=items, storage=storage,
                            mask=mask, fp=fp)
        if is_empty_marker(payload):
            return None
        if payload is not None:
            return payload
    info: dict[str, Any] = {}
    built = compose_block_date(group_id=group_id, block=block, obs_date=obs_date, items=items, reader=reader,
                               mask=mask, info=info)
    if built is None:
        if not info.get("read_failed"):
            mark_empty(group_id=group_id, block=block, obs_date=obs_date, items=items, storage=storage, fp=fp)
        return None
    return publish_block_date(*built, storage)


def run_group(
    group_id: str,
    parcels: Sequence[Mapping[str, Any]],
    date_from: date,
    date_to: date,
    *,
    dates: Iterable[str] | None = None,
    force: bool = False,
    storage: Any = None,
    report: Callable[[dict[str, Any]], Any] | None = _report_to_api,
    searcher: Callable[..., list[dict]] = search_items,
    reader: Callable[[Mapping[str, Any], Block], np.ndarray] = read_item_stack,
    upload_workers: int | None = None,
) -> dict[str, Any]:
    """为一个项目在日期区间内（可选限定具体日期）生成全部块的 COG。

    读景/写 COG 在当前线程串行；上传 OSS + 回执 API 放到小线程池（默认 4），
    在途任务数有上限（2×workers），避免本地临时 COG 堆积。每块的掩膜只算一次。
    """
    storage = robust(storage or _storage())
    report = retrying_report(report)
    workers = max(1, upload_workers or UPLOAD_WORKERS)
    wanted = {str(d)[:10] for d in dates} if dates else None
    blocks = build_blocks(parcels)
    stats = {"group_id": group_id, "blocks": len(blocks), "written": 0, "reused": 0, "empty": 0, "failed": 0,
             "rebuilt": 0, "fingerprint_backfilled": 0, "empty_marked_skipped": 0,
             "reported": 0, "report_failed": 0, "failed_dates": []}
    lock = threading.Lock()

    def _fail(block_key: str, obs_date: str, stage: str, exc: BaseException) -> None:
        logger.error("project_cog_build_failed", group_id=group_id, block=block_key, date=obs_date, stage=stage,
                     exc_type=type(exc).__name__, error=str(exc)[:500], exc_info=exc)
        with lock:
            stats["failed"] += 1
            stats["failed_dates"].append(f"{block_key}/{obs_date}")

    def _report(payload: dict[str, Any]) -> None:
        if report is None:
            return
        try:
            report(payload)
            with lock:
                stats["reported"] += 1
        except Exception as exc:
            logger.error("project_cog_report_failed", key=payload.get("oss_key"), exc_type=type(exc).__name__,
                         error=str(exc)[:300])
            with lock:
                stats["report_failed"] += 1

    def _publish(tmp: str, payload: dict[str, Any]) -> None:
        try:
            publish_block_date(tmp, payload, storage)
        except Exception as exc:
            _fail(payload["block_key"], payload["obs_date"], "upload", exc)
            return
        with lock:
            stats["written"] += 1
        _report(payload)

    pending: deque[Future] = deque()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cog-upload") as pool:
        for block in blocks:
            try:
                by_date = group_items_by_date(searcher(block.bounds_4326(), date_from, date_to))
            except Exception as exc:
                _fail(block.key, "*", "search", exc)
                continue
            mask = parcel_mask(block)  # 每块一次，各日期复用
            fp = block_fingerprint(block)
            for obs_date, items in by_date.items():
                if wanted is not None and obs_date not in wanted:
                    continue
                try:
                    payload = None if force else try_reuse(
                        group_id=group_id, block=block, obs_date=obs_date, items=items,
                        storage=storage, mask=mask, fp=fp)
                    if is_empty_marker(payload):
                        stats["empty"] += 1
                        stats["empty_marked_skipped"] += 1
                    elif payload is not None:
                        with lock:
                            stats["reused"] += 1
                            if payload.get("fingerprint_backfilled"):
                                stats["fingerprint_backfilled"] += 1
                        pending.append(pool.submit(_report, payload))
                    else:
                        cog_exists = storage.exists(cog_key(group_id, block.key, obs_date))
                        if not force and cog_exists:
                            stats["rebuilt"] += 1
                        info: dict[str, Any] = {}
                        built = compose_block_date(group_id=group_id, block=block, obs_date=obs_date, items=items,
                                                   reader=reader, mask=mask, info=info)
                        if built is None:
                            stats["empty"] += 1
                            if not info.get("read_failed"):
                                mark_empty(group_id=group_id, block=block, obs_date=obs_date, items=items,
                                           storage=storage, fp=fp, cog_exists=cog_exists)
                        else:
                            pending.append(pool.submit(_publish, *built))
                except Exception as exc:
                    _fail(block.key, obs_date, "build", exc)
                while len(pending) > 2 * workers:
                    pending.popleft().result()
        for f in pending:
            f.result()
    logger.info("project_cogs_group_done", **stats)
    return stats


def _inputs(*, group_id: str | None = None, land_ids: Sequence[str] | None = None) -> list[dict]:
    from agric_satellite_analysis_common.internal_api import project_cog_inputs

    return project_cog_inputs(group_id=group_id, land_ids=list(land_ids or []))


# ---------------------------------------------------------------- celery
from app.worker import celery_app  # noqa: E402


@celery_app.task(name="app.tasks.project_cogs.build_project_cogs", acks_late=True, time_limit=6 * 3600, soft_time_limit=6 * 3600 - 300)
def build_project_cogs(group_id: str, date_from: str, date_to: str, force: bool = False) -> dict:
    """手动补数：一个项目在日期区间内的全部块 × 采集日。"""
    groups = _inputs(group_id=str(group_id))
    out = []
    for g in groups:
        out.append(run_group(str(g["group_id"]), g["lands"], date.fromisoformat(date_from), date.fromisoformat(date_to), force=bool(force)))
    return {"groups": out}


@celery_app.task(name="app.tasks.project_cogs.build_project_cogs_for_lands", acks_late=True, time_limit=3 * 3600, soft_time_limit=3 * 3600 - 300)
def build_project_cogs_for_lands(land_ids: list[str], dates: list[str]) -> dict:
    """卫星批任务后的自动生成：找到这些地块所属项目，为给定采集日生成 COG。"""
    if not dates:
        return {"groups": []}
    ds = sorted({str(d)[:10] for d in dates})
    d0, d1 = date.fromisoformat(ds[0]), date.fromisoformat(ds[-1])
    out = []
    for g in _inputs(land_ids=land_ids):
        out.append(run_group(str(g["group_id"]), g["lands"], d0, d1, dates=ds))
    return {"groups": out}


def maybe_dispatch_after_batch(land_ids: Sequence[str], published_products: Sequence[Mapping[str, Any]], sensor: str) -> bool:
    """卫星批任务结束后调用：开关开启且有 S2 新发布日期时异步派发，失败不影响主流程。"""
    if sensor != "S2" or not project_cogs_enabled():
        return False
    dates = sorted({str(p.get("date"))[:10] for p in published_products or [] if p.get("date")})
    if not dates or not land_ids:
        return False
    try:
        build_project_cogs_for_lands.apply_async(args=[list(land_ids), dates])
        return True
    except Exception as exc:
        logger.warning("project_cog_dispatch_failed", error=str(exc)[:300])
        return False


# ---------------------------------------------------------------- CLI
def _cli(argv: Sequence[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description="生成项目级全波段 S2 L2A COG")
    ap.add_argument("--group-id", required=True)
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--date", action="append", help="只生成这些日期（可多次）")
    ap.add_argument("--parcels-json", help="本地地块文件（[{land_id, boundary_geojson}]）；不给则走 Internal API")
    ap.add_argument("--local-dir", help="写到本地目录（伪 OSS），不回执 API")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    storage = LocalDirStorage(a.local_dir) if a.local_dir else None
    if a.parcels_json:
        parcels = json.loads(Path(a.parcels_json).read_text())
    else:
        groups = _inputs(group_id=a.group_id)
        parcels = groups[0]["lands"] if groups else []
    return run_group(
        a.group_id, parcels, date.fromisoformat(a.date_from), date.fromisoformat(a.date_to),
        dates=a.date, force=a.force, storage=storage, report=None if a.local_dir else _report_to_api,
    )


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(_cli(), ensure_ascii=False, indent=1))
