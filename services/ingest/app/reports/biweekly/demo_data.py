"""Reproducible demo inputs for biweekly report samples. NOT real observations.

示例数据，非实测，不用于农事决策。

本模块只服务于样稿和回归测试：仓库里没有可公开的脱敏实测像元数据，
因此用固定随机种子生成一块虚构田块的像元产品（格式与正式地块产品 lonlat_columns_v1 相同）、
气象序列和种植方案日期，再交给与正式流程相同的代码生成报告：

    TriggerService.prepare_generate_facts（接口侧事实构建）
    → generate_official_biweekly_pdf（下载机侧补充与渲染）

生成的每个文件和每页报告都带有「示例数据」标记。数值只为演示版式、色标和各阶段的指标组合，
不代表任何真实地块、年份或品种，阶段日期也是虚构的计划日期。
正式生成流程（Celery 任务、Internal HTTP、OSS）不会引用本模块。
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

DATA_KIND = "demo"
DATA_NOTICE = "示例数据，非实测，不用于农事决策"
DATA_SOURCE_NOTE = (
    "数据由仓库内固定随机种子的示例数据生成器生成，田块、坐标、阶段日期和气象均为虚构，"
    "仅用于展示报告版式和各生长阶段的指标组合。"
)
LAND_ID = "DEMO-0001"
LAND_NAME = "示例地块（非实测）"
# 虚构位置：只用于换算像元间距，不对应任何真实地块。
LON0, LAT0 = 115.5, 36.5
PIXEL_M = 10.0
GRID_NX, GRID_NY = 30, 24
FIELD_OUTLINE = [(0, 3), (4, 0), (27, 0), (30, 5), (30, 21), (25, 24), (2, 24), (0, 20)]

# 虚构的冬小麦种植方案计划日期（阶段名取自 phase_policy 的正式生育期名称）。
PLAN_INTERVALS = [
    {"stage_code": "播种期", "start": "2025-10-05", "end_exclusive": "2025-10-12"},
    {"stage_code": "出苗期", "start": "2025-10-12", "end_exclusive": "2025-10-22"},
    {"stage_code": "分蘖期", "start": "2025-10-22", "end_exclusive": "2025-12-05"},
    {"stage_code": "越冬期", "start": "2025-12-05", "end_exclusive": "2026-02-20"},
    {"stage_code": "返青期", "start": "2026-02-20", "end_exclusive": "2026-03-15"},
    {"stage_code": "拔节孕穗期", "start": "2026-03-15", "end_exclusive": "2026-04-20"},
    {"stage_code": "抽穗期", "start": "2026-04-20", "end_exclusive": "2026-05-05"},
    {"stage_code": "灌浆期", "start": "2026-05-05", "end_exclusive": "2026-05-28"},
    {"stage_code": "成熟期", "start": "2026-05-28", "end_exclusive": "2026-06-10"},
]

# 三个样稿场景。ndvi 是该日全田 NDVI 的大致水平；patch_depth 是局部斑块相对全田的差值。
CASES: dict[str, dict[str, Any]] = {
    "s1-tillering": {
        "title": "营养生长阶段（分蘖期）",
        "seed": 20251027,
        "period": ("2025-10-27", "2025-11-10"),
        "patch": {"cx": 23.0, "cy": 18.0, "radius": 3.6},
        "scenes": [
            {"date": "2025-10-21", "ndvi": 0.215, "patch_depth": -0.07, "cloud": 3.0},
            {"date": "2025-10-31", "ndvi": 0.245, "patch_depth": -0.085, "cloud": 6.0},
            {"date": "2025-11-03", "cloud": 78.0, "rejected": True},
            {"date": "2025-11-07", "ndvi": 0.262, "patch_depth": -0.10, "cloud": 1.0},
        ],
        "radar": 0,
        "weather": {"t_mean": 11.0, "t_trend": -0.18, "rain_days": {3: 6.4, 4: 2.1, 9: 0.6}, "et0": 1.5},
        "surrounding": False,
    },
    "s2-jointing": {
        "title": "营养生长与生殖生长并进阶段（拔节孕穗期）",
        "seed": 20260330,
        "period": ("2026-03-30", "2026-04-13"),
        "patch": {"cx": 24.5, "cy": 9.0, "radius": 3.2},
        "scenes": [
            {"date": "2026-03-25", "ndvi": 0.60, "patch_depth": -0.16, "cloud": 4.0},
            {"date": "2026-04-01", "cloud": 64.0, "rejected": True},
            {"date": "2026-04-04", "ndvi": 0.68, "patch_depth": -0.19, "cloud": 2.0},
            # 最近一次观测西南角有云影：像元级掩膜 clear=0，这些像元不参与统计。
            {
                "date": "2026-04-09",
                "ndvi": 0.74,
                "patch_depth": -0.21,
                "cloud": 9.0,
                "shadow": {"cx": 4.0, "cy": 4.5, "radius": 4.6},
            },
        ],
        "radar": 2,
        "weather": {"t_mean": 13.5, "t_trend": 0.22, "rain_days": {1: 11.8, 2: 3.5, 11: 4.2}, "et0": 3.1},
        "surrounding": True,
    },
    "s3-grainfill": {
        "title": "生殖生长阶段（灌浆期）",
        "seed": 20260511,
        "period": ("2026-05-11", "2026-05-25"),
        "patch": None,
        "scenes": [
            {"date": "2026-05-06", "ndvi": 0.78, "cloud": 5.0},
            {"date": "2026-05-14", "ndvi": 0.72, "cloud": 0.0},
            {"date": "2026-05-22", "ndvi": 0.64, "cloud": 3.0},
        ],
        "radar": 1,
        # 降水有 2 天缺测、ET0 有 3 天缺测，用来演示缺测不计入合计、图上不画成 0。
        "weather": {
            "t_mean": 22.5,
            "t_trend": 0.15,
            "rain_days": {6: 8.6},
            "et0": 4.8,
            "missing_rain": (4, 5),
            "missing_et0": (4, 5, 12),
        },
        "surrounding": False,
    },
}


def _day(value: str) -> date:
    return date.fromisoformat(value)


def field_grid() -> dict[str, np.ndarray]:
    """Pixel centres of the invented field on a 10 m grid."""
    from matplotlib.path import Path as MplPath

    gx, gy = np.meshgrid(np.arange(GRID_NX) + 0.5, np.arange(GRID_NY) + 0.5)
    inside = MplPath(FIELD_OUTLINE).contains_points(np.column_stack([gx.ravel(), gy.ravel()]))
    gx, gy = gx.ravel()[inside], gy.ravel()[inside]
    dlat = PIXEL_M / 111320.0
    dlon = PIXEL_M / (111320.0 * math.cos(math.radians(LAT0)))
    return {
        "gx": gx,
        "gy": gy,
        "lon": np.round(LON0 + (gx - GRID_NX / 2) * dlon, 7),
        "lat": np.round(LAT0 + (gy - GRID_NY / 2) * dlat, 7),
    }


def processing_bounds() -> list[float]:
    """A 10 km square around the invented field, like the official processing window."""
    half_lat = 5000.0 / 111320.0
    half_lon = 5000.0 / (111320.0 * math.cos(math.radians(LAT0)))
    return [round(LON0 - half_lon, 6), round(LAT0 - half_lat, 6), round(LON0 + half_lon, 6), round(LAT0 + half_lat, 6)]


def _smooth_noise(rng: np.random.Generator) -> np.ndarray:
    """Spatially correlated noise on the full grid (soil-like pattern), unit scale."""
    raw = rng.normal(size=(GRID_NY + 4, GRID_NX + 4))
    acc = np.zeros((GRID_NY, GRID_NX))
    for dy in range(5):
        for dx in range(5):
            acc += raw[dy : dy + GRID_NY, dx : dx + GRID_NX]
    acc /= 25.0
    return acc / max(float(acc.std()), 1e-9)


def _gauss(gx: np.ndarray, gy: np.ndarray, spec: dict[str, float]) -> np.ndarray:
    return np.exp(-(((gx - spec["cx"]) ** 2 + (gy - spec["cy"]) ** 2) / (2.0 * spec["radius"] ** 2)))


def build_product(case: dict[str, Any], scene: dict[str, Any], grid: dict[str, np.ndarray]) -> dict[str, Any]:
    """One parcel product in the official lonlat_columns_v1 layout, marked as demo."""
    day = scene["date"]
    base: dict[str, Any] = {
        "data_kind": DATA_KIND,
        "data_notice": DATA_NOTICE,
        "land_id": LAND_ID,
        "date": day,
        "sensor": "S2",
        "acquired_at": f"{day}T03:12:00+00:00",
        "cloud_cover": float(scene["cloud"]),
        "parcel_cloud_cover_pct": float(scene["cloud"]),
        # 含 _L2A 的示例编号：流程据此跳过影像目录检索，不发起网络请求。
        "stac_item_id": f"DEMO_{day.replace('-', '')}_0_L2A",
        "processing_window": {"bounds": processing_bounds()},
    }
    if scene.get("rejected"):
        # 云量超限的过境：正式产品同样只有景信息，没有可用像元。
        base["pixel_count"] = 0
        return base

    gx, gy = grid["gx"], grid["gy"]
    ix, iy = gx.astype(int), gy.astype(int)
    static_rng = np.random.default_rng(case["seed"])
    soil = _smooth_noise(static_rng)[iy, ix]
    outliers = static_rng.choice(gx.size, size=3, replace=False)
    day_rng = np.random.default_rng(case["seed"] + _day(day).toordinal())

    ndvi = scene["ndvi"] + 0.012 * (gx - GRID_NX / 2) / GRID_NX + 0.022 * soil
    if case.get("patch") and scene.get("patch_depth"):
        ndvi = ndvi + scene["patch_depth"] * _gauss(gx, gy, case["patch"])
    ndvi = ndvi + day_rng.normal(scale=0.008, size=gx.size)
    # 个别极端像元（如田边树木、田埂）：用来检验统计量和色标不受单个像元支配。
    ndvi[outliers[0]] = min(0.97, scene["ndvi"] + 0.42)
    ndvi[outliers[1]] = min(0.95, scene["ndvi"] + 0.36)
    ndvi[outliers[2]] = -0.06
    ndvi = np.clip(ndvi, -1.0, 1.0)

    def derive(intercept: float, slope: float, scale: float) -> np.ndarray:
        return np.clip(intercept + slope * ndvi + day_rng.normal(scale=scale, size=gx.size), -1.0, 1.0)

    ndre = derive(0.04, 0.52, 0.006)
    ndmi = derive(-0.22, 0.55, 0.008)
    # 版式探针用：把指数整体平移到色标上沿之外，检验变化图是否裁切。不是生物学校正。
    if scene.get("ndre_shift"):
        ndre = np.clip(ndre + float(scene["ndre_shift"]), -1.0, 1.0)
    if scene.get("ndmi_shift"):
        ndmi = np.clip(ndmi + float(scene["ndmi_shift"]), -1.0, 1.0)
    columns: dict[str, list[Any]] = {
        "lon": [float(v) for v in grid["lon"]],
        "lat": [float(v) for v in grid["lat"]],
        "NDVI": [round(float(v), 4) for v in ndvi],
        "EVI": [round(float(v), 4) for v in derive(0.05, 0.60, 0.007)],
        "NDRE": [round(float(v), 4) for v in ndre],
        "NDMI": [round(float(v), 4) for v in ndmi],
    }
    clear = np.ones(gx.size, dtype=int)
    if scene.get("shadow"):
        clear[_gauss(gx, gy, scene["shadow"]) > 0.55] = 0
    columns["clear"] = [int(v) for v in clear]
    base["parcel_cloud_cover_pct"] = round(100.0 * float(np.mean(clear == 0)), 1) if scene.get("shadow") else base[
        "parcel_cloud_cover_pct"
    ]
    base["pixel_count"] = int(gx.size)
    for name in ("NDVI", "EVI", "NDRE", "NDMI"):
        values = np.asarray(columns[name], dtype=float)
        # 与正式产品一致：产品级均值按全部像元计算，不区分云影。
        base[f"{name.lower()}_avg"] = round(float(values.mean()), 4)
        base[f"{name.lower()}_min"] = round(float(values.min()), 4)
        base[f"{name.lower()}_max"] = round(float(values.max()), 4)
    base["pixel_data"] = {"format": "lonlat_columns_v1", "columns": columns}
    return base


def build_weather(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Invented daily weather: 30 days before the period plus the period itself."""
    start, end_exclusive = (_day(v) for v in case["period"])
    spec = case["weather"]
    rng = np.random.default_rng(case["seed"] + 7)
    rows = []
    cursor = start - timedelta(days=30)
    while cursor < end_exclusive:
        offset = (cursor - start).days
        mean = spec["t_mean"] + spec["t_trend"] * offset + float(rng.normal(scale=1.6))
        swing = 5.5 + float(rng.uniform(-1.0, 1.5))
        rain: float | None = 0.0
        if offset >= 0:
            rain = float(spec["rain_days"].get(offset, 0.0))
        elif rng.uniform() < 0.14:
            rain = round(float(rng.uniform(0.4, 9.0)), 1)
        et0: float | None = round(max(0.3, spec["et0"] + float(rng.normal(scale=0.35)) - (0.9 if rain else 0.0)), 1)
        if offset in spec.get("missing_rain", ()):
            rain = None
        if offset in spec.get("missing_et0", ()):
            et0 = None
        rows.append(
            {
                "date": cursor.isoformat(),
                "precipitation_mm": rain,
                "min_c": round(mean - swing, 1),
                "max_c": round(mean + swing, 1),
                "et0_mm": et0,
            }
        )
        cursor += timedelta(days=1)
    return rows


