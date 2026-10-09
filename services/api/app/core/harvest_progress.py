"""按观测日期估算地块已收获面积占比（v2：分季、像元粘滞、单调）。

v1（``s2_ndvi_peak_drop_v1``）对每个日期独立判定，用 150 天回看峰值，实测出现
0%↔100% 来回跳变：全云景在 ``clear`` 全 0 时被当成全部有效、冬季休眠/裸土被
当作“峰值后回落”、上一季（甚至饱和的 1.0）峰值被跨季使用。v2 改为：

1. **观测质控**（每天至多一景）：
   - 只用 ``clear=1`` 像元；整景无 clear 像元即不可用（不再回退到全部像元）；
     仅当像元完全没有 ``clear`` 字段（旧产品）且景级为正式光学产品时才用全部像元；
   - 去云合成景（UnCRtainTS 等）是模型预测，不作为收获证据；
   - 疑似积雪/水体像元（MNDWI≥阈值，S2 上即 NDSI）剔除；
   - 有效像元占比低于 ``min_valid_pct`` 的日期剔除；
   - 地块中位绿度比前后相邻有效观测都低 ``outlier_drop`` 以上的“凹陷日”
     （未识别的云影/薄雾）剔除。
2. **统一绿度**：按景的辐射定标来源选择可信指数并线性映射到 0–1 绿度
   （NDVI 0.15→0、0.85→1；EVI 0.10→0、0.60→1）。Earth Search
   ``sentinel-2-l2a`` 景在入库时被重复扣除 0.1 反射率偏移，NDVI 系统性偏高乃至
   饱和为 1.0，这类景改用受影响很小的 EVI；早期未定标产品 EVI 按 DN 计算
   失真，用比值型、与定标比例无关的 NDVI。
3. **生长季**：地块中位绿度首次≥``season_green``且下一有效观测仍≥该值记为
   返青（季开始）；峰值后中位绿度跌破该值记为成熟/收获期起点，收获窗口持续
   ``harvest_window_days`` 天或到下一次返青为止。季外日期为 off_season（0%）。
4. **像元粘滞**：本季曾≥``season_green``的像元为作物像元；其绿度降到
   ``harvest_green`` 以下且较本像元季峰值回落≥``peak_drop``，并被下一有效观测
   （``confirm_days`` 天内、≤``confirm_green``）确认，即从首次低值日起记为已收获，
   之后保持已收获直到下一季返青。最新一期尚无后续观测的候选像元先计入并标记
   ``confirmed=false``。
5. 已收获占比 = 本季已收获作物像元 / 本季作物像元，季内单调不减（0→100%），
   较上期新增 = 本期 − 同季上一期（不为负），新一季从 0 重新开始。

阈值可用环境变量调整；结果带 ``method_version``。光学影像无法区分“已收割”与
“已完全枯黄未收割”，结果仍需实地核实。
"""

from __future__ import annotations

import math
import os
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

HARVEST_PROGRESS_METHOD_VERSION = "s2_season_monotonic_v2"
HARVEST_PROGRESS_RULE_ZH = (
    "估算方法（v2）：按生长季判断，地块返青后、作物像元绿度较本季峰值回落≥{drop_pct}%"
    "且降到收获阈值以下，并经下一期影像确认，记为已收获，季内只增不减；"
    "只用无云有效像元，剔除积雪/水体与异常凹陷日，季外日期不计。"
    "光学影像无法区分已收割与完全枯黄未收割，结果需结合实地核实。"
)

# Earth Search v1 ``sentinel-2-l2a`` 条目 ID（如 S2A_50SLJ_20260103_0_L2A）。
# 该集合 COG 已扣除 BOA 偏移（earthsearch:boa_offset_applied=true），但 raster:bands
# 仍声明 offset=-0.1，入库时被再次扣除，导致 NDVI 偏高/饱和。
_EARTH_SEARCH_L2A_ID = re.compile(r"^S2[A-D]_\d{1,2}[A-Z]{3}_\d{8}_\d+_L2A$")

