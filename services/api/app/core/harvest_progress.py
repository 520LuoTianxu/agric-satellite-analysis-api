"""按观测日期估算地块已收获面积占比（v3：分季、像元粘滞、单调 + 置信度）。

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

v3 在 v2 基础上增加：

6. **阈值来源**：按作物/省份配置档（``HARVEST_PROGRESS_PROFILES``）→ 地块自身多季
   历史自适应（峰值/谷值分位数）→ 默认值，依次回退；来源写入 ``threshold_source``。
7. **Sentinel-1 佐证**：同轨道、同产品版本下，比较观测日附近 S1 与本季峰值期 S1
   的地块中位 VH 及 VH−VV 交叉极化比；只在峰值之后使用。实测像元级 S1 变化与
   光学收获像元不可分（噪声≈信号），因此 S1 **不改变收获占比**，只用于
   一致性评分和提前确认最新一期的待确认像元。
8. **逐观测置信度**（0–1、high/medium/low、原因码），公式见
   :func:`observation_confidence` 与 ADR。
9. **按日插值**（仅查询时，不入库）：同季相邻观测间线性插值，单调，标记
   ``interpolated``，置信度按相邻观测打折。

阈值可用环境变量调整；结果带 ``method_version``。光学影像无法区分“已收割”与
“已完全枯黄未收割”，结果仍需实地核实。
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Iterable

HARVEST_PROGRESS_METHOD_VERSION = "s2s1_season_monotonic_v3"
HARVEST_PROGRESS_RULE_ZH = (
    "估算方法（v3）：按生长季判断，地块返青后、作物像元绿度较本季峰值回落≥{drop_pct}%"
    "且降到收获阈值以下，并经下一期影像确认，记为已收获，季内只增不减；"
    "只用无云有效像元，剔除积雪/水体与异常凹陷日，季外日期不计。"
    "光学影像无法区分已收割与完全枯黄未收割，结果需结合实地核实。"
)

# Earth Search v1 ``sentinel-2-l2a`` 条目 ID（如 S2A_50SLJ_20260103_0_L2A）。
# 该集合 COG 已扣除 BOA 偏移（earthsearch:boa_offset_applied=true），但 raster:bands
# 仍声明 offset=-0.1，入库 v3/v4 时被再次扣除，导致 NDVI 偏高/饱和；v5 起已修复。
_EARTH_SEARCH_L2A_ID = re.compile(r"^S2[A-D]_\d{1,2}[A-Z]{3}_\d{8}_\d+_L2A$")
_DOUBLE_OFFSET_VERSIONS = frozenset(
    {"stac-optical-lonlat-v3", "stac-optical-lonlat-v4"}
)

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

    def with_overrides(self, raw: dict[str, Any]) -> "HarvestProgressThresholds":
        """按配置档覆盖部分阈值；未知键/非法值忽略，取值按 from_env 同样的范围钳制。"""
        changes: dict[str, Any] = {}
        for name, (lo, hi, is_int) in _PROFILE_FIELDS.items():
            value = _num(raw.get(name)) if isinstance(raw, dict) else None
            if value is None:
                continue
            value = _clamp(value, lo, hi)
            changes[name] = int(value) if is_int else value
        return replace(self, **changes) if changes else self


# 配置档可覆盖的阈值：名称 → (下限, 上限, 是否整数)。
_PROFILE_FIELDS: dict[str, tuple[float, float, bool]] = {
    "season_green": (0.1, 0.95, False),
    "harvest_green": (0.0, 0.9, False),
    "peak_drop": (0.0, 0.95, False),
    "confirm_green": (0.0, 0.95, False),
    "confirm_days": (5, 120, True),
    "harvest_window_days": (15, 180, True),
}


def load_threshold_profiles(raw: str | None = None) -> dict[str, dict[str, Any]]:
    """解析 ``HARVEST_PROGRESS_PROFILES``（JSON 对象）。

    键：``"<crop_type>"``、``"<crop_type>@<province_code>"``、``"@<province_code>"``，
    作物名不区分大小写。例：``{"corn": {"harvest_green": 0.22},
    "@21": {"harvest_window_days": 75}}``。解析失败时返回空（使用默认/自适应）。
    """
    text = os.getenv("HARVEST_PROGRESS_PROFILES") if raw is None else raw
    if not text or not str(text).strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k).strip().lower(): v
        for k, v in data.items()
        if isinstance(v, dict) and str(k).strip()
    }


def match_threshold_profile(
    profiles: dict[str, dict[str, Any]],
    crop_type: str | None,
    region_code: str | None,
) -> tuple[str, dict[str, Any]] | None:
    """优先级：作物@省份 > 作物 > @省份。"""
    crop = (crop_type or "").strip().lower()
    region = (region_code or "").strip().lower()
    for key in (
        f"{crop}@{region}" if crop and region else None,
        crop or None,
        f"@{region}" if region else None,
    ):
        if key and key in profiles:
            return key, profiles[key]
    return None


# 自适应阈值：在地块自身的多季绿度幅度上取固定比例，并钳制在默认值附近，
# 防止个别异常季把阈值推到不合理位置。比例取自默认值在典型幅度（谷 0、峰 0.9）上
# 的位置：返青 0.45、收获 0.25、确认 0.35。
ADAPTIVE_MIN_SEASONS = 2
ADAPTIVE_MIN_OBS = 12
ADAPTIVE_MIN_AMPLITUDE = 0.30
_ADAPTIVE_FRACTIONS = {
    "season_green": 0.50,
    "harvest_green": 0.28,
    "confirm_green": 0.39,
}
# 自适应结果只允许在默认值 ±0.05 内微调（保守）：实测饱和/混合指数会把幅度推高，
# 放宽会推迟下一季返青切季（如麦收后玉米季）。
_ADAPTIVE_LIMITS = {
    "season_green": (0.40, 0.50),
    "harvest_green": (0.20, 0.30),
    "confirm_green": (0.30, 0.40),
}


def _percentile(values: list[float], q: float) -> float:
    xs = sorted(values)
    if not xs:
        return 0.0
    pos = (len(xs) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


# 计算一个地块需要的上下文天数：覆盖最长生长季（冬小麦返青前也可能达标）+ 收获窗口。
SEASON_CONTEXT_DAYS = 330
# 自适应阈值需要的多季历史（天）。
ADAPTIVE_HISTORY_DAYS = 730
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
    - v3/v4 的 Earth Search ``sentinel-2-l2a`` 条目：入库重复扣除 0.1 偏移，NDVI
      偏高/饱和；EVI 受影响很小，改用 EVI。
    - 其余（v5 起的 Earth Search、PC / c1 等已正确定标）：NDVI。
    """
    version = (algorithm_version or "").strip()
    if not version:
        return "NDVI"
    if version in _DOUBLE_OFFSET_VERSIONS and _EARTH_SEARCH_L2A_ID.match(
        (stac_item_id or "").strip()
    ):
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


