"""冬小麦秋季出苗：像元土壤本底 + 本块噪声上的持续抬升。

判定只用 NDVI 相对本块早季低平台的增量。NDVI 0.30 是产品参考刻度，
只统计对照比例，不当本底、不当阈值。噪声取该平台残差绝对值的 P99，
写入结果，不把某一次试算的 0.035 或文献里的 NDGI+0.04 当成全局规则。

春季返青不在本模块的日历窗内，不会被记为出苗。
已出苗是字段状态，不是出苗日、缺苗率或苗数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import json
from typing import Any

import numpy as np

METHOD_VERSION = "ww_autumn_soil_baseline_v1"
BASELINE_STATISTIC = "per-pixel median of NDVI on early clear low-plateau dates"
NOISE_STATISTIC = (
    "P99 of absolute NDVI residuals on the low-plateau dates "
    "after robust spike exclusion"
)

STATUS_NOT_EMERGED = "not_emerged"
STATUS_EMERGING = "emerging"
STATUS_EMERGED = "emerged"
STATUS_UNCERTAIN = "uncertain"

STATUS_ZH = {
    STATUS_NOT_EMERGED: "未出苗",
    STATUS_EMERGING: "出苗中",
    STATUS_EMERGED: "已出苗",
    STATUS_UNCERTAIN: "不确定",
}

CLASS_WITHIN = 0
CLASS_ABOVE = 1
CLASS_SUSTAINED = 2

CLASS_LABELS_ZH = {
    "0": "未明显高于本底",
    "1": "高于本底噪声但尚未持续",
    "2": "持续高于本底噪声",
}

# 字段比例是“这块地大部分像元 / 一部分像元”的分类政策，不是 NDVI 截止值。
EMERGED_SHARE = 0.80
EMERGING_SHARE = 0.25
# 平台至少两个晴空日，抬升至少再有两个晴空日才允许冻结为已出苗。
MIN_PLATEAU_DATES = 2
MIN_POST_DATES_FOR_EMERGED = 2
MIN_PIXELS = 20
MIN_CLEAR_FRACTION = 0.50
# 只剔除相对残差中位数极远的尖峰，避免个别亮像元抬高 P99。不是植被阈值。
SPIKE_MAD_K = 8.0
# 前两日若是一次整体抬升（而不是像元噪声），就不能当成土壤平台。
SHIFT_SIGMA_K = 3.0
# 整块没有任何空间/时间散度时的退化保护，只用于“前两日是不是同一次观测水平”。
# 不是出苗阈值；出苗阈值仍是本块残差 P99。
FLAT_FIELD_SHIFT = 0.02
# 产品色标上的参考刻度。只用于 share_ndvi_ge_0_30，不进入判定分支。
NDVI_CONTEXT_MARK = 0.30

DISCLAIMER_ZH = (
    "相对该地块秋季晴空低平台的土壤本底判断持续抬升，"
    "不是精确出苗日、缺苗率或苗数。"
    "NDVI 0.30 只作为对照比例，不参与出苗判定。"
)
RULE_ZH = (
    "用播种后晴空低平台各像元 NDVI 中位数作为土壤本底，"
    "噪声为该平台残差绝对值的第99百分位（剔除极端尖峰）。"
    "之后至少两个晴空日、多数像元持续高于该地块噪声，记为已出苗并冻结；"
    "仅部分像元或尚无连续确认日记为出苗中；"
    "仍在噪声内记为未出苗；日期不足或信号回落记为不确定。"
    "只用于秋季出苗，不把返青当作出苗。"
)

OSS_KIND = "autumn_emergence"
DAILY_FIELD_KEY = "autumn_emergence"


def autumn_season_key(day: date) -> str | None:
    """播种年秋季窗：Y 年 9 月 1 日到 Y+1 年 1 月 31 日 → ``{Y}-autumn``。

    2 月到 8 月是返青和主生长季，返回 None，调用方不得把它当出苗。
    """
    if day.month >= 9:
        return f"{day.year}-autumn"
    if day.month == 1:
        return f"{day.year - 1}-autumn"
    return None


def default_season_key(day: date) -> str:
    """查询默认季：窗内用当季，2–8 月用刚刚结束的那个秋季。"""
    key = autumn_season_key(day)
    if key:
        return key
    return f"{day.year - 1}-autumn"


def season_bounds(season_key: str) -> tuple[date, date]:
    year = int(str(season_key).split("-", 1)[0])
    return date(year, 9, 1), date(year + 1, 1, 31)


def is_winter_wheat(crop_type: str | None) -> bool:
    """冬小麦可算秋季出苗。春小麦、空作物和其他作物不算。"""
    if crop_type is None:
        return False
    raw = str(crop_type).strip()
    if not raw:
        return False
    low = raw.lower().replace(" ", "_").replace("-", "_")
    if "春" in raw or "spring" in low:
        return False
    if low in {"wheat", "winter_wheat"} or raw in {"小麦", "冬小麦"}:
        return True
    try:
        from app.core.crops import normalize_crop_key
    except ImportError:
        return "wheat" in low or "小麦" in raw
    return normalize_crop_key(raw) == "wheat"


def pixel_class(
    increment: float,
    previous: float | None,
    *,
    noise: float,
    can_sustain: bool,
) -> int:
    """0 噪声内，1 当日高出但未连续，2 与前一个抬升日都高出噪声。"""
    if increment <= noise:
        return CLASS_WITHIN
    if can_sustain and previous is not None and previous > noise:
        return CLASS_SUSTAINED
    return CLASS_ABOVE


@dataclass
class DateStack:
    """一个晴空日、已经筛过 clear 的像元列。"""

    day: date
    lon: np.ndarray
    lat: np.ndarray
    ndvi: np.ndarray
    ndre: np.ndarray | None = None


@dataclass
class EmergenceResult:
    status: str
    reason: str
    season_key: str | None
    emerged_flag: bool
    baseline_dates: list[date]
    noise_floor: float | None
    as_of_date: date | None
    first_emerged_date: date | None
    n_clear: int
    n_pixels: int
    median_increment: float | None
    share_above_noise: float | None
    share_sustained: float | None
    share_ndvi_ge_0_30: float | None
    ndre_share_above_noise: float | None
    timeseries: list[dict[str, Any]]
    summary: dict[str, Any]
    obs_dates: list[date] = field(default_factory=list)
    n_baseline_dates: int = 0
    lon: np.ndarray | None = None
    lat: np.ndarray | None = None
    baseline_ndvi: np.ndarray | None = None
    ndvi: np.ndarray | None = None
    classes: np.ndarray | None = None

    @property
    def status_zh(self) -> str:
        return STATUS_ZH[self.status]

    def to_payload(self, land_id: str, season_key: str) -> dict[str, Any] | None:
        """像元对象。无有效像元时返回 None，调用方不上传。"""
        if (
            self.lon is None
            or self.lat is None
            or self.baseline_ndvi is None
            or self.ndvi is None
            or self.classes is None
            or self.n_pixels == 0
        ):
            return None
        ndvi_rows: list[list[float | None]] = []
        keep = self.classes >= 0
        for i in np.flatnonzero(keep):
            row: list[float | None] = []
            for j in range(self.ndvi.shape[1]):
                value = self.ndvi[i, j]
                row.append(None if not np.isfinite(value) else round(float(value), 6))
            ndvi_rows.append(row)
        return {
            "kind": OSS_KIND,
            "format": "emergence_lonlat_v1",
            "land_id": str(land_id),
            "season_key": season_key,
            "method_version": METHOD_VERSION,
            "as_of_date": _iso(self.as_of_date),
            "baseline_dates": [_iso(d) for d in self.baseline_dates],
            "n_baseline_dates": int(self.n_baseline_dates),
            "obs_dates": [_iso(d) for d in self.obs_dates],
            "noise_floor": self.noise_floor,
            "noise_statistic": NOISE_STATISTIC,
            "baseline_statistic": BASELINE_STATISTIC,
            "class_labels": dict(CLASS_LABELS_ZH),
            "disclaimer_zh": DISCLAIMER_ZH,
            "columns": {
                "lon": [round(float(v), 6) for v in self.lon[keep]],
                "lat": [round(float(v), 6) for v in self.lat[keep]],
                "baseline_ndvi": [round(float(v), 6) for v in self.baseline_ndvi[keep]],
                "class": [int(v) for v in self.classes[keep]],
                "ndvi": ndvi_rows,
            },
        }

    def to_daily_field(
        self, land_id: str, *, sowing_date: date | None = None
    ) -> dict[str, Any]:
        """One official date's emergence result, stored on that day's product JSON.

        Per-pixel arrays are for this as-of date only. The caller must have
        computed this result from all clear official dates from sowing through
        this day, not from this day's NDVI alone.
        """
        field: dict[str, Any] = {
            "kind": DAILY_FIELD_KEY,
            "method_version": METHOD_VERSION,
            "land_id": str(land_id),
            "season_key": self.season_key,
            "sowing_date": _iso(sowing_date),
            "as_of_date": _iso(self.as_of_date),
            "status": self.status,
            "status_zh": self.status_zh,
            "emerged_flag": bool(self.emerged_flag),
            "reason": self.reason,
            "baseline_dates": [_iso(d) for d in self.baseline_dates],
            "n_baseline_dates": int(self.n_baseline_dates),
            "obs_dates": [_iso(d) for d in self.obs_dates],
            "noise_floor": self.noise_floor,
            "noise_statistic": NOISE_STATISTIC if self.noise_floor is not None else None,
            "baseline_statistic": BASELINE_STATISTIC if self.baseline_dates else None,
            "first_emerged_date": _iso(self.first_emerged_date),
            "n_clear": int(self.n_clear),
            "n_pixels": int(self.n_pixels),
            "median_increment": self.median_increment,
            "share_above_noise": self.share_above_noise,
            "share_sustained": self.share_sustained,
            "share_ndvi_ge_0_30": self.share_ndvi_ge_0_30,
            "ndre_share_above_noise": self.ndre_share_above_noise,
            "class_labels": dict(CLASS_LABELS_ZH),
            "disclaimer_zh": DISCLAIMER_ZH,
            "summary": dict(self.summary or {}),
        }
        point = next(
            (row for row in self.timeseries if row.get("date") == _iso(self.as_of_date)),
            None,
        )
        if point:
            field["in_baseline"] = bool(point.get("in_baseline"))
            field["median_ndvi"] = point.get("median_ndvi")
        elif self.as_of_date is not None and self.n_baseline_dates:
            field["in_baseline"] = self.as_of_date in self.baseline_dates
        if (
            self.lon is not None
            and self.lat is not None
            and self.baseline_ndvi is not None
            and self.ndvi is not None
            and self.classes is not None
            and self.n_pixels > 0
        ):
            keep = self.classes >= 0
            as_of_idx = self.ndvi.shape[1] - 1
            ndvi_today = self.ndvi[:, as_of_idx]
            increment = ndvi_today - self.baseline_ndvi
            field["pixels"] = {
                "lon": [round(float(v), 6) for v in self.lon[keep]],
                "lat": [round(float(v), 6) for v in self.lat[keep]],
                "baseline_ndvi": [round(float(v), 6) for v in self.baseline_ndvi[keep]],
                "ndvi": [
                    None if not np.isfinite(v) else round(float(v), 6)
                    for v in ndvi_today[keep]
                ],
                "increment": [
                    None if not np.isfinite(v) else round(float(v), 6)
                    for v in increment[keep]
                ],
                "class": [int(v) for v in self.classes[keep]],
            }
            field["class_counts"] = {
                "0": int(np.sum(self.classes[keep] == 0)),
                "1": int(np.sum(self.classes[keep] == 1)),
                "2": int(np.sum(self.classes[keep] == 2)),
            }
        return field



def analyze(stacks: list[DateStack]) -> EmergenceResult:
    """对一组秋季晴空日做本底、噪声和字段状态。输入已按晴空像元筛过。"""
    stacks = _merge_days(stacks)
    n_clear = len(stacks)
    as_of = stacks[-1].day if stacks else None
    if n_clear == 0:
        return _bare(
            STATUS_UNCERTAIN,
            "uncertain_insufficient_dates",
            None,
            n_clear,
            as_of,
        )
    keys = [autumn_season_key(s.day) for s in stacks]
    if any(key is None for key in keys) or len(set(keys)) != 1:
        # 返青季或跨季序列不做出苗结论，也不允许冻结。
        return _bare(
            STATUS_UNCERTAIN,
            "uncertain_not_autumn",
            None,
            n_clear,
            as_of,
        )
    season = keys[0]
    if n_clear < MIN_PLATEAU_DATES:
        return _bare(
            STATUS_UNCERTAIN,
            "uncertain_insufficient_dates",
            season,
            n_clear,
            as_of,
        )

    lon, lat, ndvi, ndre, dates = _align(stacks)
    if ndvi.shape[0] == 0 or _second_is_shift(ndvi[:, 0], ndvi[:, 1]):
        return _bare(
            STATUS_UNCERTAIN,
            "uncertain_no_plateau",
            season,
            n_clear,
            as_of,
        )

    early = _fit(ndvi[:, :2])
    if early is None:
        return _bare(
            STATUS_UNCERTAIN,
            "uncertain_insufficient_pixels",
            season,
            n_clear,
            as_of,
        )
    base_early, noise_early, _, valid_early = early
    k = 2
    # 后续日期是否还停在最初两个晴空日的本底上。用早期本底，避免平台跟着抬升往上爬。
    while k < n_clear:
        departs, _, _, _ = _departs(ndvi[:, k], base_early, valid_early, noise_early)
        if departs:
            break
        k += 1

    fitted = _fit(ndvi[:, :k])
    if fitted is None:
        return _bare(
            STATUS_UNCERTAIN,
            "uncertain_insufficient_pixels",
            season,
            n_clear,
            as_of,
        )
    while k < n_clear:
        base, noise, _, valid = fitted
        departs, _, _, _ = _departs(ndvi[:, k], base, valid, noise)
        if departs:
            break
        k += 1
        fitted = _fit(ndvi[:, :k])
        if fitted is None:
            return _bare(
                STATUS_UNCERTAIN,
                "uncertain_insufficient_pixels",
                season,
                n_clear,
                as_of,
            )
    base, noise, n_spikes, valid = fitted
    return _classify(
        season=season,
        dates=dates,
        lon=lon,
        lat=lat,
        ndvi=ndvi,
        ndre=ndre,
        k=k,
        baseline=base,
        noise=noise,
        n_spikes=n_spikes,
        valid=valid,
    )



def pixels_from_daily_field(field: dict[str, Any]) -> dict[str, Any]:
    """Lon/lat heatmap points from one day's official JSON field."""
    pixels_col = field.get("pixels") or {}
    lons = list(pixels_col.get("lon") or [])
    lats = list(pixels_col.get("lat") or [])
    bases = list(pixels_col.get("baseline_ndvi") or [])
    ndvis = list(pixels_col.get("ndvi") or [])
    incs = list(pixels_col.get("increment") or [])
    classes = list(pixels_col.get("class") or [])
    pixels: list[dict[str, Any]] = []
    counts = {"0": 0, "1": 0, "2": 0}
    n = min(len(lons), len(lats))
    for i in range(n):
        ndvi = ndvis[i] if i < len(ndvis) else None
        base = bases[i] if i < len(bases) else None
        inc = incs[i] if i < len(incs) else None
        if inc is None and ndvi is not None and base is not None:
            inc = float(ndvi) - float(base)
        cls = int(classes[i]) if i < len(classes) and classes[i] is not None else 0
        key = str(cls)
        if key in counts:
            counts[key] += 1
        pixels.append(
            {
                "lon": float(lons[i]),
                "lat": float(lats[i]),
                "increment": None if inc is None else float(inc),
                "class": cls,
                "baseline_ndvi": None if base is None else float(base),
                "ndvi": None if ndvi is None else float(ndvi),
            }
        )
    stored_counts = field.get("class_counts")
    if isinstance(stored_counts, dict) and stored_counts:
        counts = {k: int(stored_counts.get(k, 0) or 0) for k in ("0", "1", "2")}
    day = str(field.get("as_of_date") or "")[:10]
    return {
        "date": day,
        "as_of_date": day or None,
        "method_version": field.get("method_version") or METHOD_VERSION,
        "noise_floor": field.get("noise_floor"),
        "emerged_flag": bool(field.get("emerged_flag")),
        "status": field.get("status"),
        "status_zh": field.get("status_zh"),
        "format": "lonlat_v1",
        "value": "increment",
        "class_labels": dict(field.get("class_labels") or CLASS_LABELS_ZH),
        "disclaimer_zh": field.get("disclaimer_zh") or DISCLAIMER_ZH,
        "pixel_count": len(pixels),
        "counts": counts,
        "pixels_lonlat": pixels,
        "source": "stored",
        "obs_dates": list(field.get("obs_dates") or []),
        "baseline_dates": list(field.get("baseline_dates") or []),
    }


