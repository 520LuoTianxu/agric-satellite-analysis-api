"""按观测日期估算地块已收获面积占比（首版启发式，像元级 NDVI 峰值回落）。

仓库已有的 ``harvest_detect`` 只基于地块平均 NDVI 给出“一个收获日”，没有像元级
“已收获/未收获”分类。本模块在已入库的 S2 ``lonlat_v1`` 像元上做保守判定：

1. 每个日期只取一景：优先正式光学产品（去云合格），其次有效像元最多的一景；
2. 有效像元：任一像元带 ``clear=1`` 时只用 clear 像元，否则全部像元；
   有效像元占比低于 ``min_valid_pct`` 的日期视为观测不足，不参与序列；
3. 季节峰值：回看 ``lookback_days`` 天内各有效日期的像元平均 NDVI 最大值；
   峰值 < ``grow_min`` 视为本季未见作物（no_growth，占比 0）；
   当前日期不晚于峰值日期视为仍在生长（growing，占比 0）；
4. 峰值之后，像元 NDVI ≤ ``ndvi_max`` 且 ≤ 峰值×(1-``peak_drop``) 判为已收获；
5. 已收获占比 = 已收获像元 / 有效像元；较上期新增 = max(0, 本期 − 上一有效期)。

阈值均可用环境变量调整，结果带 ``method_version``，阈值需结合农艺实测校准。
不做日期插值，只输出真实过境日期。
"""

from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

HARVEST_PROGRESS_METHOD_VERSION = "s2_ndvi_peak_drop_v1"
HARVEST_PROGRESS_RULE_ZH = (
    "首版启发式估算：峰值后像元 NDVI≤{ndvi_max} 且较季节峰值回落≥{drop_pct}% 记为已收获；"
    "只统计无云有效像元，有效像元<{min_valid}% 的日期不计；结果需结合实地核实。"
)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