def adaptive_thresholds(
    days: list[_Obs], base: HarvestProgressThresholds
) -> tuple[HarvestProgressThresholds, dict[str, Any]] | None:
    """由地块自身多季历史推导返青/收获/确认阈值；历史不足时返回 None（用 base）。

    - 峰值 = 各季峰值中位绿度的中位数（先用 base 阈值切季），上限 1.0；
    - 谷值 = 全部有效观测中位绿度的 10% 分位数；
    - 幅度 = 峰值 − 谷值，需 ≥ ``ADAPTIVE_MIN_AMPLITUDE``；
    - 阈值 = 谷值 + 比例 × 幅度（返青 0.50、收获 0.28、确认 0.39），再钳制在默认值
      ±0.05：返青 0.40–0.50，收获 0.20–0.30，确认 0.30–0.40（且收获<确认<返青）。
    """
    if len(days) < ADAPTIVE_MIN_OBS:
        return None
    seasons = _find_seasons(days, base)
    if len(seasons) < ADAPTIVE_MIN_SEASONS:
        return None
    peak = min(1.0, statistics.median(days[s.peak].median_green for s in seasons))
    trough = _percentile([d.median_green for d in days], 0.10)
    amplitude = peak - trough
    if amplitude < ADAPTIVE_MIN_AMPLITUDE:
        return None
    season_green = _clamp(
        trough + _ADAPTIVE_FRACTIONS["season_green"] * amplitude,
        *_ADAPTIVE_LIMITS["season_green"],
    )
    harvest_green = _clamp(
        trough + _ADAPTIVE_FRACTIONS["harvest_green"] * amplitude,
        *_ADAPTIVE_LIMITS["harvest_green"],
    )
    confirm_green = _clamp(
        trough + _ADAPTIVE_FRACTIONS["confirm_green"] * amplitude,
        *_ADAPTIVE_LIMITS["confirm_green"],
    )
    confirm_green = _clamp(confirm_green, harvest_green + 0.05, season_green - 0.05)
    thr = replace(
        base,
        season_green=round(season_green, 3),
        harvest_green=round(harvest_green, 3),
        confirm_green=round(confirm_green, 3),
    )
    stats = {
        "seasons": len(seasons),
        "peak": round(peak, 3),
        "trough": round(trough, 3),
        "amplitude": round(amplitude, 3),
    }
    return thr, stats