def attach_daily_field(product: dict[str, Any], field: dict[str, Any]) -> dict[str, Any]:
    """Write the daily emergence field onto the official product JSON.

    The compact summary sits at the product root for cheap indexing; the same
    object, including per-pixel columns, is stored under pixel_data so it
    lands in parcel_scene_products.pixel_data with the other indicators.
    """
    pixel_data = product.get("pixel_data")
    if isinstance(pixel_data, str):
        try:
            pixel_data = json.loads(pixel_data)
        except json.JSONDecodeError:
            pixel_data = {}
        product["pixel_data"] = pixel_data
    if not isinstance(pixel_data, dict):
        pixel_data = {}
        product["pixel_data"] = pixel_data
    pixel_data[DAILY_FIELD_KEY] = field
    summary = {k: v for k, v in field.items() if k != "pixels"}
    product[DAILY_FIELD_KEY] = summary
    return product


def daily_field_of(product: dict[str, Any] | None) -> dict[str, Any] | None:
    """Read a stored daily field from a product or its pixel_data."""
    if not isinstance(product, dict):
        return None
    pixel_data = product.get("pixel_data")
    if isinstance(pixel_data, str):
        try:
            pixel_data = json.loads(pixel_data)
        except json.JSONDecodeError:
            pixel_data = None
    for src in (product, pixel_data if isinstance(pixel_data, dict) else None):
        if not isinstance(src, dict):
            continue
        field = src.get(DAILY_FIELD_KEY)
        if isinstance(field, dict) and field:
            return field
    return None