@dataclass(frozen=True)
class HarvestProgressThresholds:
    ndvi_max: float = 0.30
    peak_drop: float = 0.50
    grow_min: float = 0.35
    lookback_days: int = 150
    min_valid_pct: float = 50.0

    @classmethod
    def from_env(cls) -> "HarvestProgressThresholds":
        return cls(
            ndvi_max=_env_float("HARVEST_PROGRESS_NDVI_MAX", 0.30),
            peak_drop=min(0.95, max(0.0, _env_float("HARVEST_PROGRESS_PEAK_DROP", 0.50))),
            grow_min=_env_float("HARVEST_PROGRESS_GROW_MIN", 0.35),
            lookback_days=max(
                30, int(_env_float("HARVEST_PROGRESS_LOOKBACK_DAYS", 150))
            ),
            min_valid_pct=min(
                100.0, max(0.0, _env_float("HARVEST_PROGRESS_MIN_VALID_PCT", 50.0))
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def rule_zh(self) -> str:
        return HARVEST_PROGRESS_RULE_ZH.format(
            ndvi_max=self.ndvi_max,
            drop_pct=round(self.peak_drop * 100),
            min_valid=round(self.min_valid_pct),
        )


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _pixel_ndvi(pix: dict[str, Any]) -> float | None:
    raw = pix.get("NDVI", pix.get("ndvi"))
    if raw is None or isinstance(raw, bool):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v < -1 or v > 1:
        return None
    return v


def _is_clear(pix: dict[str, Any]) -> bool:
    try:
        return int(pix.get("clear") or 0) == 1
    except (TypeError, ValueError, OverflowError):
        return False


def valid_ndvi_values(pixels: Iterable[Any] | None) -> tuple[list[float], int]:
    """返回（有效像元 NDVI 列表, 像元总数）；与图一日分级同一 clear 口径。"""
    items = [p for p in (pixels or []) if isinstance(p, dict)]
    any_clear = any(_is_clear(p) for p in items)
    values: list[float] = []
    for p in items:
        if any_clear and not _is_clear(p):
            continue
        v = _pixel_ndvi(p)
        if v is not None:
            values.append(v)
    return values, len(items)


@dataclass
class _DayObs:
    day: date
    scene_id: str | None
    values: list[float]
    total: int
    official: bool

    @property
    def valid_pct(self) -> float:
        return 100.0 * len(self.values) / self.total if self.total else 0.0

    @property
    def mean(self) -> float:
        return sum(self.values) / len(self.values)


def _pick_daily(scenes: Iterable[dict[str, Any]]) -> list[_DayObs]:
    """每天选一景：正式光学产品优先，其次有效像元多者。"""
    best: dict[date, _DayObs] = {}
    for s in scenes or []:
        if not isinstance(s, dict):
            continue
        day = _as_date(s.get("date"))
        if day is None:
            continue
        values, total = valid_ndvi_values(s.get("pixels"))
        if not values:
            continue
        obs = _DayObs(
            day=day,
            scene_id=s.get("scene_id"),
            values=values,
            total=total,
            official=bool(s.get("official", True)),
        )
        cur = best.get(day)
        if cur is None or (obs.official, len(obs.values)) > (
            cur.official,
            len(cur.values),
        ):
            best[day] = obs
    return [best[d] for d in sorted(best)]


def compute_harvest_series(
    scenes: Iterable[dict[str, Any]],
    *,
    parcel_area_mu: float | None = None,
    thresholds: HarvestProgressThresholds | None = None,
) -> list[dict[str, Any]]:
    """计算每个有效观测日的已收获占比序列（按日期升序）。

    ``scenes`` 每项需含 ``date``、``pixels``（lonlat_v1 像元列表），可选
    ``scene_id`` 与 ``official``。返回值不含观测不足的日期。
    """
    thr = thresholds or HarvestProgressThresholds.from_env()
    days = [d for d in _pick_daily(scenes) if d.valid_pct >= thr.min_valid_pct]
    area = None
    if parcel_area_mu is not None:
        try:
            area = float(parcel_area_mu)
        except (TypeError, ValueError):
            area = None
        if area is not None and (not math.isfinite(area) or area <= 0):
            area = None

    out: list[dict[str, Any]] = []
    prev_pct: float | None = None
    for i, obs in enumerate(days):
        window_start = obs.day - timedelta(days=thr.lookback_days)
        history = [d for d in days[: i + 1] if d.day >= window_start]
        peak = max(history, key=lambda d: (d.mean, d.day))
        peak_mean = peak.mean
        harvested = 0
        if peak_mean < thr.grow_min:
            status = "no_growth"
        elif peak.day >= obs.day:
            status = "growing"
        else:
            limit = min(thr.ndvi_max, peak_mean * (1.0 - thr.peak_drop))
            harvested = sum(1 for v in obs.values if v <= limit)
            status = "harvesting" if harvested else "not_harvested"
        valid = len(obs.values)
        pct = round(100.0 * harvested / valid, 1)
        if harvested and pct >= 99.95:
            status = "harvested"
        newly = round(max(0.0, pct - prev_pct), 1) if prev_pct is not None else pct
        out.append(
            {
                "date": obs.day.isoformat(),
                "scene_id": obs.scene_id,
                "status": status,
                "harvested_pct": pct,
                "newly_harvested_pct": newly,
                "harvested_area_mu": (
                    round(area * pct / 100.0, 2) if area is not None else None
                ),
                "parcel_area_mu": round(area, 2) if area is not None else None,
                "valid_pct": round(obs.valid_pct, 1),
                "valid_pixel_count": valid,
                "harvested_pixel_count": harvested,
                "total_pixel_count": obs.total,
                "mean_ndvi": round(obs.mean, 4),
                "peak_ndvi": round(peak_mean, 4),
                "peak_date": peak.day.isoformat(),
                "official": obs.official,
            }
        )
        prev_pct = pct
    return out