def resolve_thresholds(
    days: list[_Obs],
    base: HarvestProgressThresholds,
    *,
    crop_type: str | None = None,
    region_code: str | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    adaptive: bool = True,
) -> tuple[HarvestProgressThresholds, str, dict[str, Any]]:
    """阈值回退链：配置档（作物@省份 > 作物 > @省份）→ 自适应 → 默认。"""
    profiles = load_threshold_profiles() if profiles is None else profiles
    hit = match_threshold_profile(profiles, crop_type, region_code)
    if hit is not None:
        key, raw = hit
        return base.with_overrides(raw), f"profile:{key}", {}
    if adaptive:
        got = adaptive_thresholds(days, base)
        if got is not None:
            return got[0], "adaptive", got[1]
    return base, "default", {}


# ── Sentinel-1 佐证 ─────────────────────────────────────────────────
# 峰值期参考窗口（天）与观测匹配窗口：S1 在光学观测前 3 天至后 6 天内。
S1_REF_DAYS = 20
S1_MATCH_BEFORE_DAYS = 3
S1_MATCH_AFTER_DAYS = 6
# 地块中位变化（相对峰值期，同轨道同版本）：
# 交叉极化比 VH−VV 下降 ≥0.75 dB（且 VH 未明显上升 >1 dB）或 VH 下降 ≥1.5 dB
# 视为“收获样”（植被体散射减弱）；
# 比值下降 <0.4 dB 且 VH 下降 <0.75 dB 视为“仍有作物”；其间为不确定。
S1_HARVEST_RATIO_DB = -0.75
S1_HARVEST_VH_DB = -1.5
S1_HARVEST_MAX_VH_RISE_DB = 1.0
S1_STABLE_RATIO_DB = -0.40
S1_STABLE_VH_DB = -0.75
S1_MIN_PIXELS = 10


@dataclass
class _S1Obs:
    day: date
    orbit: Any
    version: str
    vh: float
    ratio: float

    @property
    def group(self) -> tuple[Any, str]:
        return (self.orbit, self.version)


def _s1_observations(scenes: Iterable[dict[str, Any]] | None) -> list[_S1Obs]:
    out: list[_S1Obs] = []
    for s in scenes or []:
        if not isinstance(s, dict):
            continue
        day = _as_date(s.get("date"))
        if day is None:
            continue
        vh: list[float] = []
        ratio: list[float] = []
        for p in s.get("pixels") or []:
            if not isinstance(p, dict):
                continue
            a, b = _pix_value(p, "VH_db"), _pix_value(p, "VV_db")
            if a is None or b is None:
                continue
            vh.append(a)
            ratio.append(a - b)
        if len(vh) < S1_MIN_PIXELS:
            continue
        out.append(
            _S1Obs(
                day=day,
                orbit=s.get("relative_orbit"),
                # 不同产品版本的定标不同（旧版比 v6 低约 5 dB），不能跨版本比较。
                version=str(s.get("algorithm_version") or "legacy"),
                vh=statistics.median(vh),
                ratio=statistics.median(ratio),
            )
        )
    out.sort(key=lambda o: o.day)
    return out


def _s1_change(obs: _S1Obs, refs: dict[tuple[Any, str], tuple[float, float]]):
    ref = refs.get(obs.group)
    if ref is None:
        return None
    d_vh, d_ratio = obs.vh - ref[0], obs.ratio - ref[1]
    if (
        d_ratio <= S1_HARVEST_RATIO_DB and d_vh <= S1_HARVEST_MAX_VH_RISE_DB
    ) or d_vh <= S1_HARVEST_VH_DB:
        kind = "harvest"
    elif d_ratio > S1_STABLE_RATIO_DB and d_vh > S1_STABLE_VH_DB:
        kind = "crop"
    else:
        kind = "ambiguous"
    return obs, round(d_vh, 2), round(d_ratio, 2), kind