def analyze_through(stacks: list[DateStack], as_of: date) -> EmergenceResult:
    """Compute emergence using clear official dates from the start through as_of."""
    return analyze([stack for stack in stacks if stack.day <= as_of])


def pixels_lonlat_for_date(
    payload: dict[str, Any], on: date | str | None
) -> dict[str, Any]:
    """把对象存储里的列还原成 lon/lat 点，供现有热力图按 increment / class 上色。"""
    obs = [str(d)[:10] for d in payload.get("obs_dates") or []]
    if not obs:
        raise KeyError("no obs_dates")
    if on is None:
        on_s = str(payload.get("as_of_date") or "")[:10]
    elif isinstance(on, datetime):
        on_s = on.date().isoformat()
    elif isinstance(on, date):
        on_s = on.isoformat()
    else:
        on_s = str(on)[:10]
    if on_s not in obs:
        raise KeyError(on_s)
    idx = obs.index(on_s)
    k = int(payload.get("n_baseline_dates") or 0)
    noise = float(payload["noise_floor"])
    columns = payload["columns"]
    lons = columns["lon"]
    lats = columns["lat"]
    baselines = columns["baseline_ndvi"]
    series = columns["ndvi"]
    stored_class = columns.get("class")
    as_of = str(payload.get("as_of_date") or "")[:10]
    pixels: list[dict[str, Any]] = []
    counts = {"0": 0, "1": 0, "2": 0}
    for i, lon in enumerate(lons):
        row = series[i]
        value = row[idx]
        base = baselines[i]
        if value is None or base is None:
            continue
        increment = float(value) - float(base)
        if on_s == as_of and stored_class is not None:
            cls = int(stored_class[i])
        else:
            previous = None
            can_sustain = idx >= k + 1
            if can_sustain and row[idx - 1] is not None:
                previous = float(row[idx - 1]) - float(base)
            cls = pixel_class(increment, previous, noise=noise, can_sustain=can_sustain)
        counts[str(cls)] += 1
        pixels.append(
            {
                "lon": float(lon),
                "lat": float(lats[i]),
                "increment": round(increment, 6),
                "class": cls,
                "baseline_ndvi": float(base),
                "ndvi": float(value),
            }
        )
    return {
        "date": on_s,
        "pixels_lonlat": pixels,
        "counts": counts,
        "pixel_count": len(pixels),
        "noise_floor": noise,
    }


