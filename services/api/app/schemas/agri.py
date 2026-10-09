"""Pydantic schemas for agric_satellite 地块与 S1/S2 产品数据。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


Sensor = Literal["S1", "S2"]


class LandParcelOut(BaseModel):
    """land_parcels detail including boundary_geojson."""

    land_id: str
    source_parcel_id: str | None = None
    tile_id: str
    virtual_tile_id: str | None = None
    project_key: str | None = None
    tile_assignment_type: str | None = None
    tile_anchor_land_id: str | None = None
    land_name: str | None = None
    group_id: str | None = None
    group_name: str | None = None
    org_code: str | None = None
    org_name: str | None = None
    base_id: str | None = None
    province_code: str | None = None
    province_name: str | None = None
    city_code: str | None = None
    city_name: str | None = None
    county_code: str | None = None
    county_name: str | None = None
    town_code: str | None = None
    town_name: str | None = None
    village_code: str | None = None
    village_name: str | None = None
    original_area_mu: float | None = None
    land_area_mu: float | None = None
    soil_property: str | None = None
    current_batch: str | None = None
    land_status: str | None = None
    source_update_time: datetime | None = None
    boundary_geojson: dict[str, Any]
    boundary_srid: int | None = None
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    source_properties: dict[str, Any] | None = None
    source_file: str | None = None
    source_feature_index: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SceneProductOut(BaseModel):
    """parcel_scene_products averages for growth curves (no pixel_data by default)."""

    land_id: str
    tile_id: str
    date: date
    sensor: Sensor
    scene_id: str
    land_name: str | None = None
    cloud_cover: float | None = None
    cloud_cover_over_30: bool | None = None
    parcel_cloud_cover_pct: float | None = None
    parcel_cloud_source: str | None = Field(
        default=None,
        description=(
            "How parcel_cloud_cover_pct was computed: scl or lonlat_clear. "
            "Missing means unknown, a legacy window-fill value, or an "
            "untrusted 0 that was dropped in favor of STAC cloud_cover."
        ),
    )
    source: str | None = Field(
        default=None,
        description=(
            "Normalized product_source with legacy pixel_data.source fallback: "
            "stac_direct / stac_s1_direct for raw observations, "
            "uncrtaints_decloud for the additive cloud-removal product."
        ),
    )
    stac_item_id: str | None = Field(
        default=None,
        description="Original STAC item identifier; scene_id may remain a legacy stable key.",
    )
    algorithm_version: str | None = Field(
        default=None,
        description="Processing implementation version recorded when the product was created.",
    )
    analysis_grid: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Output-grid CRS/dimensions and geodesic cell-spacing estimate; "
            "spacing is not the sensor's native spatial resolution."
        ),
    )
    radiometric_calibration: dict[str, Any] | None = Field(
        default=None,
        description="Sentinel-1辐射定标方法及处理限制；历史产品可能没有此元数据。",
    )
    quality_metrics: dict[str, Any] | None = Field(
        default=None,
        description="按VV/VH等波段记录有效像元比例与算法口径；历史产品可能没有此元数据。",
    )
    decloud_quality: str | None = Field(
        default=None,
        description=(
            "good | fair | bad. Only good feeds official drought metrics. "
            "Fair/bad are stored and marked possibly unreliable."
        ),
    )
    decloud_score: float | None = None
    decloud_reasons: list[str] | None = Field(
        default=None,
        description="Quality-gate reason codes for the additive decloud product.",
    )
    relative_orbit: int | None = Field(
        default=None,
        description="Sentinel-1 relative orbit (1-175) when parseable from scene_id.",
    )
    json_oss_key: str | None = None
    pixel_data_url: str | None = None
    pixel_count: int | None = None
    # S2 optical
    ndvi_avg: float | None = None
    ndvi_min: float | None = None
    ndvi_max: float | None = None
    evi_avg: float | None = None
    evi_min: float | None = None
    evi_max: float | None = None
    ndmi_avg: float | None = None
    ndmi_min: float | None = None
    ndmi_max: float | None = None
    ndre_avg: float | None = None
    ndre_min: float | None = None
    ndre_max: float | None = None
    mndwi_avg: float | None = None
    mndwi_min: float | None = None
    mndwi_max: float | None = None
    cire_avg: float | None = None
    cire_min: float | None = None
    cire_max: float | None = None
    # S1 SAR
    vv_avg: float | None = None
    vv_min: float | None = None
    vv_max: float | None = None
    vh_avg: float | None = None
    vh_min: float | None = None
    vh_max: float | None = None
    generated_at_shanghai: str | None = None
    ingested_at: datetime | None = None
    pixel_data: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Legacy gridified pixels {grid, pixels:[[row,col,...]]}. "
            "Only used as fallback when DB lonlat_v1 / OSS lon/lat are unavailable; "
            "omitted when pixels_lonlat is populated. Present only with include_pixels=1."
        ),
    )
    pixels_lonlat: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Preferred lon/lat pixels when include_pixels=1 (DB lonlat_v1 primary, "
            "else OSS). S2: {lon,lat,clear?,NDVI,EVI,NDMI,NDRE,CIre,MNDWI}; "
            "S1: {lon,lat,VV_db,VH_db}."
        ),
    )
    rgb_url: str | None = Field(
        default=None,
        description="Parcel true-color RGB preview URL from OSS JSON (include_pixels=1).",
    )
    large_rgb_url: str | None = Field(
        default=None,
        description="Larger true-color RGB preview URL from OSS JSON (include_pixels=1).",
    )
    heatmap_url: str | None = Field(
        default=None,
        description=(
            "Optional pre-rendered heatmap PNG URL from OSS JSON (include_pixels=1). "
            "Falls back to s2_heatmap_url when heatmap_url is absent."
        ),
    )
    s2_heatmap_url: str | None = Field(
        default=None,
        description="Sentinel-2 heatmap PNG URL from OSS JSON (include_pixels=1).",
    )
    pixels_source: Literal["db_lonlat", "oss", "db_grid"] | None = Field(
        default=None,
        description=(
            "Which pixel payload is authoritative when include_pixels=1: "
            "db_lonlat (DB pixel_data.format=lonlat_v1), oss, or db_grid."
        ),
    )


class SensorSceneSummary(BaseModel):
    sensor: Sensor
    count: int
    date_min: date | None = None
    date_max: date | None = None
    latest_ndvi_avg: float | None = None
    latest_evi_avg: float | None = None
    latest_vv_avg: float | None = None
    latest_vh_avg: float | None = None
    latest_date: date | None = None


class LandScenesSummaryOut(BaseModel):
    land_id: str
    total: int
    sensors: list[SensorSceneSummary]


class AgriTableCount(BaseModel):
    table: str
    count: int


class AgriStatsOut(BaseModel):
    schema_name: str = "agric_satellite"
    tables: list[AgriTableCount]
    note: str | None = None


# ── China overview (全国态势) ───────────────────────────────────────

OverviewLevel = Literal["country", "province", "city", "county"]


class OverviewRegionNode(BaseModel):
    level: OverviewLevel
    code: str | None = None
    name: str


class OverviewFilters(BaseModel):
    from_date: date = Field(alias="from")
    to_date: date = Field(alias="to")
    cloud_max_pct: float = 30
    phenology_months: list[int] = Field(default_factory=lambda: [6, 7, 8, 9])
    weak_ndvi_lt: float = 0.25

    model_config = {"populate_by_name": True}


class OverviewTotals(BaseModel):
    parcel_count: int
    area_mu: float


class OverviewDroughtCounts(BaseModel):
    severe: int = 0
    moderate: int = 0
    mild: int = 0
    normal: int = 0
    unknown: int = 0
    area_mu: dict[str, float] = Field(default_factory=dict)


class OverviewFloodCounts(BaseModel):
    flood_severe: int = 0
    flood_moderate: int = 0
    flood_mild: int = 0
    # Backward: open water ≈ severe+moderate
    flood: int = 0
    # Alias of flood_mild (former wet band)
    wet: int = 0
    dry: int = 0
    unknown: int = 0
    area_mu: dict[str, float] = Field(default_factory=dict)


class OverviewWeakGrowth(BaseModel):
    parcel_count: int = 0
    area_mu: float = 0.0


class OverviewChildOut(BaseModel):
    level: OverviewLevel
    code: str | None = None
    name: str
    parcel_count: int = 0
    drought_severe: int = 0
    drought_alert: int = 0  # severe + moderate + mild
    flood: int = 0  # open water: severe + moderate
    flood_alert: int = 0  # severe + moderate + mild
    weak_growth: int = 0
    area_mu: float = 0.0
    # 地图着色使用受影响地块/全部地块的比例，None 兼容旧快照，避免把旧数据误认为 0%。
    drought_ratio: float | None = None
    flood_ratio: float | None = None
    weak_growth_ratio: float | None = None


class OverviewStatsOut(BaseModel):
    region: dict[str, Any]
    filters: dict[str, Any]
    totals: OverviewTotals
    drought: OverviewDroughtCounts
    flood: OverviewFloodCounts
    weak_growth: OverviewWeakGrowth
    children: list[OverviewChildOut]


class OverviewRegionOut(BaseModel):
    level: OverviewLevel
    code: str | None = None
    name: str
    parcel_count: int = 0
    area_mu: float = 0.0


class OverviewRegionsOut(BaseModel):
    parent_level: OverviewLevel | None = None
    parent_code: str | None = None
    parent_name: str | None = None
    children: list[OverviewRegionOut]


class OverviewWeakParcelOut(BaseModel):
    land_id: str
    land_name: str | None = None
    province_name: str | None = None
    city_name: str | None = None
    county_name: str | None = None
    land_area_mu: float = 0.0
    ndvi_avg: float
    scene_date: date | None = None
    cloud_pct: float | None = None


class OverviewWeakParcelsOut(BaseModel):
    total: int
    items: list[OverviewWeakParcelOut]


class HarvestDetectOut(BaseModel):
    """Observation-only harvest detection for one growing window."""

    land_id: str
    status: str  # detected | uncertain | no_growth
    harvest_date: date | None = None
    confidence: str | None = None
    scene_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    alternates: list[dict[str, Any]] = Field(default_factory=list)
    window: dict[str, Any] = Field(default_factory=dict)


class NdviDayGradeShareItem(BaseModel):
    """One observation day: pixel NDVI 图一 grade shares (no raw pixels)."""

    date: date
    counts: dict[str, int]
    pct: dict[str, float]
    n: int
    mean: float | None = None
    scene_id: str | None = None


class NdviDayGradeSharesOut(BaseModel):
    land_id: str
    items: list[NdviDayGradeShareItem]
    rule_zh: str = "红<0.25 / 橙0.25–0.35 / 黄0.35–0.50 / 绿≥0.50"


class HarvestProgressItem(BaseModel):
    """一个观测日（或 interpolate=daily 时的插值日）的已收获面积占比，季内单调不减。"""

    date: date
    sensor: str = "S2"
    harvested_pct: float = Field(
        description="本季已收获作物像元占本季作物像元百分比 0–100，季内单调不减"
    )
    newly_harvested_pct: float = Field(
        description="较同季上一有效观测日新增的百分点，不为负；新一季首期等于本期占比"
    )
    harvested_area_mu: float | None = None
    status: str = Field(
        description="off_season（季外，0%）| growing（本季未开始收获）| harvesting | harvested"
    )
    valid_pct: float | None = Field(None, description="无云有效像元占比（插值行为空）")
    mean_ndvi: float | None = Field(
        None, description="有效像元原始 NDVI 均值（仅供参考）"
    )
    greenness: float | None = Field(
        None, description="有效像元统一绿度中位数（0≈裸土，1≈茂密植被）"
    )
    peak_greenness: float | None = Field(None, description="本季地块绿度峰值")
    peak_date: date | None = Field(None, description="本季地块绿度峰值日期")
    season_start: date | None = Field(None, description="本季返青日期（季标识）")
    vegetation_index: str | None = Field(
        None, description="本期使用的植被指数（NDVI/EVI，按产品辐射定标来源选择）"
    )
    confirmed: bool = Field(
        True, description="false 表示含尚待下一期影像确认的候选像元（仅最新几期）"
    )
    scene_id: str | None = None
    official: bool = True
    confidence: float | None = Field(
        None, ge=0, le=1, description="本期结果置信度 0–1（公式见 ADR）"
    )
    confidence_level: Literal["high", "medium", "low"] | None = Field(
        None, description="high ≥0.75，medium ≥0.50，其余 low"
    )
    confidence_reasons: list[str] = Field(
        default_factory=list,
        description=(
            "原因码：low_valid_pct、few_pixels、long_gap、small_margin、unconfirmed、"
            "s1_confirmed、s1_agree、s1_disagree、residue_signature、suspected_harvest、"
            "promoted_bare、promoted_abrupt、promoted_s1、interpolated"
        ),
    )
    confirmed_by: Literal["s2", "s1"] | None = Field(
        None, description="已确认的依据：下一期光学（s2）或 Sentinel-1 佐证（s1）"
    )
    gap_days: int | None = Field(None, description="距上一有效光学观测的天数")
    s1_date: date | None = Field(None, description="用于比对的 Sentinel-1 影像日期")
    s1_delta_vh_db: float | None = Field(
        None, description="S1 地块中位 VH 相对本季峰值期的变化（dB）"
    )
    s1_delta_ratio_db: float | None = Field(
        None, description="S1 地块中位 VH−VV 相对本季峰值期的变化（dB）"
    )
    s1_agreement: Literal["agree", "disagree", "ambiguous"] | None = None
    threshold_source: str | None = Field(
        None, description="阈值来源：profile:<键> | adaptive | default"
    )
    suspected_harvest_pct: float | None = Field(
        None,
        description=(
            "疑似收获占比 0–100：峰值后呈秸秆残茬样（绿度≤峰值约一半、NDMI≤0、亮度抬升、"
            "下一期不回绿），但与枯熟未收的站秆难以区分；后续确认后转入 harvested_pct。"
            "不含在 harvested_pct 内；旧版本结果为空"
        ),
    )
    harvested_or_suspected_pct: float | None = Field(
        None,
        description="已收获 + 疑似收获，0–100，季内单调不减；旧版本结果为空",
    )
    residue_harvested_pct: float | None = Field(
        None,
        description=(
            "harvested_pct 中先经留茬判据检出、后被确认（裸土级/突变/S1）晋升的部分"
        ),
    )
    interpolated: bool = Field(False, description="true 表示按日插值的展示点，非真实观测")


class HarvestProgressOut(BaseModel):
    land_id: str
    date_from: date
    date_to: date
    include_zero: bool
    parcel_area_mu: float | None = None
    method_version: str
    heuristic: bool = True
    rule_zh: str
    source: Literal["stored", "live"] = "stored"
    thresholds: dict[str, Any] = Field(default_factory=dict)
    threshold_source: str | None = None
    interpolate: Literal["none", "daily"] = "none"
    items: list[HarvestProgressItem] = Field(default_factory=list)