def _s1_refs(
    s1: list[_S1Obs], peak_day: date
) -> dict[tuple[Any, str], tuple[float, float]]:
    groups: dict[tuple[Any, str], list[_S1Obs]] = {}
    for o in s1:
        if abs((o.day - peak_day).days) <= S1_REF_DAYS:
            groups.setdefault(o.group, []).append(o)
    return {
        g: (
            statistics.median(o.vh for o in items),
            statistics.median(o.ratio for o in items),
        )
        for g, items in groups.items()
    }


# ── 置信度 ──────────────────────────────────────────────────────────
CONFIDENCE_WEIGHTS = {
    "data": 0.25,
    "gap": 0.15,
    "margin": 0.25,
    "confirm": 0.25,
    "s1": 0.10,
}
CONFIDENCE_HIGH = 0.75
CONFIDENCE_MEDIUM = 0.50
INTERPOLATED_FACTOR = 0.70
# 待确认像元占已收获计数 ≥25% 或 S1 矛盾时，等级最高为 medium。
PENDING_CAP_FRACTION = 0.25
MEDIUM_CAP = 0.74
MARGIN_SCALE = 0.15
FULL_PIXELS = 50


def confidence_level(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= CONFIDENCE_HIGH:
        return "high"
    if score >= CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def observation_confidence(
    *,
    valid_pct: float,
    valid_pixel_count: int,
    gap_days: int | None,
    margin: float | None,
    pending_fraction: float,
    s1_agreement: str | None = None,
    s1_confirmed: bool = False,
    min_valid_pct: float = 50.0,
) -> tuple[float, str, list[str]]:
    """单期观测置信度 = 各因子 q∈[0,1] 的加权平均（S1 因子仅在有可比 S1 时参与）。

    - data   = (0.4 + 0.6·clamp((valid_pct − min_valid_pct)/(90 − min_valid_pct)))
               × min(1, valid_pixel_count / 50)
    - gap    = 1 − 0.7·clamp((gap_days − 6)/30)；首期观测为 1
    - margin = clamp(0.3 + margin/0.15)；margin 为判定量离收获阈值的绿度距离
               （已收获像元：阈值−绿度的中位数；未收获：中位绿度−阈值）
    - confirm= 1 − 0.6·待确认占比（S1 已佐证时 1 − 0.2·待确认占比）
    - s1     = 一致 1.0 / 矛盾 0.2；不确定或无 S1 时不参与
    权重：data .25、gap .15、margin .25、confirm .25、s1 .10。
    上限：待确认占比 ≥25%（且未被 S1 佐证）或 S1 矛盾时，得分不超过 0.74。
    等级：≥0.75 high，≥0.50 medium，否则 low。
    """
    reasons: list[str] = []
    span = max(1.0, 90.0 - min_valid_pct)
    q_data = (0.4 + 0.6 * _clamp((valid_pct - min_valid_pct) / span, 0.0, 1.0)) * min(
        1.0, valid_pixel_count / FULL_PIXELS
    )
    if valid_pct < 70.0:
        reasons.append("low_valid_pct")
    if valid_pixel_count < FULL_PIXELS:
        reasons.append("few_pixels")
    q_gap = 1.0
    if gap_days is not None:
        q_gap = 1.0 - 0.7 * _clamp((gap_days - 6) / 30.0, 0.0, 1.0)
        if gap_days > 15:
            reasons.append("long_gap")
    q_margin = 0.5 if margin is None else _clamp(0.3 + margin / MARGIN_SCALE, 0.0, 1.0)
    if q_margin < 0.6:
        reasons.append("small_margin")
    pending_fraction = _clamp(pending_fraction, 0.0, 1.0)
    q_confirm = 1.0 - (0.2 if s1_confirmed else 0.6) * pending_fraction
    if pending_fraction > 0:
        reasons.append("s1_confirmed" if s1_confirmed else "unconfirmed")
    factors = {"data": q_data, "gap": q_gap, "margin": q_margin, "confirm": q_confirm}
    if s1_agreement == "agree":
        factors["s1"] = 1.0
        reasons.append("s1_agree")
    elif s1_agreement == "disagree":
        factors["s1"] = 0.2
        reasons.append("s1_disagree")
    total = sum(CONFIDENCE_WEIGHTS[k] for k in factors)
    score = sum(CONFIDENCE_WEIGHTS[k] * q for k, q in factors.items()) / total
    if (
        pending_fraction >= PENDING_CAP_FRACTION and not s1_confirmed
    ) or s1_agreement == "disagree":
        score = min(score, MEDIUM_CAP)
    score = round(score, 3)
    return score, confidence_level(score) or "low", reasons


def compute_harvest_series(
    scenes: Iterable[dict[str, Any]],
    *,
    parcel_area_mu: float | None = None,
    thresholds: HarvestProgressThresholds | None = None,
    s1_scenes: Iterable[dict[str, Any]] | None = None,
    crop_type: str | None = None,
    region_code: str | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    adaptive: bool = True,
) -> list[dict[str, Any]]:
    """计算每个有效观测日的已收获占比序列（按日期升序）。

    ``scenes`` 每项需含 ``date``、``pixels``（lonlat_v1 像元列表），可选
    ``scene_id``、``official``、``source``、``algorithm_version``、``stac_item_id``。
    ``s1_scenes`` 每项含 ``date``、``pixels``（VH_db/VV_db）、``relative_orbit``、
    ``algorithm_version``。返回值不含被质控剔除的日期；季外日期以 ``off_season``、
    0% 返回。
    """
    base = thresholds or HarvestProgressThresholds.from_env()
    days = _daily_observations(scenes, base)
    thr, threshold_source, threshold_stats = resolve_thresholds(
        days,
        base,
        crop_type=crop_type,
        region_code=region_code,
        profiles=profiles,
        adaptive=adaptive,
    )
    s1 = _s1_observations(s1_scenes)
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

    refs_of: dict[int, dict] = {}
    out: list[dict[str, Any]] = []
    prev_pct: float | None = None
    prev_season: _Season | None = None
    for k, obs in enumerate(days):
        season = season_of.get(k)
        crop_n = len(season.crop) if season else 0
        done = harvested_at.get(k, set())
        pend = provisional_at.get(k, set())
        if season is None or not crop_n:
            status, pct, count = "off_season", 0.0, 0
            prev_pct, prev_season = None, None
            season = None
        else:
            count = len(done) + len(pend)
            pct = round(100.0 * count / crop_n, 1)
            if count == 0 or pct <= 0.0:
                status = "growing"
            elif pct >= 99.95:
                status = "harvested"
            else:
                status = "harvesting"
        if season is not None and prev_season is not season:
            prev_pct = None
        newly = round(max(0.0, pct - prev_pct), 1) if prev_pct is not None else pct
        peak_obs = days[season.peak] if season else None

        # S1：仅在峰值之后、同轨道同版本与峰值期比较。
        s1_hit = None
        s1_confirmed = False
        if season is not None and s1 and obs.day > peak_obs.day:
            refs = refs_of.setdefault(id(season), _s1_refs(s1, peak_obs.day))
            lo = obs.day - timedelta(days=S1_MATCH_BEFORE_DAYS)
            hi = obs.day + timedelta(days=S1_MATCH_AFTER_DAYS)
            near = [
                c
                for c in (_s1_change(o, refs) for o in s1 if lo <= o.day <= hi)
                if c is not None
            ]
            if near:
                s1_hit = min(near, key=lambda c: abs((c[0].day - obs.day).days))
            if pend:
                limit = obs.day + timedelta(days=thr.confirm_days)
                s1_confirmed = any(
                    c is not None and c[3] == "harvest"
                    for c in (
                        _s1_change(o, refs) for o in s1 if obs.day <= o.day <= limit
                    )
                )
        s1_agreement = None
        if s1_hit is not None:
            kind = s1_hit[3]
            if kind == "ambiguous" or 10.0 <= pct < 50.0:
                s1_agreement = "ambiguous"
            elif (pct >= 50.0) == (kind == "harvest"):
                s1_agreement = "agree"
            else:
                s1_agreement = "disagree"

        # 判定量离阈值的距离。
        if status in ("harvesting", "harvested"):
            gaps = [
                thr.harvest_green - obs.values[key]
                for key in (done | pend)
                if key in obs.values
            ]
            margin = statistics.median(gaps) if gaps else None
        elif status == "growing":
            margin = obs.median_green - thr.harvest_green
        else:
            margin = thr.season_green - obs.median_green
        gap_days = (obs.day - days[k - 1].day).days if k > 0 else None
        confirmed = not pend or s1_confirmed
        score, level, reasons = observation_confidence(
            valid_pct=obs.valid_pct,
            valid_pixel_count=len(obs.values),
            gap_days=gap_days,
            margin=margin,
            pending_fraction=len(pend) / count if count else 0.0,
            s1_agreement=s1_agreement,
            s1_confirmed=s1_confirmed,
            min_valid_pct=thr.min_valid_pct,
        )
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
                "confirmed_by": None
                if not count
                else ("s1" if pend else "s2")
                if confirmed
                else None,
                "official": obs.official,
                "gap_days": gap_days,
                "confidence": score,
                "confidence_level": level,
                "confidence_reasons": reasons,
                "s1_date": s1_hit[0].day.isoformat() if s1_hit else None,
                "s1_delta_vh_db": s1_hit[1] if s1_hit else None,
                "s1_delta_ratio_db": s1_hit[2] if s1_hit else None,
                "s1_agreement": s1_agreement,
                "threshold_source": threshold_source,
                "thresholds": {
                    "season_green": thr.season_green,
                    "harvest_green": thr.harvest_green,
                    "confirm_green": thr.confirm_green,
                    "peak_drop": thr.peak_drop,
                    **({"adaptive": threshold_stats} if threshold_stats else {}),
                },
                "interpolated": False,
            }
        )
        if season is not None:
            prev_pct, prev_season = pct, season
    return out