def object_key(land_id: str, season_key: str, *, frozen: bool, digest: str) -> str:
    """冻结快照用内容哈希，避免后写的未冻结对象覆盖已冻结快照。"""
    if frozen:
        return f"emergence/autumn/{land_id}/{season_key}/frozen-{digest}.json"
    return f"emergence/autumn/{land_id}/{season_key}/latest.json"


def _classify(
    *,
    season: str,
    dates: list[date],
    lon: np.ndarray,
    lat: np.ndarray,
    ndvi: np.ndarray,
    ndre: np.ndarray | None,
    k: int,
    baseline: np.ndarray,
    noise: float,
    n_spikes: int,
    valid: np.ndarray,
) -> EmergenceResult:
    n_dates = len(dates)
    n_post = n_dates - k
    inc = ndvi - baseline[:, None]
    as_of_idx = n_dates - 1
    classes = np.full(ndvi.shape[0], -1, dtype=int)
    can_sustain = as_of_idx >= k + 1
    prev_idx = as_of_idx - 1
    usable_idx = np.flatnonzero(valid & np.isfinite(ndvi[:, as_of_idx]))
    for i in usable_idx:
        previous = None
        if can_sustain and np.isfinite(ndvi[i, prev_idx]):
            previous = float(inc[i, prev_idx])
        classes[i] = pixel_class(
            float(inc[i, as_of_idx]),
            previous,
            noise=noise,
            can_sustain=can_sustain,
        )
    usable = classes >= 0
    n_pixels = int(usable.sum())
    if n_pixels < MIN_PIXELS:
        return _bare(
            STATUS_UNCERTAIN,
            "uncertain_insufficient_pixels",
            season,
            n_dates,
            dates[-1],
        )

    share_above = float(np.mean(classes[usable] >= CLASS_ABOVE))
    share_sustained = float(np.mean(classes[usable] == CLASS_SUSTAINED))
    median_inc = float(np.median(inc[usable, as_of_idx]))
    # 对照比例。下面的状态分支不读这个数。
    share_context = float(np.mean(ndvi[usable, as_of_idx] >= NDVI_CONTEXT_MARK))
    ndre_share = _ndre_share(ndre, k, as_of_idx, n_dates)

    prev_median = float("nan")
    if n_post >= MIN_POST_DATES_FOR_EMERGED:
        prev_mask = usable & np.isfinite(ndvi[:, prev_idx])
        if int(prev_mask.sum()) >= MIN_PIXELS:
            prev_median = float(np.median(inc[prev_mask, prev_idx]))
    last_pair = (
        n_post >= MIN_POST_DATES_FOR_EMERGED
        and share_sustained >= EMERGED_SHARE
        and median_inc > noise
        and prev_median > noise
    )
    first_emerged = _first_emerged_date(dates, inc, ndvi, valid, k, noise)
    if last_pair:
        status = STATUS_EMERGED
        reason = "emerged_sustained_rise"
        first = first_emerged or dates[as_of_idx]
    elif n_post == 0:
        status = STATUS_NOT_EMERGED
        reason = "not_emerged_within_noise"
        first = None
    else:
        latest_departs = median_inc > noise or share_above >= EMERGING_SHARE
        earlier = _earlier_departure(inc, ndvi, valid, k, as_of_idx, noise)
        if latest_departs:
            status = STATUS_EMERGING
            reason = "emerging_partial_or_unconfirmed"
        elif earlier:
            status = STATUS_UNCERTAIN
            reason = "uncertain_signal_reversed"
        else:
            status = STATUS_NOT_EMERGED
            reason = "not_emerged_within_noise"
        first = None

    timeseries = _timeseries(dates, ndvi, inc, valid, k, noise)
    counts = {
        "0": int(np.sum(classes[usable] == 0)),
        "1": int(np.sum(classes[usable] == 1)),
        "2": int(np.sum(classes[usable] == 2)),
    }
    summary = {
        "reason": reason,
        "method_version": METHOD_VERSION,
        "emerged_share_min": EMERGED_SHARE,
        "emerging_share_min": EMERGING_SHARE,
        "spike_mad_k": SPIKE_MAD_K,
        "shift_sigma_k": SHIFT_SIGMA_K,
        "flat_field_shift": FLAT_FIELD_SHIFT,
        "n_spike_pixels": n_spikes,
        "n_baseline_dates": k,
        "n_post_dates": n_post,
        "class_counts": counts,
        "median_baseline_ndvi": _round(float(np.median(baseline[usable]))),
        "noise_floor": noise,
        "ndvi_ge_0_30_note": "context_only",
        "baseline_dates": [_iso(d) for d in dates[:k]],
    }
    return EmergenceResult(
        status=status,
        reason=reason,
        season_key=season,
        emerged_flag=status == STATUS_EMERGED,
        baseline_dates=list(dates[:k]),
        noise_floor=noise,
        as_of_date=dates[-1],
        first_emerged_date=first,
        n_clear=n_dates,
        n_pixels=n_pixels,
        median_increment=median_inc,
        share_above_noise=share_above,
        share_sustained=share_sustained,
        share_ndvi_ge_0_30=share_context,
        ndre_share_above_noise=ndre_share,
        timeseries=timeseries,
        summary=summary,
        obs_dates=list(dates),
        n_baseline_dates=k,
        lon=lon,
        lat=lat,
        baseline_ndvi=baseline,
        ndvi=ndvi,
        classes=classes,
    )


