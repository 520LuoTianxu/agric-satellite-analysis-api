"""Pydantic schemas - soil data."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class SoilLayerOut(BaseModel):
    depth_top_cm: int
    depth_bottom_cm: int

    # Baseline properties
    sand_pct: float | None = None
    silt_pct: float | None = None
    clay_pct: float | None = None
    ph: float | None = None
    soc_g_kg: float | None = None
    bd_kg_dm3: float | None = None
    cec_cmol_kg: float | None = None
    nitrogen_g_kg: float | None = None
    cfvo_pct: float | None = None

    # Water retention
    fc_vol_pct: float | None = None
    wp_vol_pct: float | None = None
    awc_mm: float | None = None
    ksat_cm_day: float | None = None

    # Texture
    texture_class: str | None = None

    # Uncertainty (90% CI)
    sand_q05: float | None = None
    sand_q95: float | None = None
    clay_q05: float | None = None
    clay_q95: float | None = None
    ph_q05: float | None = None
    ph_q95: float | None = None
    soc_q05: float | None = None
    soc_q95: float | None = None
    ksat_q05: float | None = None
    ksat_q95: float | None = None

    model_config = {"from_attributes": True}


class SoilProfileOut(BaseModel):
    id: uuid.UUID
    land_id: str
    source: str
    source_resolution_m: int | None = None
    fetched_at: datetime
    layers: list[SoilLayerOut] = []

    model_config = {"from_attributes": True}


class SoilFieldSummaryOut(BaseModel):
    id: uuid.UUID
    land_id: str
    dominant_texture: str | None = None
    avg_ph: float | None = None
    total_soc_stock_t_ha: float | None = None
    rootzone_awc_mm: float | None = None
    drainage_class: str | None = None

    # Risk scores (0–1)
    acidification_risk: float | None = None
    compaction_risk: float | None = None
    leaching_risk: float | None = None
    rooting_constraint: float | None = None
    waterlogging_risk: float | None = None

    # Carbon
    topsoil_soc_stock_t_ha: float | None = None

    data_quality_score: float | None = None
    computed_at: datetime

    model_config = {"from_attributes": True}


class SoilRefreshResponse(BaseModel):
    land_id: str
    job_id: str
    status: str
    message: str


# ── Intelligence Schemas ─────────────────────────────────────────────


class SamplingZoneFeature(BaseModel):
    type: str = "Feature"
    geometry: dict[str, Any]
    properties: dict[str, Any]


class SamplingZonesResponse(BaseModel):
    type: str = "FeatureCollection"
    features: list[SamplingZoneFeature] = []


class CropSuitabilityItem(BaseModel):
    crop: str
    name: str  # human-readable display name
    score: float  # 0-100
    rating: str  # excellent / good / moderate / poor
    limiting_factors: list[str] = []


class CropSuitabilityResponse(BaseModel):
    crops: list[CropSuitabilityItem] = []
    field_crop_type: str | None = None
    field_crop_suitability: CropSuitabilityItem | None = None
    weather_available: bool = True
    message: str | None = None


class NutrientContextResponse(BaseModel):
    zone_class: str
    confidence: float
    factors: list[str] = []
    interpretation: str
    disclaimer: str = (
        "This is soil context for planning purposes, not a fertilizer recommendation. "
        "Consult a local agronomist for specific nutrient management advice."
    )


class CarbonEstimateResponse(BaseModel):
    current_soc_stock_t_ha: float | None = None
    topsoil_soc_stock_t_ha: float | None = None
    estimated_soc_saturation_t_ha: float | None = None
    saturation_pct: float | None = None
    sequestration_potential_low_t_ha: float | None = None
    sequestration_potential_high_t_ha: float | None = None
    climate_zone: str | None = None
    disclaimer: str = ""


class SoilWeatherStressResponse(BaseModel):
    status: str  # drought_stress | optimal | wet_stress | approaching_drought | unknown
    severity: float
    moisture_status: str
    awc_rootzone_mm: float | None = None
    water_balance_30d_mm: float | None = None
    factors: list[str] = []


# ── Vendor NPK (cdfinance analyzeSoilV2) ─────────────────────────────


class SoilNpkIndicatorOut(BaseModel):
    code: str
    name_cn: str | None = None
    value: float | None = None
    unit: str | None = None
    grade: str | None = None
    grade_level: int | None = None
    sqi_score: float | None = None


class SoilNpkOut(BaseModel):
    land_id: str
    source: str
    # Normalized
    tn_g_kg: float | None = None  # 全氮
    an_mg_kg: float | None = None  # 碱解氮
    ap_mg_kg: float | None = None  # 有效磷
    ak_mg_kg: float | None = None  # 速效钾
    tp_g_kg: float | None = None
    tk_g_kg: float | None = None
    som_g_kg: float | None = None
    ph: float | None = None
    sqi_score: float | None = None
    sqi_rating: str | None = None
    texture_usda_cn: str | None = None
    vendor_log_id: int | None = None
    indicators: list[SoilNpkIndicatorOut] = []
    # UI aliases 氮/磷/钾
    n: dict[str, Any] | None = None
    p: dict[str, Any] | None = None
    k: dict[str, Any] | None = None
    fetched_at: datetime
    # Full vendor JSON when include_payload=1
    vendor_payload: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class SoilNpkFetchRequest(BaseModel):
    """Fetch NPK from cdfinance. Token via body or Authorization header."""

    token: str | None = None
    # Optional prebuilt gateway query: timestamp&nonce&z_seller&sv&sign=...
    auth_query: str | None = None
    hr_base_id: str | None = None
    force: bool = False


class SoilNpkFetchResponse(BaseModel):
    land_id: str
    status: str
    npk: SoilNpkOut
    message: str | None = None


# ── Vendor site admission (cdfinance groupSiteAdmission) ─────────────


class SiteAdmissionOut(BaseModel):
    id: uuid.UUID | None = None
    land_id: str
    group_id: str
    source: str
    status: str | None = None
    score: float | None = None
    score_bank: str | None = None
    survey_id: int | None = None
    answer_id: int | None = None
    total_area_mu: float | None = None
    avg_yield: float | None = None
    mu_profit: float | None = None
    key_labels: dict[str, Any] | None = None
    item_answers: dict[str, Any] | None = None
    red_line_answers: dict[str, Any] | None = None
    planned_crops: list[str] | None = None
    dimensions: list[dict[str, Any]] | None = None
    summary: dict[str, Any] | None = None
    fetched_at: datetime
    vendor_payload: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class SiteAdmissionFetchRequest(BaseModel):
    """Fetch site admission by groupId. Token via body or Authorization."""

    token: str | None = None
    group_id: str | int | None = None
    auth_query: str | None = None
    hr_base_id: str | None = None
    force: bool = False


class SiteAdmissionFetchResponse(BaseModel):
    land_id: str
    group_id: str
    status: str
    admission: SiteAdmissionOut
    message: str | None = None