def synthetic_truecolor(*, stac_item_id: str, bounds: Any, size: int = 768) -> np.ndarray:
    """An invented true-colour scene for the surrounding-farmland page. Not imagery."""
    seed = int("".join(ch for ch in str(stac_item_id) if ch.isdigit())[:8] or "1")
    rng = np.random.default_rng(seed % 100000)
    layout = np.random.default_rng(4242)  # 地物布局各日期相同，只有颜色随日期略有变化
    image = np.zeros((size, size, 3), dtype=np.float32)
    block = 26
    for r0 in range(0, size, block):
        for c0 in range(0, size, block):
            kind = layout.uniform()
            shade = layout.uniform(-1.0, 1.0)
            if kind < 0.68:
                colour = np.array([78 + 8 * shade, 104 + 12 * shade, 66 + 6 * shade])
            elif kind < 0.88:
                colour = np.array([128 + 12 * shade, 112 + 10 * shade, 84 + 8 * shade])
            else:
                colour = np.array([96 + 10 * shade, 104 + 10 * shade, 74 + 8 * shade])
            image[r0 : r0 + block, c0 : c0 + block] = colour + rng.normal(scale=1.5, size=3)
    image += layout.normal(scale=2.2, size=image.shape)
    # 村庄：亮度高、纹理粗，会被农田识别规则排除。
    for r0, c0, h, w in ((150, 560, 70, 90), (560, 170, 60, 80)):
        image[r0 : r0 + h, c0 : c0 + w] = layout.uniform(40, 215, size=(h, w, 1)) + layout.normal(
            scale=10, size=(h, w, 3)
        )
    # 河道：暗色水体。
    for col in range(size):
        row = int(size * 0.82 + 22 * math.sin(col / 60.0))
        image[max(0, row - 4) : row + 4, col] = np.array([6.0, 9.0, 12.0])
    # 本田所在位置与周边农田同色系，略深。
    minx, miny, maxx, maxy = (float(v) for v in bounds)
    grid = field_grid()
    cols = np.clip(((grid["lon"] - minx) / (maxx - minx) * size).astype(int), 0, size - 1)
    rows = np.clip(((maxy - grid["lat"]) / (maxy - miny) * size).astype(int), 0, size - 1)
    image[rows.min() : rows.max() + 1, cols.min() : cols.max() + 1] = np.array([74.0, 108.0, 62.0])
    return np.clip(image, 0, 255).astype(np.uint8)