def _bare(
    status: str,
    reason: str,
    season: str | None,
    n_clear: int,
    as_of: date | None,
) -> EmergenceResult:
    return EmergenceResult(
        status=status,
        reason=reason,
        season_key=season,
        emerged_flag=False,
        baseline_dates=[],
        noise_floor=None,
        as_of_date=as_of,
        first_emerged_date=None,
        n_clear=n_clear,
        n_pixels=0,
        median_increment=None,
        share_above_noise=None,
        share_sustained=None,
        share_ndvi_ge_0_30=None,
        ndre_share_above_noise=None,
        timeseries=[],
        summary={"reason": reason, "method_version": METHOD_VERSION},
    )


def _merge_days(stacks: list[DateStack]) -> list[DateStack]:
    ordered = sorted(stacks, key=lambda s: s.day)
    merged: list[DateStack] = []
    for stack in ordered:
        if stack.ndvi.size == 0:
            continue
        if merged and merged[-1].day == stack.day:
            merged[-1] = _concat(merged[-1], stack)
        else:
            merged.append(stack)
    return merged


def _concat(a: DateStack, b: DateStack) -> DateStack:
    ndre = None
    if a.ndre is not None or b.ndre is not None:
        left = a.ndre if a.ndre is not None else np.full(a.ndvi.shape, np.nan)
        right = b.ndre if b.ndre is not None else np.full(b.ndvi.shape, np.nan)
        ndre = np.concatenate([left, right])
    return DateStack(
        day=a.day,
        lon=np.concatenate([a.lon, b.lon]),
        lat=np.concatenate([a.lat, b.lat]),
        ndvi=np.concatenate([a.ndvi, b.ndvi]),
        ndre=ndre,
    )


