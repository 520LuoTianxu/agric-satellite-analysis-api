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


# ── Field ────────────────────────────────────────────────────────────


class FieldCreate(BaseModel):
    farm_id: uuid.UUID
    name: str
    geom: dict[str, Any]  # GeoJSON geometry
    crop_type: str  # catalog key from GET /v1/crops (required)
    season: str | None = None
    tags: list[str] | None = None


class FieldUpdate(BaseModel):
    name: str | None = None
    geom: dict[str, Any] | None = None  # GeoJSON geometry
    crop_type: str | None = None
    season: str | None = None
    tags: list[str] | None = None


class FieldOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID | None = None
    farm_id: uuid.UUID
    name: str
    geom: dict[str, Any] | None = None  # GeoJSON geometry
    area_ha: float | None = None
    crop_type: str | None = None
    season: str | None = None
    tags: list[str] | None = PydanticField(default=None, validation_alias="tags_json")
    created_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}

    @field_validator("geom", mode="before")
    @classmethod
    def convert_wkb_to_geojson(cls, v: Any) -> dict[str, Any] | None:
        """Auto-convert GeoAlchemy2 WKBElement to GeoJSON dict."""
        if v is None:
            return None
        if isinstance(v, dict):
            return v
        # WKBElement from GeoAlchemy2
        try:
            from geoalchemy2.shape import to_shape
            from shapely.geometry import mapping

            return mapping(to_shape(v))
        except Exception:
            return None

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, v: Any) -> list[str] | None:
        """Handle tags_json attribute name from ORM."""
        if v is None:
            return None
        return v


class FieldImportResponse(BaseModel):
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
    field_id: uuid.UUID
    status: str
    message: str


class BackfillStatusResponse(BaseModel):
    field_id: uuid.UUID
    has_active_backfill: bool
    pending_jobs: int
    running_jobs: int
    completed_jobs: int
    failed_jobs: int = 0
    total_jobs: int = 0
    percent: float = 0.0
    phase: str = "idle"  # idle | stac | bridge | done
    message: str = ""
