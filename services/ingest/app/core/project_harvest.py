"""项目级收获分析 proj_ts_m1_v1 的像元规则（下载机计算，纯 numpy/rasterio，无 IO）。

与原型 analysis/group2878（hs_group.py + dedup_status_ts/ts_status_report.py）一致：
晴空/去雾掩膜 → 有效日期 → 伪样本 Otsu 阈值 → M1 多波段得分 → 时序收获判定 →
分级 → 团块清理 → 覆盖比例加权。
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from agric_satellite_analysis_common.project_harvest import OUTSIDE, TIER_KEYS

REFL_SCALE = 1e-4  # COG 中 uint16 = 反射率 × 10000


def nd(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return (a - b) / (a + b)


def otsu_mid(a: np.ndarray, b: np.ndarray) -> float:
    """两组样本的分割阈值：合并样本 Otsu，偏离两中位数中间 50% 区间时退回中点。"""
    v = np.concatenate([a, b])
    v = v[np.isfinite(v)]
    if v.size < 10:
        return float((np.nanmedian(a) + np.nanmedian(b)) / 2)
    h, e = np.histogram(v, 128)
    c = (e[:-1] + e[1:]) / 2
    w = h / h.sum()
    w0 = np.cumsum(w)
    m0 = np.cumsum(w * c)
    mt = m0[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        s = (mt * w0 - m0) ** 2 / (w0 * (1 - w0))
    t = float(c[np.nanargmax(s)])
    mid = float((np.nanmedian(a) + np.nanmedian(b)) / 2)
    lo_, hi_ = sorted([np.nanmedian(a), np.nanmedian(b)])
    g = hi_ - lo_
    if not (lo_ + 0.25 * g <= t <= hi_ - 0.25 * g):
        return mid
    return t if lo_ < t < hi_ else float((lo_ + hi_) / 2)


def clear_mask(b02: np.ndarray, b08: np.ndarray, b11: np.ndarray, scl: np.ndarray, params: Mapping[str, Any]) -> np.ndarray:
    """uint16 波段（0=无数据）→ 晴空：SCL∈clear_scl、B08/B11 有值、B02 不超过去雾阈值。"""
    clear = np.isin(scl, list(params["clear_scl"]))
    clear &= (b08 > 0) & (b11 > 0)
    clear &= ~(b02.astype(np.float32) * REFL_SCALE > float(params["haze_b02"]))
    return clear


def refl(a: np.ndarray, valid: np.ndarray) -> np.ndarray:
    out = a.astype(np.float32) * np.float32(REFL_SCALE)
    out[~valid] = np.nan
    return out


def m1_score(
    B4: np.ndarray, B8: np.ndarray, B11: np.ndarray, CL: np.ndarray, calib: tuple | None = None
) -> tuple[np.ndarray, np.ndarray, dict, dict]:
    """返回 (NDVI, score, thresholds, separation)；输入 (T,H,W) 反射率，~CL 处为 NaN。

    ``calib`` 给定 (thresholds, separation) 时不再用本块伪样本标定（小块借用邻块标定）。"""
    NDVI, NBR, BSI = nd(B8, B4), nd(B8, B11), nd(B11, B4)
    for X in (NDVI, NBR, BSI):
        X[~CL] = np.nan
    feats = {"NDVI": NDVI, "NBR": NBR, "BSI": BSI}
    with np.errstate(invalid="ignore"):
        n0 = NDVI[0]
        vm = n0 >= np.nanpercentile(n0, 80) if np.isfinite(n0).any() else np.zeros_like(n0, bool)
        nl = NDVI[-1]
        bm = nl <= np.nanpercentile(nl, 20) if np.isfinite(nl).any() else np.zeros_like(nl, bool)
    if calib is not None:
        thr, sep = dict(calib[0]), dict(calib[1])
    else:
        veg = {k: F[0][vm] for k, F in feats.items()}
        bare = {k: F[-1][bm] for k, F in feats.items()}
        thr = {k: otsu_mid(veg[k], bare[k]) for k in feats}
        sep = {k: float(np.nanmedian(veg[k]) - np.nanmedian(bare[k])) for k in feats}
    B8max = np.fmax.accumulate(np.where(np.isnan(B8), -np.inf, B8), axis=0)
    B8max[np.isinf(B8max)] = np.nan
    B4min = np.fmin.accumulate(np.where(np.isnan(B4), np.inf, B4), axis=0)
    B11min = np.fmin.accumulate(np.where(np.isnan(B11), np.inf, B11), axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        c_b8 = B8 < 0.70 * B8max
        c_soil = (B4 > B4min * 1.15) & (B11 > B11min * 1.10)
        c_nbr = (NBR < thr["NBR"]) if sep["NBR"] > 0 else (NBR > thr["NBR"])
        c_bsi = (BSI < thr["BSI"]) if sep["BSI"] > 0 else (BSI > thr["BSI"])
    score = ((c_b8.astype(np.float32) + c_soil + c_nbr + c_bsi) / 4.0).astype(np.float32)
    return NDVI, score, thr, sep


def small_clumps(mask: np.ndarray, min_px: int) -> np.ndarray:
    """8 邻域连通块像元数 < min_px 的像元（不依赖 scipy）。"""
    if min_px <= 1 or not mask.any():
        return np.zeros_like(mask, bool)
    from affine import Affine
    from rasterio.features import rasterize, shapes
    from shapely.geometry import shape

    small = []
    for geom, _ in shapes(mask.astype(np.uint8), mask=mask, connectivity=8, transform=Affine.identity()):
        if shape(geom).area < min_px - 0.5:
            small.append(geom)
    if not small:
        return np.zeros_like(mask, bool)
    return rasterize(small, out_shape=mask.shape, transform=Affine.identity()).astype(bool) & mask


def center_mask(geoms: Sequence[Any], transform: Any, shape: tuple[int, int]) -> np.ndarray:
    """像元中心落在任一几何内的掩膜（rasterize 默认规则）。"""
    from rasterio.features import rasterize
    from shapely.geometry import mapping

    gs = [mapping(g) for g in geoms if g is not None and not g.is_empty]
    if not gs:
        return np.zeros(shape, bool)
    return rasterize(gs, out_shape=shape, transform=transform).astype(bool)


def classify(
    dates: Sequence[str],
    NDVI: np.ndarray,
    CL: np.ndarray,
    score: np.ndarray,
    FR: np.ndarray,
    params: Mapping[str, Any],
    early_cutoff: str,
) -> dict[str, Any]:
    """时序约束分级。返回 tiers (T,H,W) uint8（0..5，地块外 255）及耕种/峰值。"""
    T, H, W = NDVI.shape
    inside = FR > 0
    green, low_thr, drop, stay = (float(params[k]) for k in ("green_thr", "low_thr", "drop_min", "stay_thr"))
    early = [i for i, d in enumerate(dates) if d <= early_cutoff]
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        ndc = np.where(CL, NDVI, np.nan)
        pk_e = np.nanmax(ndc[early], 0) if early else np.full((H, W), np.nan, np.float32)
        pk_all = np.nanmax(ndc, 0)
        crop = (pk_e >= green) | (np.isnan(pk_e) & (pk_all >= green))
        low = CL & (NDVI < low_thr) & ((pk_e[None] - NDVI) >= drop)
        high = CL & (NDVI >= stay)
    tidx = np.arange(T)[:, None, None]
    last_high = np.where(high, tidx, -1).max(0)
    after = low & (tidx > last_high[None])
    first_low = np.where(after, tidx, T).min(0)
    h = np.where((first_low < T) & inside & crop, first_low, -1)
    hidx = np.clip(h, 0, T - 1)
    rr, cc = np.indices((H, W))
    sc = score[hidx, rr, cc]
    with np.errstate(invalid="ignore"):
        dr = pk_e - NDVI[hidx, rr, cc]
    strong = (sc >= float(params["hi_score"])) & (dr >= float(params["hi_drop"]))
    tiers = np.full((T, H, W), OUTSIDE, np.uint8)
    nlow = np.zeros((H, W), np.int16)
    for t in range(T):
        has = (h >= 0) & (h <= t)
        nlow += (low[t] & (t >= h) & has).astype(np.int16)
        conf = has & (nlow >= 2)
        prov = has & (nlow == 1)
        hi = conf & strong & (FR >= float(params["edge_min_frac"]))
        mid = (conf & ~hi) | (prov & strong)
        lo = prov & ~mid
        small = small_clumps(hi | mid, int(params["min_clump_px"]))
        hi &= ~small
        mid &= ~small
        lo |= small
        tier = tiers[t]
        tier[inside] = 3
        tier[lo & inside] = 2
        tier[mid & inside] = 1
        tier[hi & inside] = 0
        tier[inside & ~CL[t] & ~(hi | mid | lo)] = 4
        tier[inside & ~crop] = 5
    return {"tiers": tiers, "crop": crop, "pk_early": pk_e, "harvest_idx": h}


def parcel_weights(geom_utm: Any, x0: float, y1: float, H: int, W: int, res: float = 10.0, ss: int = 5):
    """地块覆盖比例（ss× 超采样）。返回 (r0, c0, w2d) 只覆盖地块所在窗口；不相交返回 None。"""
    from rasterio.features import rasterize
    from rasterio.transform import from_origin
    from shapely.geometry import mapping

    bx0, by0, bx1, by1 = geom_utm.bounds
    c0 = max(int(np.floor((bx0 - x0) / res)), 0)
    c1 = min(int(np.ceil((bx1 - x0) / res)), W)
    r0 = max(int(np.floor((y1 - by1) / res)), 0)
    r1 = min(int(np.ceil((y1 - by0) / res)), H)
    if c1 <= c0 or r1 <= r0:
        return None
    hh, ww = r1 - r0, c1 - c0
    tr = from_origin(x0 + c0 * res, y1 - r0 * res, res / ss, res / ss)
    m = rasterize([mapping(geom_utm)], out_shape=(hh * ss, ww * ss), transform=tr).astype(np.float32)
    w = m.reshape(hh, ss, ww, ss).mean((1, 3))
    if not (w > 0).any():
        return None
    return r0, c0, w


def tier_sums(tier: np.ndarray, r0: int, c0: int, w: np.ndarray) -> dict[str, float]:
    sub = tier[r0 : r0 + w.shape[0], c0 : c0 + w.shape[1]]
    return {k: float((w * (sub == code)).sum()) for code, k in enumerate(TIER_KEYS)}


def rgb_stretch(b04: np.ndarray, b03: np.ndarray, b02: np.ndarray, inside: np.ndarray) -> np.ndarray:
    """真彩色 2–98% 拉伸 → (H,W,4) uint8，无数据透明。"""
    im = np.dstack([b04, b03, b02]).astype(np.float32)
    valid = (im > 0).all(2)
    sel = im[inside & valid]
    if sel.size == 0:
        return np.zeros(im.shape[:2] + (4,), np.uint8)
    lo, hi = np.percentile(sel, 2), np.percentile(sel, 98)
    out = np.clip((im - lo) / max(hi - lo, 1e-6), 0, 1)
    rgba = np.zeros(im.shape[:2] + (4,), np.uint8)
    rgba[..., :3] = (out * 255).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 255, 0)
    return rgba