def _align(
    stacks: list[DateStack],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None, list[date]]:
    index: dict[tuple[int, int], int] = {}
    keys: list[tuple[int, int]] = []
    for stack in stacks:
        for lon, lat in zip(stack.lon, stack.lat, strict=True):
            if not np.isfinite(lon) or not np.isfinite(lat):
                continue
            key = (int(round(float(lon) * 1e5)), int(round(float(lat) * 1e5)))
            if key not in index:
                index[key] = len(keys)
                keys.append(key)
    n = len(keys)
    m = len(stacks)
    ndvi = np.full((n, m), np.nan, dtype=float)
    ndre = np.full((n, m), np.nan, dtype=float)
    has_ndre = False
    for j, stack in enumerate(stacks):
        bucket: dict[tuple[int, int], list[int]] = {}
        for i, (lon, lat) in enumerate(zip(stack.lon, stack.lat, strict=True)):
            if not np.isfinite(lon) or not np.isfinite(lat):
                continue
            key = (int(round(float(lon) * 1e5)), int(round(float(lat) * 1e5)))
            bucket.setdefault(key, []).append(i)
        for key, idxs in bucket.items():
            row = index[key]
            values = [float(stack.ndvi[i]) for i in idxs if np.isfinite(stack.ndvi[i])]
            if values:
                ndvi[row, j] = float(np.median(values))
            if stack.ndre is not None:
                nvals = [
                    float(stack.ndre[i]) for i in idxs if np.isfinite(stack.ndre[i])
                ]
                if nvals:
                    ndre[row, j] = float(np.median(nvals))
                    has_ndre = True
    lons = np.array([k[0] / 1e5 for k in keys], dtype=float)
    lats = np.array([k[1] / 1e5 for k in keys], dtype=float)
    return lons, lats, ndvi, ndre if has_ndre else None, [s.day for s in stacks]


def _second_is_shift(col0: np.ndarray, col1: np.ndarray) -> bool:
    """前两日若是整体抬升或下降，就还没有可重复的低平台。

    尺度取时间差和首日空间散度里更大的那个，避免整块一起漂几‰就被当成换季。
    """
    mask = np.isfinite(col0) & np.isfinite(col1)
    if int(mask.sum()) < MIN_PIXELS:
        return True
    delta = col1[mask] - col0[mask]
    med = float(np.median(delta))
    temporal = 1.4826 * float(np.median(np.abs(delta - med)))
    first = col0[mask]
    spatial = 1.4826 * float(np.median(np.abs(first - np.median(first))))
    scale = max(temporal, spatial)
    if scale == 0.0:
        return abs(med) > FLAT_FIELD_SHIFT
    return abs(med) > SHIFT_SIGMA_K * scale