def api_observation(product: dict[str, Any]) -> dict[str, Any]:
    """The row the API hands to TriggerService for one scene (same keys as _to_observation)."""
    cloud = product.get("parcel_cloud_cover_pct")
    has_values = product.get("ndvi_avg") is not None
    eligible = has_values and cloud is not None and float(cloud) <= 30.0
    reason = None
    if not eligible:
        reason = "cloud_over_limit" if cloud is not None and float(cloud) > 30.0 else "no_index_values"
    return {
        "date": product["date"],
        "sensor": "S2",
        "quality_status": "eligible_descriptive" if eligible else "excluded",
        "exclusion_reason": reason,
        "cloud_cover": product.get("cloud_cover"),
        "parcel_cloud_cover_pct": cloud,
        "ndvi_avg": product.get("ndvi_avg"),
        "evi_avg": product.get("evi_avg"),
        "ndre_avg": product.get("ndre_avg"),
        "ndmi_avg": product.get("ndmi_avg"),
    }


class _DemoCatalog:
    """Just enough catalogue for TriggerService to build facts for one period."""

    def __init__(self, period: dict[str, Any]):
        self._period = period

    def period(self, land_id: str, season_id: str, period_id: str) -> dict[str, Any]:
        return dict(self._period)

    def periods(self, land_id: str, season_id: str) -> list[dict[str, Any]]:
        return [dict(self._period)]

    def load_snapshot(self, name: str) -> dict[str, Any]:
        return {}

    def v8_optical_metrics(self) -> dict[str, Any]:
        return {}


