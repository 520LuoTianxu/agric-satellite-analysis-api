"""SQLAlchemy ORM models (auth/org tables removed in migration 0018)."""

from __future__ import annotations

import uuid
from datetime import date as _date, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    # 统一应用表与扩展对象的命名空间，代码不再依赖 PostgreSQL 默认 public schema。
    metadata = MetaData(schema="agric_satellite")


# ── Farms / Land parcels ─────────────────────────────────────────────


class Farm(Base):
    __tablename__ = "farms"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    country: Mapped[str | None] = mapped_column(String(3), nullable=True)
    region: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    land_parcels: Mapped[list["LandParcel"]] = relationship(
        back_populates="farm", lazy="selectin"
    )


class LandParcel(Base):
    """Canonical parcel row used by every ingest task; key is ``land_id``."""

    __tablename__ = "land_parcels"

    land_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_parcel_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    virtual_tile_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    project_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tile_assignment_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tile_anchor_land_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    farm_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("farms.id", ondelete="SET NULL"), nullable=True
    )
    land_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    org_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    org_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    base_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    province_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    province_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    city_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    city_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    county_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    county_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    town_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    town_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    village_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    village_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    soil_property: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_batch: Mapped[str | None] = mapped_column(String(64), nullable=True)
    land_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_update_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    boundary_geojson = mapped_column(JSONB, nullable=False)
    boundary_srid: Mapped[int] = mapped_column(Integer, nullable=False, default=4326)
    min_lon: Mapped[float] = mapped_column(Float, nullable=False)
    min_lat: Mapped[float] = mapped_column(Float, nullable=False)
    max_lon: Mapped[float] = mapped_column(Float, nullable=False)
    max_lat: Mapped[float] = mapped_column(Float, nullable=False)
    geom = mapped_column(Geometry("MULTIPOLYGON", srid=4326), nullable=True)
    area_ha: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    crop_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    season: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_json = mapped_column(JSONB, nullable=True)
    source_properties = mapped_column(JSONB, nullable=False, default=dict)
    source_file: Mapped[str] = mapped_column(Text, nullable=False)
    source_feature_index: Mapped[int] = mapped_column(Integer, nullable=False)
    original_area_mu: Mapped[float | None] = mapped_column(Numeric(18, 4))
    land_area_mu: Mapped[float | None] = mapped_column(Numeric(18, 4))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    farm: Mapped[Farm | None] = relationship(back_populates="land_parcels")


# ── Observations / Layers ────────────────────────────────────────────