def _fit(
    block: np.ndarray,
) -> tuple[np.ndarray, float, int, np.ndarray] | None:
    """平台本底 = 逐像元中位数；噪声 = 去掉尖峰后的残差绝对值 P99。"""
    n, _k = block.shape
    valid = np.all(np.isfinite(block), axis=1)
    if int(valid.sum()) < MIN_PIXELS:
        return None
    baseline = np.full(n, np.nan, dtype=float)
    baseline[valid] = np.median(block[valid], axis=1)
    resid = block[valid] - baseline[valid, None]
    flat = np.abs(resid).ravel()
    med = float(np.median(flat))
    mad = float(np.median(np.abs(flat - med)))
    sigma = 1.4826 * mad
    # 散度为 0 时残差是一个点，任何明显更大的值都是尖峰。
    # 否则用 MAD 围栏，避免两个亮像元把 P99 抬成植被增量。
    if sigma == 0.0:
        spike_cells = np.abs(resid) > med + 1e-9
    else:
        spike_cells = np.abs(resid) > med + SPIKE_MAD_K * sigma
    n_spike_pixels = int(np.any(spike_cells, axis=1).sum())
    keep = ~spike_cells.ravel()
    if int(keep.sum()) < MIN_PIXELS:
        keep = np.ones(flat.shape, dtype=bool)
        n_spike_pixels = 0
    noise = float(np.percentile(flat[keep], 99))
    if not np.isfinite(noise) or noise < 0:
        return None
    return baseline, noise, n_spike_pixels, valid


def _departs(
    column: np.ndarray,
    baseline: np.ndarray,
    valid: np.ndarray,
    noise: float,
) -> tuple[bool, float, float, np.ndarray]:
    increment = column - baseline
    mask = valid & np.isfinite(column)
    if int(mask.sum()) < MIN_PIXELS:
        return False, float("nan"), 0.0, mask
    med_inc = float(np.median(increment[mask]))
    share = float(np.mean(increment[mask] > noise))
    # 中位数越过噪声，或有相当一部分像元越过噪声，都离开平台。
    departs = bool(med_inc > noise or share >= EMERGING_SHARE)
    return departs, med_inc, share, mask


def _earlier_departure(
    inc: np.ndarray,
    ndvi: np.ndarray,
    valid: np.ndarray,
    k: int,
    as_of_idx: int,
    noise: float,
) -> bool:
    for j in range(k, as_of_idx):
        mask = valid & np.isfinite(ndvi[:, j])
        if int(mask.sum()) < MIN_PIXELS:
            continue
        med_inc = float(np.median(inc[mask, j]))
        share = float(np.mean(inc[mask, j] > noise))
        if med_inc > noise or share >= EMERGING_SHARE:
            return True
    return False


def _first_emerged_date(
    dates: list[date],
    inc: np.ndarray,
    ndvi: np.ndarray,
    valid: np.ndarray,
    k: int,
    noise: float,
) -> date | None:
    """最早一个“与前一个抬升日一起越过噪声”的晴空日。不是播种日或出苗日。"""
    n_dates = ndvi.shape[1]
    for j in range(k + 1, n_dates):
        mask = valid & np.isfinite(ndvi[:, j]) & np.isfinite(ndvi[:, j - 1])
        if int(mask.sum()) < MIN_PIXELS:
            continue
        both = (inc[mask, j] > noise) & (inc[mask, j - 1] > noise)
        share = float(np.mean(both))
        med_j = float(np.median(inc[mask, j]))
        med_prev = float(np.median(inc[mask, j - 1]))
        if share >= EMERGED_SHARE and med_j > noise and med_prev > noise:
            return dates[j]
    return None


def _ndre_share(
    ndre: np.ndarray | None, k: int, as_of_idx: int, n_dates: int
) -> float | None:
    """NDRE 只作旁证。没有红边、或平台不齐时留空，不改 NDVI 状态。"""
    if ndre is None or k < 2 or k > n_dates:
        return None
    fitted = _fit(ndre[:, :k])
    if fitted is None:
        return None
    baseline, noise, _, valid = fitted
    mask = valid & np.isfinite(ndre[:, as_of_idx])
    if int(mask.sum()) < MIN_PIXELS:
        return None
    increment = ndre[mask, as_of_idx] - baseline[mask]
    return float(np.mean(increment > noise))