# 指数 → (裸土端元, 植被端元)，用于映射到 0–1 绿度。
_GREENNESS_ENDMEMBERS: dict[str, tuple[float, float]] = {
    "NDVI": (0.15, 0.85),
    "EVI": (0.10, 0.60),
}
# 指数物理上合理的取值范围；超出比例过高的景视为辐射定标异常。
_PLAUSIBLE_RANGE: dict[str, tuple[float, float]] = {
    "NDVI": (-0.999, 0.999),
    "EVI": (-1.0, 1.2),
}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


@dataclass(frozen=True)
class HarvestProgressThresholds:
    season_green: float = 0.45
    harvest_green: float = 0.25
    peak_drop: float = 0.50
    confirm_green: float = 0.35
    confirm_days: int = 40
    harvest_window_days: int = 60
    outlier_drop: float = 0.20
    outlier_days: int = 30
    min_valid_pct: float = 50.0
    snow_mndwi: float = 0.40
    max_bad_radiometry_pct: float = 20.0

    @classmethod
    def from_env(cls) -> "HarvestProgressThresholds":
        d = cls()
        return cls(
            season_green=_clamp(
                _env_float("HARVEST_PROGRESS_SEASON_GREEN", d.season_green), 0.1, 0.95
            ),
            harvest_green=_clamp(
                _env_float("HARVEST_PROGRESS_HARVEST_GREEN", d.harvest_green), 0.0, 0.9
            ),
            peak_drop=_clamp(
                _env_float("HARVEST_PROGRESS_PEAK_DROP", d.peak_drop), 0.0, 0.95
            ),
            confirm_green=_clamp(
                _env_float("HARVEST_PROGRESS_CONFIRM_GREEN", d.confirm_green), 0.0, 0.95
            ),
            confirm_days=int(
                _clamp(
                    _env_float("HARVEST_PROGRESS_CONFIRM_DAYS", d.confirm_days), 5, 120
                )
            ),
            harvest_window_days=int(
                _clamp(
                    _env_float("HARVEST_PROGRESS_WINDOW_DAYS", d.harvest_window_days),
                    15,
                    180,
                )
            ),
            outlier_drop=_clamp(
                _env_float("HARVEST_PROGRESS_OUTLIER_DROP", d.outlier_drop), 0.05, 1.0
            ),
            outlier_days=int(
                _clamp(
                    _env_float("HARVEST_PROGRESS_OUTLIER_DAYS", d.outlier_days), 5, 90
                )
            ),
            min_valid_pct=_clamp(
                _env_float("HARVEST_PROGRESS_MIN_VALID_PCT", d.min_valid_pct),
                0.0,
                100.0,
            ),
            snow_mndwi=_clamp(
                _env_float("HARVEST_PROGRESS_SNOW_MNDWI", d.snow_mndwi), 0.0, 1.0
            ),
            max_bad_radiometry_pct=_clamp(
                _env_float(
                    "HARVEST_PROGRESS_MAX_BAD_RADIOMETRY_PCT", d.max_bad_radiometry_pct
                ),
                0.0,
                100.0,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def rule_zh(self) -> str:
        return HARVEST_PROGRESS_RULE_ZH.format(drop_pct=round(self.peak_drop * 100))


# 计算一个地块需要的上下文天数：覆盖最长生长季（冬小麦返青前也可能达标）+ 收获窗口。
SEASON_CONTEXT_DAYS = 330
# 新观测最多会改写多早的结果（像元确认与凹陷日判定都只看相邻观测）。
REVISION_DAYS = 60


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


def _num(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pix_value(pix: dict[str, Any], key: str) -> float | None:
    return _num(pix.get(key, pix.get(key.lower())))


def scene_vegetation_index(
    algorithm_version: str | None, stac_item_id: str | None
) -> str:
    """按产品辐射定标来源选择可信的植被指数。

    - 未带 ``algorithm_version`` 的早期产品：指数按未定标 DN 计算，EVI 失真；
      NDVI 是比值、与定标比例无关，可用。
    - Earth Search ``sentinel-2-l2a`` 条目：入库重复扣除 0.1 偏移，NDVI 偏高/饱和；
      EVI 受影响很小，改用 EVI。
    - 其余（PC / Earth Search c1 等已正确定标）：NDVI。
    """
    if not (algorithm_version or "").strip():
        return "NDVI"
    if _EARTH_SEARCH_L2A_ID.match((stac_item_id or "").strip()):
        return "EVI"
    return "NDVI"


def greenness(value: float, index: str) -> float:
    soil, veg = _GREENNESS_ENDMEMBERS[index]
    return _clamp((value - soil) / (veg - soil), -0.2, 1.2)


def _is_decloud(scene: dict[str, Any]) -> bool:
    source = str(scene.get("source") or "").strip().lower()
    scene_id = str(scene.get("scene_id") or "").lower()
    return (
        "decloud" in source or "uncrtaints" in source or scene_id.endswith("_decloud")
    )


@dataclass
class _Obs:
    day: date
    scene_id: str | None
    index: str
    official: bool
    total: int
    # 像元键 → 绿度（只含有效像元）
    values: dict[tuple[float, float], float]
    mean_ndvi: float | None
    median_green: float = 0.0

    @property
    def valid_pct(self) -> float:
        return 100.0 * len(self.values) / self.total if self.total else 0.0


def _scene_obs(scene: dict[str, Any], thr: HarvestProgressThresholds) -> _Obs | None:
    day = _as_date(scene.get("date"))
    if day is None or _is_decloud(scene):
        return None
    pixels = [p for p in (scene.get("pixels") or []) if isinstance(p, dict)]
    if not pixels:
        return None
    official = bool(scene.get("official", True))
    has_clear = any("clear" in p for p in pixels)
    if not has_clear and not official:
        return None
    index = scene.get("vegetation_index") or scene_vegetation_index(
        scene.get("algorithm_version"), scene.get("stac_item_id")
    )
    lo, hi = _PLAUSIBLE_RANGE[index]
    values: dict[tuple[float, float], float] = {}
    ndvis: list[float] = []
    bad = 0
    considered = 0
    for i, p in enumerate(pixels):
        if has_clear:
            try:
                if int(p.get("clear") or 0) != 1:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
        v = _pix_value(p, index)
        if v is None:
            continue
        considered += 1
        if not lo <= v <= hi:
            bad += 1
            continue
        mndwi = _pix_value(p, "MNDWI")
        if mndwi is not None and mndwi >= thr.snow_mndwi:
            continue
        lon, lat = _num(p.get("lon")), _num(p.get("lat"))
        key = (
            (round(lon, 6), round(lat, 6))
            if lon is not None and lat is not None
            else (i, 0)
        )
        values[key] = greenness(v, index)
        nd = _pix_value(p, "NDVI")
        if nd is not None:
            ndvis.append(nd)
    if not values:
        return None
    # 指数大面积超出物理范围（饱和/定标失真）的景不可信，整景丢弃。
    if considered and 100.0 * bad / considered > thr.max_bad_radiometry_pct:
        return None
    obs = _Obs(
        day=day,
        scene_id=scene.get("scene_id"),
        index=index,
        official=official,
        total=len(pixels),
        values=values,
        mean_ndvi=sum(ndvis) / len(ndvis) if ndvis else None,
    )
    obs.median_green = statistics.median(values.values())
    return obs


def _daily_observations(
    scenes: Iterable[dict[str, Any]], thr: HarvestProgressThresholds
) -> list[_Obs]:
    best: dict[date, _Obs] = {}
    for s in scenes or []:
        if not isinstance(s, dict):
            continue
        obs = _scene_obs(s, thr)
        if obs is None or obs.valid_pct < thr.min_valid_pct:
            continue
        cur = best.get(obs.day)
        if cur is None or (obs.official, len(obs.values)) > (
            cur.official,
            len(cur.values),
        ):
            best[obs.day] = obs
    days = [best[d] for d in sorted(best)]
    _align_grids(days)
    return _drop_dips(days, thr)


# 不同产品版本的像元网格不同（旧产品经纬度网格 vs 新产品 UTM 网格，偏移数米），
# 像元状态要跨日期延续，需把各景像元吸附到同一参考网格。
_SNAP_MAX_M = 12.0
_SNAP_CELL_DEG = 0.0002


def _align_grids(days: list[_Obs]) -> None:
    if len(days) < 2:
        return
    grids: dict[frozenset, list[int]] = {}
    for i, obs in enumerate(days):
        grids.setdefault(frozenset(obs.values), []).append(i)
    if len(grids) < 2:
        return
    # 参考网格：被最多观测使用的网格（并列取最近的），其像元键即全序列的像元身份。
    ref_keys = max(grids.items(), key=lambda kv: (len(kv[1]), kv[1][-1]))[0]
    if not all(isinstance(k[0], float) for k in ref_keys):
        return
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for lon, lat in ref_keys:
        buckets.setdefault(_bucket(lon, lat), []).append((lon, lat))
    mapping: dict[Any, Any] = {}
    for keys, members in grids.items():
        if keys is ref_keys:
            continue
        for key in keys:
            if key in mapping:
                continue
            mapping[key] = _nearest(key, buckets) if isinstance(key[0], float) else None
        for i in members:
            snapped: dict[Any, list[float]] = {}
            for key, g in days[i].values.items():
                target = key if key in ref_keys else mapping.get(key)
                if target is not None:
                    snapped.setdefault(target, []).append(g)
            days[i].values = {k: sum(v) / len(v) for k, v in snapped.items()}


def _bucket(lon: float, lat: float) -> tuple[int, int]:
    return (math.floor(lon / _SNAP_CELL_DEG), math.floor(lat / _SNAP_CELL_DEG))


def _nearest(
    key: tuple[float, float], buckets: dict[tuple[int, int], list[tuple[float, float]]]
) -> tuple[float, float] | None:
    lon, lat = key
    bx, by = _bucket(lon, lat)
    kx = 111_320.0 * math.cos(math.radians(lat))
    best, best_d = None, _SNAP_MAX_M
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for rlon, rlat in buckets.get((bx + dx, by + dy), ()):
                d = math.hypot((rlon - lon) * kx, (rlat - lat) * 110_540.0)
                if d <= best_d:
                    best, best_d = (rlon, rlat), d
    return best


def _drop_dips(days: list[_Obs], thr: HarvestProgressThresholds) -> list[_Obs]:
    """剔除地块中位绿度明显低于前后相邻观测的孤立凹陷日（未识别云影/薄雾）。"""
    keep: list[_Obs] = []
    for i, obs in enumerate(days):
        prev = days[i - 1] if i > 0 else None
        nxt = days[i + 1] if i + 1 < len(days) else None
        if (
            prev is not None
            and nxt is not None
            and (obs.day - prev.day).days <= thr.outlier_days
            and (nxt.day - obs.day).days <= thr.outlier_days
            and obs.median_green
            < min(prev.median_green, nxt.median_green) - thr.outlier_drop
        ):
            continue
        keep.append(obs)
    return keep


@dataclass
class _Season:
    start: int  # days 下标
    end: int  # 含；季在该下标之后结束
    peak: int
    crop: set = field(default_factory=set)


def _find_seasons(days: list[_Obs], thr: HarvestProgressThresholds) -> list[_Season]:
    seasons: list[_Season] = []
    n = len(days)
    i = 0
    while i < n:
        g = days[i].median_green
        confirmed = g >= thr.season_green and (
            i + 1 >= n or days[i + 1].median_green >= thr.season_green
        )
        if not confirmed:
            i += 1
            continue
        start = i
        peak = i
        j = i + 1
        senesce: int | None = None
        end = n - 1
        while j < n:
            gj = days[j].median_green
            if senesce is None:
                if gj > days[peak].median_green:
                    peak = j
                elif gj < thr.season_green:
                    senesce = j
            else:
                window_end = days[senesce].day + timedelta(days=thr.harvest_window_days)
                regreen = gj >= thr.season_green and (
                    j + 1 >= n or days[j + 1].median_green >= thr.season_green
                )
                if regreen or days[j].day > window_end:
                    end = j - 1
                    break
            j += 1
        seasons.append(_Season(start=start, end=end, peak=peak))
        i = end + 1
    return seasons


def compute_harvest_series(
    scenes: Iterable[dict[str, Any]],
    *,
    parcel_area_mu: float | None = None,
    thresholds: HarvestProgressThresholds | None = None,
) -> list[dict[str, Any]]:
    """计算每个有效观测日的已收获占比序列（按日期升序）。

    ``scenes`` 每项需含 ``date``、``pixels``（lonlat_v1 像元列表），可选
    ``scene_id``、``official``、``source``、``algorithm_version``、``stac_item_id``。
    返回值不含被质控剔除的日期；季外日期以 ``off_season``、0% 返回。
    """
    thr = thresholds or HarvestProgressThresholds.from_env()
    days = _daily_observations(scenes, thr)
    area = _num(parcel_area_mu)
    if area is not None and area <= 0:
        area = None

    season_of: dict[int, _Season] = {}
    harvested_at: dict[int, set] = {}
    provisional_at: dict[int, set] = {}
    for season in _find_seasons(days, thr):
        idx = range(season.start, season.end + 1)
        for k in idx:
            season_of[k] = season
        # 作物像元：本季任一有效观测绿度≥返青阈值。
        peak_green: dict[Any, float] = {}
        for k in idx:
            for key, g in days[k].values.items():
                if g >= thr.season_green:
                    season.crop.add(key)
        harvested: set = set()
        # 尚无后续观测可确认的候选像元：一旦出现就延续到季末，保证季内单调。
        pending: set = set()
        for k in idx:
            obs = days[k]
            for key, g in obs.values.items():
                if key not in season.crop or key in harvested or key in pending:
                    continue
                pk = peak_green.get(key)
                if (
                    pk is not None
                    and pk >= thr.season_green
                    and g <= thr.harvest_green
                    and g <= pk * (1.0 - thr.peak_drop)
                ):
                    verdict = _confirm(days, k, key, season.end, thr)
                    if verdict is True:
                        harvested.add(key)
                        continue
                    if verdict is None:
                        pending.add(key)
                if pk is None or g > pk:
                    peak_green[key] = g
            harvested_at[k] = set(harvested)
            provisional_at[k] = set(pending)

    out: list[dict[str, Any]] = []
    prev_pct: float | None = None
    prev_season: _Season | None = None
    for k, obs in enumerate(days):
        season = season_of.get(k)
        crop_n = len(season.crop) if season else 0
        done = harvested_at.get(k, set())
        pend = provisional_at.get(k, set())
        if season is None or not crop_n:
            status, pct, count, confirmed = "off_season", 0.0, 0, True
            prev_pct, prev_season = None, None
        else:
            count = len(done) + len(pend)
            pct = round(100.0 * count / crop_n, 1)
            confirmed = not pend
            if count == 0:
                status = "growing"
            elif pct >= 99.95:
                status = "harvested"
            else:
                status = "harvesting"
        if season is not None and prev_season is not season:
            prev_pct = None
        newly = round(max(0.0, pct - prev_pct), 1) if prev_pct is not None else pct
        peak_obs = days[season.peak] if season else None
        out.append(
            {
                "date": obs.day.isoformat(),
                "scene_id": obs.scene_id,
                "status": status,
                "harvested_pct": pct,
                "newly_harvested_pct": newly,
                "harvested_area_mu": round(area * pct / 100.0, 2)
                if area is not None
                else None,
                "parcel_area_mu": round(area, 2) if area is not None else None,
                "valid_pct": round(obs.valid_pct, 1),
                "valid_pixel_count": len(obs.values),
                "harvested_pixel_count": count,
                "crop_pixel_count": crop_n,
                "total_pixel_count": obs.total,
                "mean_ndvi": round(obs.mean_ndvi, 4)
                if obs.mean_ndvi is not None
                else None,
                "greenness": round(obs.median_green, 4),
                "peak_greenness": round(peak_obs.median_green, 4) if peak_obs else None,
                "peak_date": peak_obs.day.isoformat() if peak_obs else None,
                "season_start": days[season.start].day.isoformat() if season else None,
                "vegetation_index": obs.index,
                "confirmed": confirmed,
                "official": obs.official,
            }
        )
        if season is not None:
            prev_pct, prev_season = pct, season
    return out


def _confirm(
    days: list[_Obs], k: int, key: Any, last: int, thr: HarvestProgressThresholds
) -> bool | None:
    """用该像元下一次有效观测确认低值：True 确认 / False 被推翻 / None 尚无后续观测。"""
    limit = days[k].day + timedelta(days=thr.confirm_days)
    for m in range(k + 1, len(days)):
        if days[m].day > limit:
            return False
        g = days[m].values.get(key)
        if g is None:
            continue
        # 季已结束后的观测（下一季返青）也可作为确认依据：低值则确认。
        return g <= thr.confirm_green
    return None if k <= last else False
