"""Pydantic schemas - farms and fields."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator, Field as PydanticField


# ── Farm ─────────────────────────────────────────────────────────────


class FarmCreate(BaseModel):
    name: str
    country: str | None = None
    region: str | None = None
    timezone: str | None = None


class FarmUpdate(BaseModel):
    name: str | None = None
    country: str | None = None
    region: str | None = None
    timezone: str | None = None


class FarmOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID | None = None
    name: str
    country: str | None = None
    region: str | None = None
    timezone: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Canonical land parcel ───────────────────────────────────────────


class LandParcelCreate(BaseModel):
    """Create one canonical parcel row; ``land_id`` is supplied by the caller."""

    land_id: str
    tile_id: str | None = None
    farm_id: uuid.UUID | None = None
    land_name: str
    group_id: str | None = None
    group_name: str | None = None
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
    boundary_geojson: dict[str, Any]
    crop_type: str | None = None
    season: str | None = None
    tags_json: list[str] | None = None


class LandParcelUpdate(BaseModel):
    tile_id: str | None = None
    land_name: str | None = None
    group_id: str | None = None
    group_name: str | None = None
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
    boundary_geojson: dict[str, Any] | None = None
    crop_type: str | None = None
    season: str | None = None
    tags_json: list[str] | None = None
    farm_id: uuid.UUID | None = None


class LandParcelOut(BaseModel):
    land_id: str
    source_parcel_id: str | None = None
    tile_id: str
    virtual_tile_id: str | None = None
    project_key: str | None = None
    tile_assignment_type: str | None = None
    tile_anchor_land_id: str | None = None
    farm_id: uuid.UUID | None = None
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
    soil_property: str | None = None
    current_batch: str | None = None
    land_status: str | None = None
    source_update_time: datetime | None = None
    boundary_geojson: dict[str, Any]
    boundary_srid: int = 4326
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float
    # 兼容旧客户端的字段名；实际值由 boundary_geojson 提供，不再读取空间列。
    geom: dict[str, Any] | None = None
    area_ha: float | None = None
    crop_type: str | None = None
    season: str | None = None
    tags_json: list[str] | None = None
    source_properties: dict[str, Any] | None = None
    source_file: str | None = None
    source_feature_index: int | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}

    @field_validator("tags_json", mode="before")
    @classmethod
    def normalize_tags(cls, v: Any) -> list[str] | None:
        """Normalize the JSON tag array stored on the canonical parcel."""
        if v is None:
            return None
        return v


class LandParcelImportResponse(BaseModel):
    imported: int
    errors: list[str] = []


# ── Backfill ─────────────────────────────────────────────────────────


class GrowingSeasonWindow(BaseModel):
    """One growing-season window for RS pull / decloud / harvest.

    Rotation: send several windows. Intercrop: ``crops`` length 2 in one window.
    Prefer ``start_date``/``end_date``. Also accepts ``months`` or
    ``start_month``/``end_month`` (wrap OK). Legacy ``crop`` → ``crops``.
    """

    label: str | None = None
    crops: list[str] = PydanticField(default_factory=list)
    crop: str | None = None  # legacy singular
    months: list[int] = PydanticField(default_factory=list)
    start_month: int | None = None
    end_month: int | None = None
    start_date: str | None = None
    end_date: str | None = None

    def resolved_crops(self) -> list[str]:
        out: list[str] = []
        for c in self.crops or []:
            s = str(c).strip()
            if s and s not in out:
                out.append(s)
        if self.crop:
            s = str(self.crop).strip()
            if s and s not in out:
                out.append(s)
        return out


class BackfillIndicesRequest(BaseModel):
    months: int = 24
    force: bool = False
    # Inclusive start date (YYYY-MM-DD). When set, range is date_from → today
    # (or date_to); months is ignored for range calculation.
    date_from: str | None = None
    date_to: str | None = None
    # User-selected growing seasons (rotation = multiple windows).
    # Each window: dates + crops[1..2]. Union of months drives high-cloud pull/decloud.
    growing_seasons: list[GrowingSeasonWindow] | None = None
    # Flat month list alternative / override merged with growing_seasons.
    season_months: list[int] | None = None


class BackfillIndicesResponse(BaseModel):
    land_id: str
    status: str
    message: str


class BackfillStatusResponse(BaseModel):
    land_id: str
    has_active_backfill: bool
    pending_jobs: int
    running_jobs: int
    completed_jobs: int
    failed_jobs: int = 0
    total_jobs: int = 0
    percent: float = 0.0
    phase: str = "idle"  # idle | stac | bridge | done
    message: str = ""