def build_case(name: str, work_dir: str | Path, case: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write one case's inputs under work_dir and return facts + paths.

    Facts come from the same TriggerService code the API uses, fed with the
    demo scenes as observation overrides and the demo plan as the native plan.
    ``case`` 只用于不进入 CASES 的版式探针，避免三个阶段样稿的断言被探针改掉。
    """
    from agric_satellite_analysis_common.biweekly.trigger import TriggerService
    from app.reports.biweekly.weather_fetch import summarize_weather_rows

    case = CASES[name] if case is None else case
    work = Path(work_dir)
    products_dir = work / "products"
    products_dir.mkdir(parents=True, exist_ok=True)
    grid = field_grid()
    products = [build_product(case, scene, grid) for scene in case["scenes"]]
    for product in products:
        path = products_dir / f"{product['date']}_S2.json"
        path.write_text(json.dumps(product, ensure_ascii=False) + "\n", encoding="utf-8")

    start, end_exclusive = case["period"]
    end_inclusive = (_day(end_exclusive) - timedelta(days=1)).isoformat()
    period = {
        "period_id": "demo",
        "start": start,
        "end_exclusive": end_exclusive,
        "end_inclusive": end_inclusive,
        "days": (_day(end_exclusive) - _day(start)).days,
        "s1_products": int(case["radar"]),
    }
    weather = build_weather(case)
    period_rows = [row for row in weather if start <= row["date"] <= end_inclusive]
    before_rows = [row for row in weather if row["date"] < start]
    rows = [api_observation(product) for product in products]
    job = {
        "job_id": f"demo-{name}",
        "land_id": LAND_ID,
        "season_id": "demo-season",
        "period_id": "demo",
        "payload": {
            "land_name": LAND_NAME,
            "observations_override": [row for row in rows if row["date"] >= start],
            "prior_observations_override": [row for row in rows if row["date"] < start],
            "native_plan": {"intervals": PLAN_INTERVALS},
            "weather_daily": weather,
            "weather_period": summarize_weather_rows(period_rows),
            "weather_antecedent": summarize_weather_rows(before_rows),
            "s1_count": int(case["radar"]),
            "observation_source": "demo_fixture",
        },
    }
    if case.get("sowing_date"):
        job["payload"]["sowing_date"] = case["sowing_date"]
    if isinstance(case.get("historical_same_period"), list):
        # 只放调用方已经准备好的归档，不向任何服务回填往年景。
        job["payload"]["historical_same_period"] = case["historical_same_period"]
    service = TriggerService(work / "trigger", _DemoCatalog(period), auto_run=False)
    facts, status = service.prepare_generate_facts(job)
    facts.update(
        {
            "data_kind": DATA_KIND,
            "data_notice": DATA_NOTICE,
            "data_source_note": DATA_SOURCE_NOTE,
            "crop_name": "冬小麦",
            "report_date": end_exclusive,
            "generated_at": f"{end_exclusive}T08:00:00+08:00",
            "weather_note": "本样稿的气象序列为示例数据。",
        }
    )
    if case["surrounding"]:
        facts["surrounding_basemap_note"] = "底图为示例合成图像，非卫星影像"
    facts_path = work / "facts.json"
    facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return {
        "name": name,
        "case": case,
        "facts": facts,
        "status": status,
        "products": products,
        "products_dir": products_dir,
        "facts_path": facts_path,
    }


def no_network_weather(**_kwargs: Any) -> dict[str, Any]:
    """Weather is already in the demo facts; never reach an external service."""
    return {}


def no_truecolor(**_kwargs: Any) -> None:
    return None


def render_case(name: str, out_dir: str | Path) -> dict[str, Any]:
    """Build inputs and render the PDF through the official generate entry."""
    from app.reports.biweekly.generate import generate_official_biweekly_pdf

    out = Path(out_dir) / name
    built = build_case(name, out / "inputs")
    pdf = out / f"biweekly-demo-{name}.pdf"
    generate_official_biweekly_pdf(
        built["facts"],
        pdf,
        figures_dir=out / "figures",
        products_dir=built["products_dir"],
        latitude=LAT0,
        longitude=LON0,
        fetch_weather=no_network_weather,
        fetch_truecolor=synthetic_truecolor if built["case"]["surrounding"] else no_truecolor,
        facts_sidecar=out / "facts-enriched.json",
    )
    built["pdf"] = pdf
    built["out_dir"] = out
    return built


def _layout_case() -> dict[str, Any]:
    """Grain-fill dates packed together, with NDMI above the fixed colour scale.

    复现报告截图里的问题：NDMI 中位数高于 0.50、相邻日期的标签挤在一起、
    一景因云量未采用。数字是示例，不是生产地块 32696。
    """
    case = {
        "title": "版式探针（灌浆期，NDMI 超出色标）",
        "seed": 20260506,
        "period": ("2026-05-02", "2026-05-16"),
        "patch": {"cx": 8.0, "cy": 8.0, "radius": 3.4},
        "scenes": [
            {"date": "2026-04-28", "ndvi": 0.84, "ndre_shift": 0.24, "ndmi_shift": 0.30, "cloud": 4.0},
            {"date": "2026-05-03", "ndvi": 0.86, "ndre_shift": 0.26, "ndmi_shift": 0.32, "patch_depth": -0.16, "cloud": 2.0},
            {"date": "2026-05-06", "ndvi": 0.87, "ndre_shift": 0.27, "ndmi_shift": 0.36, "patch_depth": -0.18, "cloud": 1.0},
            {"date": "2026-05-08", "ndvi": 0.85, "ndre_shift": 0.25, "ndmi_shift": 0.34, "patch_depth": -0.17, "cloud": 6.0},
            {"date": "2026-05-10", "ndvi": 0.84, "ndre_shift": 0.24, "ndmi_shift": 0.33, "patch_depth": -0.15, "cloud": 3.0},
            {"date": "2026-05-11", "ndvi": 0.83, "ndre_shift": 0.23, "ndmi_shift": 0.38, "patch_depth": -0.19, "cloud": 0.0},
            {"date": "2026-05-13", "cloud": 42.0, "rejected": True},
        ],
        "radar": 0,
        # 降水少、ET0 完整，和升高的 NDMI 方向相反，用来看冲突句是否被收成一个结论。
        "weather": {
            "t_mean": 21.0,
            "t_trend": 0.05,
            "rain_days": {2: 1.2, 9: 2.4},
            "et0": 4.2,
        },
        "surrounding": False,
        "sowing_date": "2025-10-06",
        "historical_same_period": [
            {
                "land_id": LAND_ID,
                "crop_type": "冬小麦",
                "sowing_date": "2024-10-08",
                "observations": [
                    {
                        "date": "2024-05-12",
                        "metrics": {
                            "ndre": {"median": 0.70, "n_valid": 420, "statistic": "median"},
                            "ndmi": {"median": 0.55, "n_valid": 420, "statistic": "median"},
                        },
                    }
                ],
            }
        ],
    }
    return case


def render_layout_fixture(out_dir: str | Path) -> dict[str, Any]:
    """Dense two-metric page, uncropped trend, comparable archive, conflicting water evidence."""
    from app.reports.biweekly.generate import generate_official_biweekly_pdf

    out = Path(out_dir) / "layout-dense"
    case = _layout_case()
    built = build_case("layout-dense", out / "inputs", case)
    built["facts"]["data_source_note"] = (
        str(built["facts"].get("data_source_note") or "")
        + "本样稿不是生产地块 32696 的结论。"
    )
    pdf = out / "biweekly-layout-dense.pdf"
    generate_official_biweekly_pdf(
        built["facts"],
        pdf,
        figures_dir=out / "figures",
        products_dir=built["products_dir"],
        latitude=LAT0,
        longitude=LON0,
        fetch_weather=no_network_weather,
        fetch_truecolor=no_truecolor,
        facts_sidecar=out / "facts-enriched.json",
        offline=True,
    )
    built["pdf"] = pdf
    built["out_dir"] = out
    return built


def render_water_insufficient(out_dir: str | Path) -> dict[str, Any]:
    """No NDMI pair and no weather: one insufficiency sentence, not a blank page and not 'no risk'."""
    from app.reports.biweekly.generate import generate_official_biweekly_pdf

    out = Path(out_dir) / "water-insufficient"
    built = build_case("s1-tillering", out / "inputs")
    for product in built["products"]:
        columns = ((product.get("pixel_data") or {}).get("columns") or {})
        columns.pop("NDMI", None)
        for key in ("ndmi_avg", "ndmi_min", "ndmi_max"):
            product.pop(key, None)
        path = built["products_dir"] / f"{product['date']}_S2.json"
        path.write_text(json.dumps(product, ensure_ascii=False) + "\n", encoding="utf-8")
    facts = built["facts"]
    for key in ("weather_daily", "weather_period", "weather_antecedent"):
        facts.pop(key, None)
    facts["data_source_note"] = (
        str(facts.get("data_source_note") or "")
        + "本样稿去掉了 NDMI 和气象，用来展示水分证据不足，不是生产地块 32696 的结论。"
    )
    pdf = out / "biweekly-water-insufficient.pdf"
    generate_official_biweekly_pdf(
        facts,
        pdf,
        figures_dir=out / "figures",
        products_dir=built["products_dir"],
        latitude=LAT0,
        longitude=LON0,
        fetch_weather=no_network_weather,
        fetch_truecolor=no_truecolor,
        facts_sidecar=out / "facts-enriched.json",
        offline=True,
    )
    built["pdf"] = pdf
    built["out_dir"] = out
    return built