class RasterLayer(Base):
    __tablename__ = "raster_layers"
    __table_args__ = (
        UniqueConstraint(
            "land_id", "date", "layer_type", name="uq_raster_land_date_type"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("land_parcels.land_id"), nullable=False
    )
    layer_type: Mapped[str] = mapped_column(Text, nullable=False, default="NDVI")
    satellite: Mapped[str] = mapped_column(Text, nullable=False, default="S2")
    date: Mapped[_date] = mapped_column(Date, nullable=False)
    cog_uri: Mapped[str] = mapped_column(Text, nullable=False)
    min: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    max: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    params_json = mapped_column(JSONB, nullable=True)
    provenance_json = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FieldStat(Base):
    __tablename__ = "field_stats"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("land_parcels.land_id"), nullable=False, index=True
    )
    layer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raster_layers.id"), nullable=False, index=True
    )
    date: Mapped[_date] = mapped_column(Date, nullable=False)
    mean: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    median: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    min: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    max: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    p10: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    p90: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    stddev: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── Alerts / Scouting ────────────────────────────────────────────────


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("land_parcels.land_id"), nullable=False, index=True
    )
    date: Mapped[_date] = mapped_column(Date, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_params_json = mapped_column(JSONB, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    index_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, default="ndvi"
    )
    weather_context = mapped_column(JSONB, nullable=True)
    soil_context = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ScoutingObservation(Base):
    __tablename__ = "scouting_observations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("land_parcels.land_id"), nullable=False, index=True
    )
    alert_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alerts.id"), nullable=True
    )
    geom_point = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_json = mapped_column(JSONB, nullable=True)
    photo_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    weather_snapshot = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── Jobs / Audit / Share ─────────────────────────────────────────────


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("land_parcels.land_id"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    progress_json = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    params_json = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    metadata_json = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("land_parcels.land_id"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    scope: Mapped[str] = mapped_column(String(50), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── Weather Data ─────────────────────────────────────────────────────


class WeatherDaily(Base):
    __tablename__ = "weather_daily"
    __table_args__ = (
        UniqueConstraint("land_id", "date", name="uq_weather_land_date"),
        Index("idx_weather_land_date", "land_id", "date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("land_parcels.land_id", ondelete="CASCADE"),
        nullable=False,
    )
    date: Mapped[_date] = mapped_column(Date, nullable=False)
    latitude: Mapped[float] = mapped_column(Numeric(10, 8), nullable=False)
    longitude: Mapped[float] = mapped_column(Numeric(11, 8), nullable=False)

    # Raw Open-Meteo variables
    temperature_2m_min: Mapped[float | None] = mapped_column(Numeric(5, 2))
    temperature_2m_max: Mapped[float | None] = mapped_column(Numeric(5, 2))
    temperature_2m_mean: Mapped[float | None] = mapped_column(Numeric(5, 2))
    precipitation_sum: Mapped[float | None] = mapped_column(Numeric(6, 2))
    et0_fao_mm: Mapped[float | None] = mapped_column(Numeric(6, 2))
    soil_temperature_0cm: Mapped[float | None] = mapped_column(Numeric(5, 2))
    soil_temperature_6cm: Mapped[float | None] = mapped_column(Numeric(5, 2))
    soil_temperature_18cm: Mapped[float | None] = mapped_column(Numeric(5, 2))
    soil_temperature_54cm: Mapped[float | None] = mapped_column(Numeric(5, 2))
    soil_moisture_0_1cm: Mapped[float | None] = mapped_column(Numeric(5, 3))
    soil_moisture_1_3cm: Mapped[float | None] = mapped_column(Numeric(5, 3))
    soil_moisture_3_9cm: Mapped[float | None] = mapped_column(Numeric(5, 3))
    soil_moisture_9_27cm: Mapped[float | None] = mapped_column(Numeric(5, 3))
    soil_moisture_27_81cm: Mapped[float | None] = mapped_column(Numeric(5, 3))
    vapor_pressure_deficit: Mapped[float | None] = mapped_column(Numeric(5, 2))
    shortwave_radiation_sum: Mapped[float | None] = mapped_column(Numeric(7, 2))
    wind_speed_10m_max: Mapped[float | None] = mapped_column(Numeric(5, 2))
    cloud_cover_mean: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Derived agricultural indices
    gdd_daily: Mapped[float | None] = mapped_column(Numeric(5, 1))
    gdd_cumulative: Mapped[float | None] = mapped_column(Numeric(8, 1))
    water_balance_30d_mm: Mapped[float | None] = mapped_column(Numeric(7, 2))
    drought_index: Mapped[float | None] = mapped_column(Numeric(5, 2))
    heat_stress_flag: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Metadata
    source: Mapped[str | None] = mapped_column(Text, server_default="open-meteo")
    model_used: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ── Soil Data ────────────────────────────────────────────────────────


class SoilProfile(Base):
    __tablename__ = "soil_profiles"
    __table_args__ = (
        Index("idx_soil_profiles_land_id", "land_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("land_parcels.land_id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # soilgrids | polaris
    source_resolution_m: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    layers = relationship(
        "SoilLayer", back_populates="profile", cascade="all, delete-orphan"
    )


class SoilLayer(Base):
    __tablename__ = "soil_layers"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "depth_top_cm", name="uq_soil_layer_profile_depth"
        ),
        Index("idx_soil_layers_profile_id", "profile_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("soil_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Depth range (GlobalSoilMap standard: 0-5, 5-15, 15-30, 30-60, 60-100, 100-200 cm)
    depth_top_cm: Mapped[int] = mapped_column(Integer, nullable=False)
    depth_bottom_cm: Mapped[int] = mapped_column(Integer, nullable=False)

    # Baseline properties
    sand_pct: Mapped[float | None] = mapped_column(Float)
    silt_pct: Mapped[float | None] = mapped_column(Float)
    clay_pct: Mapped[float | None] = mapped_column(Float)
    ph: Mapped[float | None] = mapped_column(Float)
    soc_g_kg: Mapped[float | None] = mapped_column(Float)  # soil organic carbon
    bd_kg_dm3: Mapped[float | None] = mapped_column(Float)  # bulk density
    cec_cmol_kg: Mapped[float | None] = mapped_column(Float)  # cation exchange capacity
    nitrogen_g_kg: Mapped[float | None] = mapped_column(Float)
    cfvo_pct: Mapped[float | None] = mapped_column(Float)  # coarse fragments vol %

    # Water retention
    fc_vol_pct: Mapped[float | None] = mapped_column(
        Float
    )  # field capacity (θ at 33kPa)
    wp_vol_pct: Mapped[float | None] = mapped_column(
        Float
    )  # wilting point (θ at 1500kPa)
    awc_mm: Mapped[float | None] = mapped_column(
        Float
    )  # available water capacity for layer
    ksat_cm_day: Mapped[float | None] = mapped_column(
        Float
    )  # saturated hydraulic conductivity

    # Texture classification
    texture_class: Mapped[str | None] = mapped_column(String(20))

    # Uncertainty (90% confidence intervals)
    sand_q05: Mapped[float | None] = mapped_column(Float)
    sand_q95: Mapped[float | None] = mapped_column(Float)
    clay_q05: Mapped[float | None] = mapped_column(Float)
    clay_q95: Mapped[float | None] = mapped_column(Float)
    ph_q05: Mapped[float | None] = mapped_column(Float)
    ph_q95: Mapped[float | None] = mapped_column(Float)
    soc_q05: Mapped[float | None] = mapped_column(Float)
    soc_q95: Mapped[float | None] = mapped_column(Float)
    ksat_q05: Mapped[float | None] = mapped_column(Float)
    ksat_q95: Mapped[float | None] = mapped_column(Float)

    profile = relationship("SoilProfile", back_populates="layers")


class SoilFieldSummary(Base):
    __tablename__ = "soil_field_summary"
    __table_args__ = (UniqueConstraint("land_id", name="uq_soil_land_summary_land"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("land_parcels.land_id", ondelete="CASCADE"),
        nullable=False,
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("soil_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Aggregated properties
    dominant_texture: Mapped[str | None] = mapped_column(String(20))
    avg_ph: Mapped[float | None] = mapped_column(Float)
    total_soc_stock_t_ha: Mapped[float | None] = mapped_column(Float)
    rootzone_awc_mm: Mapped[float | None] = mapped_column(Float)  # 0-100cm integrated
    drainage_class: Mapped[str | None] = mapped_column(String(30))

    # Risk scores (0–1 scale)
    acidification_risk: Mapped[float | None] = mapped_column(Float)
    compaction_risk: Mapped[float | None] = mapped_column(Float)
    leaching_risk: Mapped[float | None] = mapped_column(Float)
    rooting_constraint: Mapped[float | None] = mapped_column(Float)
    waterlogging_risk: Mapped[float | None] = mapped_column(Float)

    # Carbon
    topsoil_soc_stock_t_ha: Mapped[float | None] = mapped_column(Float)  # 0-30cm

    # Data quality
    data_quality_score: Mapped[float | None] = mapped_column(
        Float
    )  # 0–1, higher=better

    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SoilNutrientNpk(Base):
    """Vendor NPK / fertility snapshot (cdfinance analyzeSoilV2).

    Separate from SoilGrids ``soil_profiles`` / ``soil_field_summary`` so
    SoilGrids data is never overwritten.
    """

    __tablename__ = "soil_nutrient_npk"
    __table_args__ = (
        UniqueConstraint("land_id", name="uq_soil_nutrient_npk_land"),
        Index("idx_soil_nutrient_npk_land_id", "land_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    land_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("land_parcels.land_id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="cdfinance_analyzeSoilV2"
    )

    tn_g_kg: Mapped[float | None] = mapped_column(Float)  # 全氮
    an_mg_kg: Mapped[float | None] = mapped_column(Float)  # 碱解氮
    ap_mg_kg: Mapped[float | None] = mapped_column(Float)  # 有效磷
    ak_mg_kg: Mapped[float | None] = mapped_column(Float)  # 速效钾
    tp_g_kg: Mapped[float | None] = mapped_column(Float)  # 全磷
    tk_g_kg: Mapped[float | None] = mapped_column(Float)  # 全钾
    som_g_kg: Mapped[float | None] = mapped_column(Float)  # 有机质
    ph: Mapped[float | None] = mapped_column(Float)
    sqi_score: Mapped[float | None] = mapped_column(Float)
    sqi_rating: Mapped[str | None] = mapped_column(Text)
    texture_usda_cn: Mapped[str | None] = mapped_column(String(40))
    vendor_log_id: Mapped[int | None] = mapped_column(BigInteger)
    vendor_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GroupSiteAdmission(Base):
    """Vendor site-admission snapshot keyed directly by the canonical land."""

    __tablename__ = "group_site_admission"
    __table_args__ = (
        UniqueConstraint("group_id", name="uq_group_site_admission_group"),
        Index("idx_group_site_admission_land_id", "land_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()")
    )
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    land_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("land_parcels.land_id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="cdfinance_groupSiteAdmission"
    )
    status: Mapped[str | None] = mapped_column(String(32))
    score: Mapped[float | None] = mapped_column(Float)
    score_bank: Mapped[str | None] = mapped_column(String(80))
    survey_id: Mapped[int | None] = mapped_column(BigInteger)
    answer_id: Mapped[int | None] = mapped_column(BigInteger)
    total_area_mu: Mapped[float | None] = mapped_column(Float)
    avg_yield: Mapped[float | None] = mapped_column(Float)
    mu_profit: Mapped[float | None] = mapped_column(Float)
    summary_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    vendor_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
