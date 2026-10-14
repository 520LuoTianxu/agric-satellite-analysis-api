"""项目级全波段 S2 L2A COG：按 group 计算块 × 采集日生成、上传 OSS 并回执 API 落元数据。

触发：
- 自动：``process_satellite_batch``（S2）结束后，若 ``PROJECT_SCENE_COGS_ENABLED`` 开启，
  按本批地块与已发布日期派发 :func:`build_project_cogs_for_lands`；
- 手动补数：API ``POST /v1/internal/agri/project-cogs/backfill`` 派发 :func:`build_project_cogs`；
- 本地：``python -m app.tasks.project_cogs --parcels-json ... --local-dir ...``（不连 API/OSS）。

幂等：OSS 键确定（见 ``core.project_cogs.cog_key``）；键已存在且旁车 JSON 存在时只重放回执，不重新下载。
内存：单块像元数受 ``PROJECT_SCENE_COGS_MAX_BLOCK_PIXELS`` 限制，逐景读取、即时转 uint16。
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import defaultdict
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
    build_blocks,
    cog_key,
    mask_buffer_m,
    mosaic_fill,
    parcel_mask,
    project_cogs_enabled,
    sidecar_key,
    to_uint16_reflectance,
    to_uint16_scl,
    write_cog,
)

logger = structlog.get_logger()

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

    def download_file(self, key: str, file_path: str) -> None:
        Path(file_path).write_bytes(self._p(key).read_bytes())


def _storage():
    local = str(os.environ.get("PROJECT_SCENE_COGS_LOCAL_DIR") or "").strip()
    if local:
        return LocalDirStorage(local)
    from agric_satellite_analysis_common.storage import get_storage

    return get_storage()


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
def build_block_date(
    *,
    group_id: str,
    block: Block,
    obs_date: str,
    items: Sequence[Mapping[str, Any]],
    storage: Any,
    force: bool = False,
    reader: Callable[[Mapping[str, Any], Block], np.ndarray] = read_item_stack,
) -> dict[str, Any] | None:
    """生成（或复用）一个块一天的 COG，返回回执载荷；整块无有效像元时返回 None。"""
    key = cog_key(group_id, block.key, obs_date)
    side = sidecar_key(key)
    if not force and storage.exists(key):
        if storage.exists(side):
            try:
                payload = json.loads(storage.get_bytes(side))
                payload["reused"] = True
                return payload
            except Exception:
                logger.warning("project_cog_sidecar_unreadable", key=side)
        else:
            logger.info("project_cog_exists_without_sidecar", key=key)
            return {"kind": "project_scene_cog", "oss_key": key, "skipped": "exists_no_sidecar"}

    mask = parcel_mask(block)
    acc: np.ndarray | None = None
    used: list[Mapping[str, Any]] = []
    for it in items:
        try:
            stack = reader(it, block)
        except Exception as exc:
            logger.warning("project_cog_item_read_failed", item=it.get("id"), error=str(exc)[:300])
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
    acquired = sorted(str((i.get("properties") or {}).get("datetime")) for i in used)
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
        size = os.path.getsize(tmp)
        storage.upload_file(key, tmp, content_type="image/tiff")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    w, s, e, n = block.bounds_4326()
    x0, y0, x1, y1 = block.bounds
    payload: dict[str, Any] = {
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
        "scene_ids": [str(i.get("id")) for i in used],
        "tiles": sorted({str((i.get("properties") or {}).get("grid:code") or "") for i in used} - {""}),
        "land_ids": list(block.land_ids),
        "parcel_count": len(block.land_ids),
        "valid_pixel_count": valid,
        "mask_pixel_count": int(mask.sum()),
        "mask_buffer_m": mask_buffer_m(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    storage.put_bytes(side, json.dumps(payload, ensure_ascii=False).encode(), content_type="application/json")
    return payload


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
) -> dict[str, Any]:
    """为一个项目在日期区间内（可选限定具体日期）生成全部块的 COG。"""
    storage = storage or _storage()
    wanted = {str(d)[:10] for d in dates} if dates else None
    blocks = build_blocks(parcels)
    stats = {"group_id": group_id, "blocks": len(blocks), "written": 0, "reused": 0, "empty": 0, "failed": 0, "reported": 0}
    for block in blocks:
        try:
            by_date = group_items_by_date(searcher(block.bounds_4326(), date_from, date_to))
        except Exception as exc:
            logger.exception("project_cog_search_failed", group_id=group_id, block=block.key)
            stats["failed"] += 1
            continue
        for obs_date, items in by_date.items():
            if wanted is not None and obs_date not in wanted:
                continue
            try:
                payload = build_block_date(
                    group_id=group_id, block=block, obs_date=obs_date, items=items,
                    storage=storage, force=force, reader=reader,
                )
            except Exception as exc:
                logger.exception("project_cog_build_failed", group_id=group_id, block=block.key, date=obs_date)
                stats["failed"] += 1
                continue
            if payload is None:
                stats["empty"] += 1
                continue
            if payload.get("skipped"):
                stats["reused"] += 1
                continue
            stats["reused" if payload.get("reused") else "written"] += 1
            if report is not None:
                try:
                    report(payload)
                    stats["reported"] += 1
                except Exception as exc:
                    logger.warning("project_cog_report_failed", key=payload.get("oss_key"), error=str(exc)[:300])
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