def interpolate_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在同一生长季相邻观测之间按日线性插值（仅用于查询展示，不入库）。

    观测行原样保留并标记 ``interpolated=false``；插值行 ``interpolated=true``、
    无影像/像元字段，占比单调不减且不超过下一期观测，置信度 =
    0.7 × min(前后观测置信度)。不在最后一期观测之后外推，季外不插值。
    """
    out: list[dict[str, Any]] = []
    ordered = sorted(rows, key=lambda r: r["date"])
    for i, row in enumerate(ordered):
        out.append({**row, "interpolated": False})
        if i + 1 >= len(ordered):
            break
        nxt = ordered[i + 1]
        if (
            row.get("status") == "off_season"
            or nxt.get("status") == "off_season"
            or not row.get("season_start")
            or row.get("season_start") != nxt.get("season_start")
        ):
            continue
        d0 = date.fromisoformat(str(row["date"])[:10])
        d1 = date.fromisoformat(str(nxt["date"])[:10])
        span = (d1 - d0).days
        if span <= 1:
            continue
        p0 = float(row.get("harvested_pct") or 0.0)
        p1 = max(p0, float(nxt.get("harvested_pct") or 0.0))
        c0, c1 = row.get("confidence"), nxt.get("confidence")
        conf = (
            round(INTERPOLATED_FACTOR * min(float(c0), float(c1)), 3)
            if c0 is not None and c1 is not None
            else None
        )
        confirmed = bool(row.get("confirmed", True)) and bool(
            nxt.get("confirmed", True)
        )
        reasons = ["interpolated"] + ([] if confirmed else ["unconfirmed"])
        area = row.get("parcel_area_mu")
        prev = p0
        for step in range(1, span):
            pct = round(min(p1, max(prev, p0 + (p1 - p0) * step / span)), 1)
            if pct <= 0:
                status = "growing"
            elif pct >= 99.95:
                status = "harvested"
            else:
                status = "harvesting"
            out.append(
                {
                    "date": (d0 + timedelta(days=step)).isoformat(),
                    "sensor": row.get("sensor") or "S2",
                    "scene_id": None,
                    "status": status,
                    "harvested_pct": pct,
                    "newly_harvested_pct": round(max(0.0, pct - prev), 1),
                    "harvested_area_mu": round(float(area) * pct / 100.0, 2)
                    if area is not None
                    else None,
                    "parcel_area_mu": area,
                    "valid_pct": None,
                    "mean_ndvi": None,
                    "greenness": None,
                    "peak_greenness": row.get("peak_greenness"),
                    "peak_date": row.get("peak_date"),
                    "season_start": row.get("season_start"),
                    "vegetation_index": None,
                    "confirmed": confirmed,
                    "official": False,
                    "confidence": conf,
                    "confidence_level": confidence_level(conf),
                    "confidence_reasons": reasons,
                    "interpolated": True,
                }
            )
            prev = pct
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