def _timeseries(
    dates: list[date],
    ndvi: np.ndarray,
    inc: np.ndarray,
    valid: np.ndarray,
    k: int,
    noise: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for j, day in enumerate(dates):
        mask = valid & np.isfinite(ndvi[:, j])
        if int(mask.sum()) == 0:
            rows.append(
                {
                    "date": _iso(day),
                    "in_baseline": j < k,
                    "n_pixels": 0,
                    "median_ndvi": None,
                    "median_increment": None,
                    "share_above_noise": None,
                    "share_sustained": None,
                }
            )
            continue
        share_sustained = None
        if j >= k + 1:
            prev = np.isfinite(ndvi[:, j - 1])
            both = mask & prev
            if int(both.sum()) > 0:
                share_sustained = _round(
                    float(np.mean((inc[both, j] > noise) & (inc[both, j - 1] > noise))),
                    4,
                )
        rows.append(
            {
                "date": _iso(day),
                "in_baseline": j < k,
                "n_pixels": int(mask.sum()),
                "median_ndvi": _round(float(np.median(ndvi[mask, j]))),
                "median_increment": _round(float(np.median(inc[mask, j]))),
                "share_above_noise": _round(float(np.mean(inc[mask, j] > noise)), 4),
                "share_sustained": share_sustained,
            }
        )
    return rows


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day is not None else None


def _round(value: float, digits: int = 6) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), digits)


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _pixel_clear(pix: dict) -> bool:
    if "clear" not in pix and "Clear" not in pix:
        return True
    raw = pix.get("clear", pix.get("Clear"))
    try:
        return int(raw) == 1
    except (TypeError, ValueError):
        return False


def _ndvi_of(pix: dict) -> float | None:
    value = _as_float(pix.get("NDVI", pix.get("ndvi")))
    if value is None or value < -1.0 or value > 1.0:
        return None
    return value


def _as_day(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def stack_from_pixel_data(day: date, pixel_data) -> tuple[DateStack | None, int]:
    """Build a clear-pixel DateStack from an official lonlat product."""
    if isinstance(pixel_data, str):
        try:
            pixel_data = json.loads(pixel_data)
        except json.JSONDecodeError:
            return None, 0
    from agric_satellite_analysis_common.lonlat_pixels import extract_pixels

    pixels = extract_pixels(pixel_data)
    if not pixels:
        return None, 0
    lon: list[float] = []
    lat: list[float] = []
    ndvi: list[float] = []
    ndre: list[float] = []
    has_ndre = False
    for pix in pixels:
        if not _pixel_clear(pix):
            continue
        value = _ndvi_of(pix)
        lon_v = _as_float(pix.get("lon"))
        lat_v = _as_float(pix.get("lat"))
        if value is None or lon_v is None or lat_v is None:
            continue
        lon.append(lon_v)
        lat.append(lat_v)
        ndvi.append(value)
        ndre_v = _as_float(pix.get("NDRE"))
        if ndre_v is None:
            ndre_v = _as_float(pix.get("ndre"))
        if ndre_v is not None and -1.0 <= ndre_v <= 1.0:
            ndre.append(ndre_v)
            has_ndre = True
        else:
            ndre.append(float("nan"))
    if not lon:
        return None, len(pixels)
    return (
        DateStack(
            day=day,
            lon=np.asarray(lon, dtype=float),
            lat=np.asarray(lat, dtype=float),
            ndvi=np.asarray(ndvi, dtype=float),
            ndre=np.asarray(ndre, dtype=float) if has_ndre else None,
        ),
        len(pixels),
    )


def official_stack_from_product(product: dict) -> tuple[DateStack | None, int]:
    """DateStack from an autumn S2 product with usable pixels, or (None, total).

    The official biweekly / drought 30% cloud gate is not applied here. A raw
    S2 product with usable per-pixel NDVI is stacked even when
    ``cloud_cover_over_30`` is true or scene cloud exceeds ``CLOUD_MAX_PCT``.
    Decloud rows still follow official quality rules (good only). Products
    without a date, without a pixel NDVI stack, or with clear fraction below
    ``MIN_CLEAR_FRACTION`` are skipped.
    """
    from agric_satellite_analysis_common.agri_classify import (
        is_decloud_product,
        is_official_optical_product,
    )

    if not isinstance(product, dict):
        return None, 0
    day = _as_day(product.get("date"))
    if day is None or autumn_season_key(day) is None:
        return None, 0
    pixel_data = product.get("pixel_data")
    if isinstance(pixel_data, str):
        try:
            pixel_data = json.loads(pixel_data)
        except json.JSONDecodeError:
            pixel_data = None
    source = product.get("source") or product.get("product_source")
    decloud_quality = product.get("decloud_quality")
    parcel_cloud_source = product.get("parcel_cloud_source")
    if isinstance(pixel_data, dict):
        source = source or pixel_data.get("source")
        decloud_quality = decloud_quality or pixel_data.get("decloud_quality")
        parcel_cloud_source = parcel_cloud_source or pixel_data.get("parcel_cloud_source")
    # Decloud still uses official quality (good only). Raw S2 skips the
    # scene-level 30% cloud gate; clear-fraction decides usability.
    if is_decloud_product(source, product.get("scene_id")) and not is_official_optical_product(
        source=source,
        scene_id=product.get("scene_id"),
        decloud_quality=decloud_quality,
        parcel_cloud_cover_pct=_as_float(product.get("parcel_cloud_cover_pct")),
        cloud_cover=_as_float(product.get("cloud_cover")),
        cloud_cover_over_30=product.get("cloud_cover_over_30"),
        parcel_cloud_source=parcel_cloud_source,
    ):
        return None, 0
    stack, total = stack_from_pixel_data(day, pixel_data)
    if stack is None or total <= 0:
        return stack, total
    if int(stack.ndvi.size) / total < MIN_CLEAR_FRACTION:
        return None, total
    return stack, total
