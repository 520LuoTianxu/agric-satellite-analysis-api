"""Pydantic schemas for agri schema (项目区 / 地块 / S1·S2 产品)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


Sensor = Literal["S1", "S2"]


class ProjectAreaOut(BaseModel):
    """virtual_project_areas row (项目区 / ~5km tile)."""

    tile_id: str
    project_key: str | None = None
    anchor_land_id: str | None = None
    assignment_type: str | None = None
    parcel_count: int | None = None
    tile_width_m: float | None = None
    tile_height_m: float | None = None
    group_id: str | None = None
    group_name: str | None = None
    base_id: str | None = None
    org_code: str | None = None
    org_name: str | None = None
    province_name: str | None = None
    city_name: str | None = None
    county_name: str | None = None
    boundary_geojson: dict[str, Any] | None = None
    boundary_srid: int | None = None
    min_lon: float | None = None
    min_lat: float | None = None
    max_lon: float | None = None
    max_lat: float | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    land_count: int | None = None


class ProjectAreaLandOut(BaseModel):
    """virtual_project_area_lands join row (+ optional parcel fields)."""

    tile_id: str
    land_id: str
    assignment_type: str | None = None
    is_anchor: bool | None = None
    intersection_area_m2: float | None = None
    coverage_ratio: float | None = None
    land_name: str | None = None
    land_area_mu: float | None = None
    province_name: str | None = None
    city_name: str | None = None
    county_name: str | None = None
    min_lon: float | None = None
    min_lat: float | None = None
    max_lon: float | None = None
    max_lat: float | None = None


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
    heatmap_url: str | None = Field(
        default=None,
        description="Optional pre-rendered heatmap PNG URL from OSS JSON (include_pixels=1).",
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
    schema_name: str = "agri"
    tables: list[AgriTableCount]
    note: str | None = None
