"""项目级收获分析 proj_ts_m1_v1（下载机）：读项目 COG（缺失时现场补建）→ 像元规则 → 逐地块统计 →
分级 PNG(EPSG:3857)/COG(UTM) 与 result.json 上传 OSS → 回执 API（kind=project_harvest_result）。

输入由 API ``GET /v1/internal/project-harvest/runs/{run_id}/inputs`` 提供（去重后地块 + 全部地块边界），
本地可用 ``--inputs-json`` 直接给。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from agric_satellite_analysis_common import project_harvest as ph

from app.core import project_harvest as core
from app.core.project_cogs import (
    BAND_NAMES,
    Block,
    build_blocks,
    cog_key,
    parcel_geometry,
)
from app.tasks.project_cogs import (
    LocalDirStorage,
    _report_to_api,
    _storage,
    build_block_date,
    group_items_by_date,
    read_item_stack,
    search_items,
)

from app.core.oss_robust import retry_call, retrying_report, robust

logger = structlog.get_logger()
READ_WORKERS = max(1, int(os.environ.get("PROJECT_HARVEST_READ_WORKERS") or 3))


class CogReadError(RuntimeError):
    """重试后仍有 COG 读取失败：不静默丢日期，整次运行失败。"""

    def __init__(self, failed: Sequence[Mapping[str, Any]]) -> None:
        self.failed = list(failed)
        keys = ", ".join(f"{f['cog_key']} ({f['error']})" for f in self.failed[:20])
        more = f" …共 {len(self.failed)} 个" if len(self.failed) > 20 else ""
        super().__init__(f"project COG read failed after retries: {keys}{more}")


def max_missing_frac(params: Mapping[str, Any]) -> float:
    """每块允许读取失败的日期比例；默认 0 = 任何日期读不到就失败。"""
    v = params.get("max_missing_date_frac", os.environ.get("PROJECT_HARVEST_MAX_MISSING_DATE_FRAC", 0))
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0

RESULT_PREFIX = os.environ.get("PROJECT_HARVEST_PREFIX", "project-harvest")
NEED = ("B02", "B03", "B04", "B08", "B11", "SCL")
BAND_IDX = {b: BAND_NAMES.index(b) + 1 for b in NEED}


def result_prefix(group_id: str, run_id: str) -> str:
    return f"{RESULT_PREFIX.rstrip('/')}/{group_id}/{run_id}"


# ------------------------------------------------------------------ IO helpers
def read_cog_bands_retry(storage: Any, key: str) -> dict[str, np.ndarray]:
    """下载 + 解码整体再重试 1 次（下载本身已在 RobustStorage 内重试；此处兜底半截文件/解码错误）。"""
    return retry_call(lambda: read_cog_bands(storage, key), what="read_cog", attempts=2, key=key)


def read_cog_bands(storage: Any, key: str) -> dict[str, np.ndarray]:
    import rasterio

    fd, tmp = tempfile.mkstemp(suffix=".tif", prefix="ph_cog_")
    os.close(fd)
    try:
        storage.download_file(key, tmp)
        with rasterio.open(tmp) as ds:
            return {b: ds.read(i) for b, i in BAND_IDX.items()}
    finally:
        os.remove(tmp)


def _to_3857(arr: np.ndarray, block: Block, nodata: int) -> tuple[np.ndarray, list[float]]:
    """(bands,H,W) → EPSG:3857 最近邻重投影；返回 (数组, [w,s,e,n] WGS84)。"""
    from pyproj import Transformer
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    x0, y0, x1, y1 = block.bounds
    dst_tr, w, h = calculate_default_transform(block.crs, "EPSG:3857", block.width, block.height, x0, y0, x1, y1)
    out = np.full((arr.shape[0], h, w), nodata, arr.dtype)
    for i in range(arr.shape[0]):
        reproject(
            arr[i], out[i], src_transform=block.transform, src_crs=block.crs, dst_transform=dst_tr,
            dst_crs="EPSG:3857", resampling=Resampling.nearest, src_nodata=nodata, dst_nodata=nodata,
        )
    mx0, my1 = dst_tr.c, dst_tr.f
    mx1, my0 = mx0 + dst_tr.a * w, my1 + dst_tr.e * h
    back = Transformer.from_crs(3857, 4326, always_xy=True).transform
    west, south = back(mx0, my0)
    east, north = back(mx1, my1)
    return out, [west, south, east, north]


def _png(rgba: np.ndarray) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


_PALETTE = np.zeros((256, 4), np.uint8)
for _t in ph.TIERS:
    _c = _t["color"].lstrip("#")
    _PALETTE[_t["code"]] = [int(_c[0:2], 16), int(_c[2:4], 16), int(_c[4:6], 16), 255]


def tier_png(tier: np.ndarray, block: Block) -> tuple[bytes, list[float], tuple[int, int]]:
    out, b4326 = _to_3857(tier[None], block, ph.OUTSIDE)
    return _png(_PALETTE[out[0]]), b4326, out.shape[1:]


def rgb_png(rgba: np.ndarray, block: Block) -> tuple[bytes, list[float], tuple[int, int]]:
    out, b4326 = _to_3857(np.moveaxis(rgba, 2, 0), block, 0)
    return _png(np.moveaxis(out, 0, 2)), b4326, out.shape[1:]


def tier_cog(path: str, tier: np.ndarray, block: Block) -> None:
    from rasterio.io import MemoryFile
    from rio_cogeo.cogeo import cog_translate
    from rio_cogeo.profiles import cog_profiles

    prof = {"driver": "GTiff", "dtype": "uint8", "count": 1, "height": tier.shape[0], "width": tier.shape[1],
            "crs": block.crs, "transform": block.transform, "nodata": ph.OUTSIDE}
    with MemoryFile() as mem:
        with mem.open(**prof) as ds:
            ds.write(tier[None])
        with mem.open() as src:
            cog_translate(src, path, {**cog_profiles.get("deflate"), "blockxsize": 256, "blockysize": 256},
                          overview_resampling="nearest", in_memory=True, quiet=True, add_mask=False)


# ------------------------------------------------------------------ core run
def run_block(
    *,
    group_id: str,
    run_id: str,
    block: Block,
    parcels: Sequence[Mapping[str, Any]],
    season_from: date,
    season_to: date,
    params: Mapping[str, Any],
    storage: Any,
    report: Callable[[dict], Any] | None,
    searcher: Callable[..., list[dict]] = search_items,
    reader: Callable = read_item_stack,
    calib: tuple | None = None,
    mask_geojsons: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """一个计算块：返回 {block, scenes, valid_dates, thresholds, stats, rasters}。"""
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform as stf

    H, W = block.height, block.width
    to = Transformer.from_crs(4326, block.crs, always_xy=True).transform
    weights: dict[str, tuple[int, int, np.ndarray]] = {}
    FR = np.zeros((H, W), np.float32)
    geoms_utm = []
    for p in parcels:
        g_utm = stf(to, shape(p["clipped_geojson"]))
        geoms_utm.append(g_utm)
        res = core.parcel_weights(g_utm, block.x0, block.y1, H, W)
        if res is None:
            continue
        weights[str(p["land_id"])] = res
        r0, c0, w = res
        FR[r0 : r0 + w.shape[0], c0 : c0 + w.shape[1]] += w
    FR = np.clip(FR, 0, 1)
    inside = FR > 0
    # 观测掩膜：默认只取像元中心落在地块内的像元（与原型一致）；中心在外的边缘像元无观测，归入未种植/非耕地
    # 掩膜几何用块内全部原始边界（含被合并地块，与原型 AOI 一致），缺省用去重后几何
    mgeoms = [stf(to, g) for g in (parcel_geometry(m) for m in (mask_geojsons or [])) if g is not None] or geoms_utm
    obs_mask = core.center_mask(mgeoms, block.transform, (H, W)) & inside if params.get("clear_center_only", True) else inside
    n_in = int(obs_mask.sum())
    info: dict[str, Any] = {"key": block.key, "crs": block.crs, "x0": block.x0, "y1": block.y1, "w": W, "h": H,
                            "bbox4326": list(block.bounds_4326()), "parcel_count": len(weights)}
    if not n_in:
        return {"block": info, "scenes": [], "valid_dates": [], "thresholds": {}, "stats": [], "rasters": []}

    by_date = group_items_by_date(searcher(block.bounds_4326(), season_from, season_to))
    scenes, dates, B4s, B8s, B11s, CLs, rgbs = [], [], [], [], [], [], []
    failed: list[dict[str, Any]] = []
    readable: list[tuple[str, dict[str, Any]]] = []
    # 1) 确保 COG 存在（缺失时现场补建）
    for d in sorted(by_date):
        key = cog_key(group_id, block.key, d)
        rec: dict[str, Any] = {"date": d, "cog_key": key, "scene_ids": [str(i.get("id")) for i in by_date[d]]}
        try:
            if not storage.exists(key):
                payload = build_block_date(group_id=group_id, block=block, obs_date=d, items=by_date[d],
                                           storage=storage, reader=reader)
                rec["cog_built"] = True
                if payload is None:
                    rec.update(valid=False, note="no_data")
                    scenes.append(rec)
                    continue
                if report is not None:
                    try:
                        report(payload)
                    except Exception as exc:
                        logger.warning("project_harvest_cog_report_failed", key=key, exc_type=type(exc).__name__,
                                       error=str(exc)[:200])
        except Exception as exc:
            logger.error("project_harvest_cog_failed", key=key, stage="build", exc_type=type(exc).__name__,
                         error=str(exc)[:300])
            rec.update(valid=False, note="read_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            scenes.append(rec)
            failed.append(rec)
            continue
        readable.append((d, rec))

    # 2) 有界并行读取（每个 COG 自带重试）；按日期顺序消费，在途数受限以控内存
    def _read(key: str):
        try:
            return read_cog_bands_retry(storage, key), None
        except Exception as exc:
            return None, exc

    workers = max(1, int(params.get("read_workers") or READ_WORKERS))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ph-read") as pool:
        futs = {}
        nxt = 0
        for i, (d, rec) in enumerate(readable):
            while nxt < len(readable) and nxt < i + workers:
                futs[nxt] = pool.submit(_read, readable[nxt][1]["cog_key"])
                nxt += 1
            b, exc = futs.pop(i).result()
            if exc is not None:
                logger.error("project_harvest_cog_failed", key=rec["cog_key"], stage="read", exc_type=type(exc).__name__,
                             error=str(exc)[:300])
                rec.update(valid=False, note="read_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
                scenes.append(rec)
                failed.append(rec)
                continue
            clear = core.clear_mask(b["B02"], b["B08"], b["B11"], b["SCL"], params) & obs_mask
            frac = float(clear.sum()) / n_in
            rec["clear_frac"] = round(frac, 3)
            rec["valid"] = frac >= float(params["valid_frac"])
            scenes.append(rec)
            if not rec["valid"]:
                continue
            dates.append(d)
            B4s.append(core.refl(b["B04"], clear))
            B8s.append(core.refl(b["B08"], clear))
            B11s.append(core.refl(b["B11"], clear))
            CLs.append(clear)
            rgbs.append(core.rgb_stretch(b["B04"], b["B03"], b["B02"], inside))
            del b
    scenes.sort(key=lambda r: r["date"])
    if failed:
        total = len(by_date)
        frac_missing = len(failed) / max(1, total)
        info["missing_dates"] = [f["date"] for f in failed]
        if frac_missing > max_missing_frac(params):
            raise CogReadError(failed)
        logger.warning("project_harvest_dates_missing_tolerated", block=block.key, missing=len(failed), total=total)
    info["dates"] = dates
    if len(dates) < 2:
        return {"block": info, "scenes": scenes, "valid_dates": dates, "thresholds": {}, "stats": [], "rasters": [],
                "note": "not_enough_valid_dates"}
    CL = np.stack(CLs)
    NDVI, score, thr, sep = core.m1_score(np.stack(B4s), np.stack(B8s), np.stack(B11s), CL, calib=calib)
    del B4s, B8s, B11s
    early = f"{season_from.year}-{params['early_cutoff']}"
    out = core.classify(dates, NDVI, CL, score, FR, params, early)
    tiers = out["tiers"]

    meta = {str(p["land_id"]): p for p in parcels}
    stats = []
    for lid, (r0, c0, w) in weights.items():
        by = {d: ph.parcel_date_row(core.tier_sums(tiers[t], r0, c0, w), float(w.sum()), float(meta[lid]["clipped_area_mu"]))
              for t, d in enumerate(dates)}
        pick = ph.choose_pick(by, float(params["pick_cloud_max"]))
        with np.errstate(invalid="ignore"):
            sub = out["pk_early"][r0 : r0 + w.shape[0], c0 : c0 + w.shape[1]]
            sel = sub[(w > 0.5) & np.isfinite(sub)]
        pkmed = float(np.median(sel)) if sel.size else None
        for d, row in by.items():
            stats.append({"land_id": lid, "obs_date": d, "block_key": block.key, "weight_px": float(w.sum()),
                          "is_pick": d == pick, "pick_date": pick, "peak_ndvi_median": pkmed,
                          **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items()}})

    rasters = []
    pre = result_prefix(group_id, run_id)
    for t, d in enumerate(dates):
        png, b4326, (ph_, pw_) = tier_png(tiers[t], block)
        k = f"{pre}/{block.key}/{d}_tier.png"
        storage.put_bytes(k, png, content_type="image/png")
        rasters.append({"block_key": block.key, "obs_date": d, "kind": "tier", "format": "png", "oss_key": k,
                        "crs": "EPSG:3857", "width": pw_, "height": ph_, "bounds_4326": b4326})
        png, b4326, (ph_, pw_) = rgb_png(rgbs[t], block)
        k = f"{pre}/{block.key}/{d}_rgb.png"
        storage.put_bytes(k, png, content_type="image/png")
        rasters.append({"block_key": block.key, "obs_date": d, "kind": "rgb", "format": "png", "oss_key": k,
                        "crs": "EPSG:3857", "width": pw_, "height": ph_, "bounds_4326": b4326})
        fd, tmp = tempfile.mkstemp(suffix=".tif")
        os.close(fd)
        try:
            tier_cog(tmp, tiers[t], block)
            k = f"{pre}/{block.key}/{d}_tier.tif"
            storage.upload_file(k, tmp, content_type="image/tiff")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        rasters.append({"block_key": block.key, "obs_date": d, "kind": "tier", "format": "cog", "oss_key": k,
                        "crs": block.crs, "width": W, "height": H, "bounds_4326": list(block.bounds_4326())})
    if calib is not None:
        info["calib_from"] = calib[2] if len(calib) > 2 else True
    return {"block": info, "scenes": scenes, "valid_dates": dates, "calib": (thr, sep),
            "thresholds": {k: round(float(v), 4) for k, v in thr.items()}, "stats": stats, "rasters": rasters}


def _bbox_gap(a: Sequence[float], b: Sequence[float]) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return (dx * dx + dy * dy) ** 0.5


def run(
    inputs: Mapping[str, Any],
    *,
    storage: Any = None,
    report: Callable[[dict], Any] | None = _report_to_api,
    searcher: Callable[..., list[dict]] = search_items,
    reader: Callable = read_item_stack,
) -> dict[str, Any]:
    """整次运行；上传 result.json，返回回执载荷。"""
    storage = robust(storage or _storage())
    report = retrying_report(report)
    t0 = time.time()
    run_id = str(inputs["run_id"])
    group_id = str(inputs["group_id"])
    params = ph.merge_params(inputs.get("params"))
    d0 = date.fromisoformat(str(inputs["season_from"]))
    d1 = date.fromisoformat(str(inputs["season_to"]))
    parcels = [p for p in inputs["parcels"] if p.get("clipped_geojson")]
    blocks = build_blocks(inputs["lands"])
    lands_geo = {str(x["land_id"]): x["boundary_geojson"] for x in inputs["lands"] if x.get("boundary_geojson")}
    by_lid = {str(p["land_id"]): p for p in parcels}
    out_blocks, scenes, stats, rasters, thresholds = [], {}, [], [], {}
    assigned: set[str] = set()
    todo = []
    for block in blocks:
        mine = [by_lid[x] for x in block.land_ids if x in by_lid and x not in assigned]
        if mine:
            assigned.update(str(p["land_id"]) for p in mine)
            todo.append((block, mine))
    # 地块少的块伪样本不可靠：先算大块，小块借用最近大块的阈值标定
    min_n = int(params.get("calib_min_parcels", 10))
    todo.sort(key=lambda t: len(t[1]) < min_n)
    calibs: list[tuple[Any, tuple[dict, dict]]] = []
    for block, mine in todo:
        calib = None
        if len(mine) < min_n and calibs:
            bb = block.bounds_4326()
            src, c = min(calibs, key=lambda x: _bbox_gap(bb, x[0].bounds_4326()))
            calib = (c[0], c[1], src.key)
        masks = [lands_geo[x] for x in block.land_ids if x in lands_geo]
        r = run_block(group_id=group_id, run_id=run_id, block=block, parcels=mine, season_from=d0, season_to=d1,
                      params=params, storage=storage, report=report, searcher=searcher, reader=reader, calib=calib,
                      mask_geojsons=masks)
        if calib is None and r.get("calib") and len(mine) >= min_n:
            calibs.append((block, r["calib"]))
        out_blocks.append({**r["block"], **({"note": r["note"]} if r.get("note") else {})})
        scenes[block.key] = r["scenes"]
        thresholds[block.key] = r["thresholds"]
        stats.extend(r["stats"])
        rasters.extend(r["rasters"])
        logger.info("project_harvest_block_done", run_id=run_id, block=block.key, dates=len(r["valid_dates"]))
    valid_dates = sorted({d for b in out_blocks for d in b.get("dates", [])})
    result = {
        "kind": "project_harvest_result_v1",
        "method_version": ph.METHOD_VERSION,
        "run_id": run_id,
        "group_id": group_id,
        "season_from": d0.isoformat(),
        "season_to": d1.isoformat(),
        "params": params,
        "thresholds": thresholds,
        "blocks": out_blocks,
        "valid_dates": valid_dates,
        "scenes": scenes,
        "stats": stats,
        "rasters": rasters,
        "unassigned_land_ids": sorted(set(by_lid) - assigned),
        "missing_dates": {b["key"]: b["missing_dates"] for b in out_blocks if b.get("missing_dates")},
        "elapsed_s": round(time.time() - t0, 1),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    key = f"{result_prefix(group_id, run_id)}/result.json"
    storage.put_bytes(key, json.dumps(result, ensure_ascii=False).encode(), content_type="application/json")
    return {"kind": "project_harvest_result", "run_id": run_id, "group_id": group_id, "status": "succeeded",
            "result_oss_key": key, "stat_rows": len(stats), "valid_dates": valid_dates}


def _inputs(run_id: str) -> dict:
    from agric_satellite_analysis_common.internal_api import project_harvest_inputs

    return project_harvest_inputs(run_id)


# ------------------------------------------------------------------ celery
from app.worker import celery_app  # noqa: E402


@celery_app.task(name="app.tasks.project_harvest.run_project_harvest", acks_late=True,
                 time_limit=3 * 3600, soft_time_limit=3 * 3600 - 300)
def run_project_harvest(run_id: str) -> dict:
    """API 派发：拉取输入 → 计算 → 回执。失败回执 status=failed。"""
    try:
        _report_to_api({"kind": "project_harvest_result", "run_id": run_id, "status": "running"})
    except Exception as exc:
        logger.warning("project_harvest_running_report_failed", run_id=run_id, error=str(exc)[:200])
    try:
        payload = run(_inputs(run_id))
    except CogReadError as exc:
        logger.error("project_harvest_failed", run_id=run_id, reason="cog_read_failed",
                     failed_keys=[f["cog_key"] for f in exc.failed])
        payload = {"kind": "project_harvest_result", "run_id": run_id, "status": "failed", "error": str(exc)[:2000]}
    except Exception as exc:
        logger.exception("project_harvest_failed", run_id=run_id)
        payload = {"kind": "project_harvest_result", "run_id": run_id, "status": "failed", "error": str(exc)[:2000]}
    retrying_report(_report_to_api)(payload)
    return {k: v for k, v in payload.items() if k != "valid_dates"}


def _cli(argv: Sequence[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description="项目级收获分析（本地）")
    ap.add_argument("--inputs-json", required=True, help="与 internal inputs 接口同结构的 JSON")
    ap.add_argument("--local-dir", required=True, help="本地伪 OSS 目录")
    a = ap.parse_args(argv)
    inputs = json.loads(Path(a.inputs_json).read_text())
    return run(inputs, storage=LocalDirStorage(a.local_dir), report=None)


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(_cli(), ensure_ascii=False, indent=1))
