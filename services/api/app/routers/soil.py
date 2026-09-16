"""Soil profile and vendor soil endpoints for canonical land parcels."""

from __future__ import annotations

import uuid
from datetime import timedelta, timezone, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.geo import geojson_centroid
from app.core.logging import logger
from app.core.rate_limit import limiter
from app.core.soil_intelligence import (
    assess_crop_suitability,
    classify_nutrient_risk,
    compute_sampling_zones,
    compute_soil_weather_stress,
    estimate_sequestration_potential,
)
from app.middleware.auth import OrgContext, get_org_context, require_roles
from app.models.tables import (
    LandParcel,
    Job,
    SoilFieldSummary,
    GroupSiteAdmission,
    SoilNutrientNpk,
    SoilProfile,
    WeatherDaily,
)
from app.schemas.soil import (
    CarbonEstimateResponse,
    CropSuitabilityItem,
    CropSuitabilityResponse,
    NutrientContextResponse,
    SamplingZonesResponse,
    SoilFieldSummaryOut,
    SoilNpkFetchRequest,
    SoilNpkFetchResponse,
    SoilNpkIndicatorOut,
    SiteAdmissionFetchRequest,
    SiteAdmissionFetchResponse,
    SiteAdmissionOut,
    SoilNpkOut,
    SoilProfileOut,
    SoilRefreshResponse,
    SoilWeatherStressResponse,
)

router = APIRouter()

_writer = require_roles("owner", "admin")


async def _get_land_or_404(
    land_id: str, org_id: uuid.UUID, db: AsyncSession
) -> LandParcel:
    field = await db.get(LandParcel, land_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Land parcel not found")
    return field


@router.get("/lands/{land_id}/soil", response_model=SoilProfileOut)
async def get_soil_profile(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the soil profile with all layers for a field."""
    await _get_land_or_404(land_id, ctx.org_id, db)

    result = await db.execute(
        select(SoilProfile)
        .options(selectinload(SoilProfile.layers))
        .where(SoilProfile.land_id == land_id)
        .order_by(SoilProfile.fetched_at.desc())
        .limit(1)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Soil profile not yet available")

    return profile


@router.get("/lands/{land_id}/soil/summary", response_model=SoilFieldSummaryOut)
async def get_soil_summary(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the aggregated soil summary for a field."""
    await _get_land_or_404(land_id, ctx.org_id, db)

    result = await db.execute(
        select(SoilFieldSummary).where(SoilFieldSummary.land_id == land_id)
    )
    summary = result.scalar_one_or_none()
    if not summary:
        raise HTTPException(status_code=404, detail="Soil summary not yet available")

    return summary


@router.post(
    "/lands/{land_id}/soil/refresh",
    response_model=SoilRefreshResponse,
    status_code=202,
)
@limiter.limit("5/minute")
async def refresh_soil(
    request: Request,
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Trigger a re-fetch of soil data for a field."""
    await _get_land_or_404(land_id, ctx.org_id, db)

    # Create a Job row for progress tracking
    job = Job(land_id=land_id, type="soil_fetch", status="pending")
    db.add(job)
    await db.flush()

    await db.commit()

    from app.mq_publish import publish_api_task

    task_id = publish_api_task(
        type="soil_fetch", land_id=str(land_id), extras={"job_id": str(job.id)}
    )

    logger.info(
        "soil_refresh_triggered",
        land_id=str(land_id),
        job_id=str(job.id),
        mq_task_id=task_id,
    )

    return SoilRefreshResponse(
        land_id=str(land_id),
        job_id=str(job.id),
        status="accepted",
        message="Soil data refresh queued.",
    )


# ── Intelligence Endpoints ───────────────────────────────────────────


async def _get_profile_and_summary(
    land_id: str, db: AsyncSession
) -> tuple[SoilProfile, SoilFieldSummary]:
    """Load profile with layers + summary, or raise 404."""
    result = await db.execute(
        select(SoilProfile)
        .options(selectinload(SoilProfile.layers))
        .where(SoilProfile.land_id == land_id)
        .order_by(SoilProfile.fetched_at.desc())
        .limit(1)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Soil data not yet available")

    s_result = await db.execute(
        select(SoilFieldSummary).where(SoilFieldSummary.land_id == land_id)
    )
    summary = s_result.scalar_one_or_none()
    if not summary:
        raise HTTPException(status_code=404, detail="Soil summary not yet available")

    return profile, summary


def _layers_to_dicts(layers) -> list[dict]:
    """Convert SoilLayer ORM objects to plain dicts for intelligence functions."""
    return [
        {
            "depth_top_cm": lyr.depth_top_cm,
            "depth_bottom_cm": lyr.depth_bottom_cm,
            "sand_pct": lyr.sand_pct,
            "silt_pct": lyr.silt_pct,
            "clay_pct": lyr.clay_pct,
            "ph": lyr.ph,
            "soc_g_kg": lyr.soc_g_kg,
            "bd_kg_dm3": lyr.bd_kg_dm3,
            "cec_cmol_kg": lyr.cec_cmol_kg,
            "nitrogen_g_kg": lyr.nitrogen_g_kg,
            "cfvo_pct": lyr.cfvo_pct,
            "fc_vol_pct": lyr.fc_vol_pct,
            "wp_vol_pct": lyr.wp_vol_pct,
            "awc_mm": lyr.awc_mm,
            "ksat_cm_day": lyr.ksat_cm_day,
            "texture_class": lyr.texture_class,
            "sand_q05": lyr.sand_q05,
            "sand_q95": lyr.sand_q95,
            "clay_q05": lyr.clay_q05,
            "clay_q95": lyr.clay_q95,
            "ph_q05": lyr.ph_q05,
            "ph_q95": lyr.ph_q95,
            "soc_q05": lyr.soc_q05,
            "soc_q95": lyr.soc_q95,
        }
        for lyr in layers
    ]


def _summary_to_dict(s: SoilFieldSummary) -> dict:
    """Convert SoilFieldSummary ORM object to plain dict."""
    return {
        "dominant_texture": s.dominant_texture,
        "avg_ph": float(s.avg_ph) if s.avg_ph is not None else None,
        "total_soc_stock_t_ha": float(s.total_soc_stock_t_ha)
        if s.total_soc_stock_t_ha is not None
        else None,
        "topsoil_soc_stock_t_ha": float(s.topsoil_soc_stock_t_ha)
        if s.topsoil_soc_stock_t_ha is not None
        else None,
        "rootzone_awc_mm": float(s.rootzone_awc_mm)
        if s.rootzone_awc_mm is not None
        else None,
        "drainage_class": s.drainage_class,
        "acidification_risk": float(s.acidification_risk)
        if s.acidification_risk is not None
        else None,
        "compaction_risk": float(s.compaction_risk)
        if s.compaction_risk is not None
        else None,
        "leaching_risk": float(s.leaching_risk)
        if s.leaching_risk is not None
        else None,
        "rooting_constraint": float(s.rooting_constraint)
        if s.rooting_constraint is not None
        else None,
        "waterlogging_risk": float(s.waterlogging_risk)
        if s.waterlogging_risk is not None
        else None,
    }


@router.get(
    "/lands/{land_id}/soil/sampling-zones", response_model=SamplingZonesResponse
)
async def get_sampling_zones(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return suggested sampling zone GeoJSON based on soil variability."""
    land = await _get_land_or_404(land_id, ctx.org_id, db)
    profile, _summary = await _get_profile_and_summary(land_id, db)

    layer_dicts = _layers_to_dicts(profile.layers)

    # 优先使用土壤档案缓存的中心点；缺失时从 JSONB 边界在应用内计算。
    meta = profile.metadata_json or {}
    centroid_lat = meta.get("centroid_lat")
    centroid_lon = meta.get("centroid_lon")
    if centroid_lat is None or centroid_lon is None:
        centroid = geojson_centroid(land.boundary_geojson)
        if centroid is None:
            raise HTTPException(status_code=400, detail="Land parcel boundary is invalid")
        centroid_lat, centroid_lon = centroid

    area_ha = float(land.area_ha) if land.area_ha else None

    zones = compute_sampling_zones(layer_dicts, centroid_lat, centroid_lon, area_ha)
    return SamplingZonesResponse(features=zones)


@router.get(
    "/lands/{land_id}/soil/crop-suitability", response_model=CropSuitabilityResponse
)
async def get_crop_suitability(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return crop suitability scores for the field's soil conditions."""
    field = await _get_land_or_404(land_id, ctx.org_id, db)
    profile, summary = await _get_profile_and_summary(land_id, db)

    summary_dict = _summary_to_dict(summary)
    layer_dicts = _layers_to_dicts(profile.layers)

    # ── Build weather summary for 4-pillar scoring ─────────────
    weather_summary: dict | None = None
    now = datetime.now(timezone.utc).date()
    one_year_ago = now - timedelta(days=365)

    # Annual aggregation (full year for proper rainfall totals)
    annual_result = await db.execute(
        select(
            func.sum(WeatherDaily.precipitation_sum),
            func.avg(WeatherDaily.temperature_2m_mean),
            func.min(WeatherDaily.temperature_2m_min),
            func.max(WeatherDaily.temperature_2m_max),
            func.count(),
        ).where(WeatherDaily.land_id == land_id, WeatherDaily.date >= one_year_ago)
    )
    arow = annual_result.one_or_none()
    day_count = int(arow[4]) if arow and arow[4] else 0
    # Need 90+ days for meaningful annual extrapolation
    has_weather = day_count >= 90

    if has_weather:
        weather_summary = {
            "annual_rainfall_mm": float(arow[0]) * (365 / day_count)
            if arow[0]
            else None,
            "avg_temp_c": float(arow[1]) if arow[1] else None,
            "min_temp_c": float(arow[2]) if arow[2] else None,
            "max_temp_c": float(arow[3]) if arow[3] else None,
            "water_balance_30d_mm": None,
            "drought_index": None,
            "drought_severity": None,
        }

        # Latest 30-day stress data
        recent_result = await db.execute(
            select(WeatherDaily)
            .where(
                WeatherDaily.land_id == land_id,
                WeatherDaily.date >= now - timedelta(days=30),
            )
            .order_by(WeatherDaily.date.desc())
            .limit(1)
        )
        latest = recent_result.scalar_one_or_none()
        if latest:
            if latest.water_balance_30d_mm is not None:
                weather_summary["water_balance_30d_mm"] = float(
                    latest.water_balance_30d_mm
                )
            if latest.drought_index is not None:
                weather_summary["drought_index"] = float(latest.drought_index)
                # Convert drought_index to severity: 0 = normal, 1 = extreme
                # Negative drought_index = water deficit (drought)
                di = float(latest.drought_index)
                if di < 0:
                    weather_summary["drought_severity"] = min(1.0, abs(di) / 3.0)

    results = assess_crop_suitability(summary_dict, layer_dicts, weather_summary)

    if not has_weather:
        return CropSuitabilityResponse(
            crops=[],
            field_crop_type=field.crop_type,
            field_crop_suitability=None,
            weather_available=False,
            message="Crop suitability requires weather data. Run the weather pipeline first.",
        )

    crops_out = [
        CropSuitabilityItem(
            crop=r.crop,
            name=r.name,
            score=r.score,
            rating=r.rating,
            limiting_factors=r.limiting_factors,
        )
        for r in results
    ]

    # Highlight current crop if set (search full list before truncating)
    field_crop = field.crop_type
    field_crop_item = None
    if field_crop:
        crop_key = field_crop.lower().replace(" ", "_").replace("/", "_")
        for item in crops_out:
            if item.crop == crop_key:
                field_crop_item = item
                break

    return CropSuitabilityResponse(
        crops=crops_out[:10],
        field_crop_type=field_crop,
        field_crop_suitability=field_crop_item,
        weather_available=True,
    )


@router.get(
    "/lands/{land_id}/soil/nutrient-context", response_model=NutrientContextResponse
)
async def get_nutrient_context(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return nutrient risk zone classification for the field."""
    await _get_land_or_404(land_id, ctx.org_id, db)
    profile, summary = await _get_profile_and_summary(land_id, db)

    summary_dict = _summary_to_dict(summary)
    layer_dicts = _layers_to_dicts(profile.layers)

    result = classify_nutrient_risk(summary_dict, layer_dicts)

    return NutrientContextResponse(
        zone_class=result.zone_class,
        confidence=result.confidence,
        factors=result.factors,
        interpretation=result.interpretation,
    )


@router.get("/lands/{land_id}/soil/carbon", response_model=CarbonEstimateResponse)
async def get_carbon_estimate(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return SOC stock, saturation estimate, and sequestration potential."""
    await _get_land_or_404(land_id, ctx.org_id, db)
    profile, summary = await _get_profile_and_summary(land_id, db)

    summary_dict = _summary_to_dict(summary)
    layer_dicts = _layers_to_dicts(profile.layers)

    # Gather annual climate data from weather_daily
    annual_precip = None
    annual_temp = None
    one_year_ago = datetime.now(timezone.utc).date() - timedelta(days=365)
    wd_result = await db.execute(
        select(
            func.sum(WeatherDaily.precipitation_sum),
            func.avg(WeatherDaily.temperature_2m_mean),
            func.count(),
        ).where(WeatherDaily.land_id == land_id, WeatherDaily.date >= one_year_ago)
    )
    row = wd_result.one_or_none()
    if row and row[2] and row[2] > 180:  # need at least 6 months of data
        annual_precip = float(row[0]) * (365 / int(row[2])) if row[0] else None
        annual_temp = float(row[1]) if row[1] else None

    result = estimate_sequestration_potential(
        summary_dict, layer_dicts, annual_precip, annual_temp
    )

    seq_low = None
    seq_high = None
    if result.sequestration_potential_t_ha:
        seq_low, seq_high = result.sequestration_potential_t_ha

    return CarbonEstimateResponse(
        current_soc_stock_t_ha=result.current_soc_stock_t_ha,
        topsoil_soc_stock_t_ha=result.topsoil_soc_stock_t_ha,
        estimated_soc_saturation_t_ha=result.estimated_soc_saturation_t_ha,
        saturation_pct=result.saturation_pct,
        sequestration_potential_low_t_ha=seq_low,
        sequestration_potential_high_t_ha=seq_high,
        climate_zone=result.climate_zone,
        disclaimer=result.disclaimer,
    )


@router.get(
    "/lands/{land_id}/soil/weather-stress", response_model=SoilWeatherStressResponse
)
async def get_soil_weather_stress(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return current root-zone moisture stress assessment."""
    await _get_land_or_404(land_id, ctx.org_id, db)
    _profile, summary = await _get_profile_and_summary(land_id, db)

    summary_dict = _summary_to_dict(summary)

    # Get latest weather data for stress calculation
    now = datetime.now(timezone.utc).date()
    wd_result = await db.execute(
        select(WeatherDaily)
        .where(
            WeatherDaily.land_id == land_id,
            WeatherDaily.date >= now - timedelta(days=30),
        )
        .order_by(WeatherDaily.date.desc())
    )
    weather_rows = wd_result.scalars().all()

    water_balance = None
    drought_idx = None
    soil_moisture = None

    if weather_rows:
        latest = weather_rows[0]
        if latest.water_balance_30d_mm is not None:
            water_balance = float(latest.water_balance_30d_mm)
        if latest.drought_index is not None:
            drought_idx = float(latest.drought_index)
        if latest.soil_moisture_0_1cm is not None:
            soil_moisture = float(latest.soil_moisture_0_1cm)

        # Fallback: compute water balance from last 30 days
        if water_balance is None:
            total_precip = sum(float(r.precipitation_sum or 0) for r in weather_rows)
            total_et0 = sum(float(r.et0_fao_mm or 0) for r in weather_rows)
            water_balance = round(total_precip - total_et0, 1)

    result = compute_soil_weather_stress(
        summary_dict, water_balance, drought_idx, soil_moisture
    )

    return SoilWeatherStressResponse(
        status=result.status,
        severity=result.severity,
        moisture_status=result.moisture_status,
        awc_rootzone_mm=result.awc_rootzone_mm,
        water_balance_30d_mm=result.water_balance_30d_mm,
        factors=result.factors,
    )


# ── Vendor NPK (cdfinance analyzeSoilV2) ─────────────────────────────


def _npk_row_to_out(
    row: SoilNutrientNpk, *, include_payload: bool = False
) -> SoilNpkOut:
    payload = row.vendor_payload if isinstance(row.vendor_payload, dict) else {}
    from app.core.cdfinance_soil import normalize_vendor_payload

    norm = normalize_vendor_payload(payload) if payload else {}
    indicators = [
        SoilNpkIndicatorOut(**i)
        for i in (norm.get("indicators") or [])
        if isinstance(i, dict)
    ]
    return SoilNpkOut(
        land_id=row.land_id,
        source=row.source,
        tn_g_kg=row.tn_g_kg,
        an_mg_kg=row.an_mg_kg,
        ap_mg_kg=row.ap_mg_kg,
        ak_mg_kg=row.ak_mg_kg,
        tp_g_kg=row.tp_g_kg,
        tk_g_kg=row.tk_g_kg,
        som_g_kg=row.som_g_kg,
        ph=row.ph,
        sqi_score=row.sqi_score,
        sqi_rating=row.sqi_rating,
        texture_usda_cn=row.texture_usda_cn,
        vendor_log_id=row.vendor_log_id,
        indicators=indicators,
        n=norm.get("n"),
        p=norm.get("p"),
        k=norm.get("k"),
        fetched_at=row.fetched_at,
        vendor_payload=payload if include_payload else None,
    )


async def _load_agri_admin_and_boundary(db: AsyncSession, land_id: str) -> dict:
    """Return admin codes + boundary_geojson for an agri land."""
    from sqlalchemy import text as sa_text

    result = await db.execute(
        sa_text(
            """
            SELECT land_id, province_code, province_name, city_code, city_name,
                   county_code, county_name, boundary_geojson
            FROM agric_satellite.land_parcels
            WHERE land_id = :land_id
            LIMIT 1
            """
        ),
        {"land_id": land_id},
    )
    row = result.mappings().first()
    return dict(row) if row else {}


def _land_coords_string(land: LandParcel) -> str:
    from app.core.cdfinance_soil import geojson_to_coords_string

    if not isinstance(land.boundary_geojson, dict):
        raise HTTPException(status_code=400, detail="Land parcel has no boundary")
    try:
        return geojson_to_coords_string(land.boundary_geojson)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/lands/{land_id}/soil/npk", response_model=SoilNpkOut)
async def get_soil_npk(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_payload: bool = False,
):
    """Return stored vendor NPK for a field (404 if never fetched)."""
    await _get_land_or_404(land_id, ctx.org_id, db)
    result = await db.execute(
        select(SoilNutrientNpk).where(SoilNutrientNpk.land_id == land_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Soil NPK not yet available")
    return _npk_row_to_out(row, include_payload=include_payload)


@router.post(
    "/lands/{land_id}/soil/npk",
    response_model=SoilNpkFetchResponse,
)
@limiter.limit("10/minute")
async def fetch_soil_npk(
    request: Request,
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: SoilNpkFetchRequest | None = None,
    authorization: Annotated[str | None, Header()] = None,
):
    """Call cdfinance analyzeSoilV2, upsert DB, return normalized NPK.

    Auth: paste H5 Bearer in ``Authorization`` header **or** body.token.
    Optional body.auth_query for gateway sign params (usually unnecessary).
    """
    from datetime import datetime, timezone

    import httpx

    from app.core.cdfinance_soil import (
        SOURCE_NAME,
        analyze_soil_v2,
        build_analysis_body,
        geojson_to_coords_string,
        normalize_vendor_payload,
    )

    body = body or SoilNpkFetchRequest()
    land = await _get_land_or_404(land_id, ctx.org_id, db)

    existing = (
        await db.execute(
            select(SoilNutrientNpk).where(SoilNutrientNpk.land_id == land_id)
        )
    ).scalar_one_or_none()
    if existing and not body.force:
        return SoilNpkFetchResponse(
            land_id=str(land_id),
            status="cached",
            npk=_npk_row_to_out(existing),
            message="已有 NPK 缓存；传 force=true 可重新拉取。",
        )

    token = body.token or authorization
    if not token:
        raise HTTPException(
            status_code=400,
            detail="需要中和农信 Bearer token（Authorization 头或 body.token）",
        )

    admin = await _load_agri_admin_and_boundary(db, land_id)
    boundary = admin.get("boundary_geojson")
    coords: str | None = None
    if isinstance(boundary, dict):
        try:
            coords = geojson_to_coords_string(boundary)
        except ValueError:
            coords = None
    if not coords:
        coords = _land_coords_string(land)

    req_body = build_analysis_body(
        coords=coords,
        province_code=admin.get("province_code") or "",
        city_code=admin.get("city_code") or "",
        district_code=admin.get("county_code") or "",
        province_name=admin.get("province_name") or "",
        city_name=admin.get("city_name") or "",
        district_name=admin.get("county_name") or "",
        land_id=land_id,
        source=2,
    )

    try:
        payload = await analyze_soil_v2(
            bearer_token=token,
            body=req_body,
            auth_query=body.auth_query,
            hr_base_id=body.hr_base_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else 502
        detail = "上游土壤 API 调用失败"
        try:
            detail = e.response.text[:300]
        except Exception:
            pass
        logger.warning(
            "cdfinance_soil_http_error",
            land_id=str(land_id),
            status=status,
        )
        raise HTTPException(status_code=502, detail=detail) from e
    except httpx.HTTPError as e:
        logger.warning("cdfinance_soil_transport_error", error=str(e))
        raise HTTPException(status_code=502, detail="上游土壤 API 网络错误") from e

    norm = normalize_vendor_payload(payload)
    now = datetime.now(timezone.utc)

    if existing:
        row = existing
    else:
        row = SoilNutrientNpk(land_id=land_id)
        db.add(row)

    row.land_id = land_id
    row.source = SOURCE_NAME
    row.tn_g_kg = norm.get("tn_g_kg")
    row.an_mg_kg = norm.get("an_mg_kg")
    row.ap_mg_kg = norm.get("ap_mg_kg")
    row.ak_mg_kg = norm.get("ak_mg_kg")
    row.tp_g_kg = norm.get("tp_g_kg")
    row.tk_g_kg = norm.get("tk_g_kg")
    row.som_g_kg = norm.get("som_g_kg")
    row.ph = norm.get("ph")
    row.sqi_score = norm.get("sqi_score")
    row.sqi_rating = norm.get("sqi_rating")
    row.texture_usda_cn = norm.get("texture_usda_cn")
    row.vendor_log_id = norm.get("vendor_log_id")
    row.vendor_payload = payload
    row.fetched_at = now
    row.updated_at = now

    await db.commit()
    await db.refresh(row)

    logger.info(
        "soil_npk_upserted",
        land_id=land_id,
        tn=row.tn_g_kg,
        ap=row.ap_mg_kg,
        ak=row.ak_mg_kg,
    )

    return SoilNpkFetchResponse(
        land_id=str(land_id),
        status="fetched",
        npk=_npk_row_to_out(row),
        message="已从中和农信拉取并保存 NPK。",
    )


# ── Vendor site admission (cdfinance groupSiteAdmission) ─────────────


def _admission_row_to_out(
    row: GroupSiteAdmission, *, include_payload: bool = False
) -> SiteAdmissionOut:
    summary = row.summary_json if isinstance(row.summary_json, dict) else {}
    return SiteAdmissionOut(
        id=row.id,
        land_id=row.land_id,
        group_id=row.group_id,
        source=row.source,
        status=row.status,
        score=row.score,
        score_bank=row.score_bank,
        survey_id=row.survey_id,
        answer_id=row.answer_id,
        total_area_mu=row.total_area_mu,
        avg_yield=row.avg_yield,
        mu_profit=row.mu_profit,
        key_labels=summary.get("key_labels"),
        item_answers=summary.get("item_answers"),
        red_line_answers=summary.get("red_line_answers"),
        planned_crops=summary.get("planned_crops"),
        dimensions=summary.get("dimensions"),
        summary=summary,
        fetched_at=row.fetched_at,
        vendor_payload=row.vendor_payload if include_payload else None,
    )


async def _resolve_group_id_for_land(
    db: AsyncSession, land: LandParcel, explicit: str | int | None
) -> tuple[str | None, str]:
    """读取当前规范地块的 group_id，不通过 tags 或其他表做地块映射。"""
    from sqlalchemy import text as sa_text

    if explicit is not None and str(explicit).strip():
        return str(explicit).strip(), land.land_id

    result = await db.execute(
        sa_text(
            """
            SELECT group_id::text AS group_id
            FROM agric_satellite.land_parcels
            WHERE land_id = :land_id
            LIMIT 1
            """
        ),
        {"land_id": land.land_id},
    )
    row = result.mappings().first()
    return (str(row["group_id"]) if row and row.get("group_id") else None, land.land_id)


@router.get(
    "/lands/{land_id}/site-admission",
    response_model=SiteAdmissionOut,
)
async def get_site_admission(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_payload: bool = False,
):
    """Return the site-admission snapshot stored for this canonical land parcel."""
    await _get_land_or_404(land_id, ctx.org_id, db)
    result = await db.execute(
        select(GroupSiteAdmission).where(GroupSiteAdmission.land_id == land_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Site admission not yet available")
    return _admission_row_to_out(row, include_payload=include_payload)


@router.post(
    "/lands/{land_id}/site-admission",
    response_model=SiteAdmissionFetchResponse,
)
@limiter.limit("10/minute")
async def fetch_site_admission(
    request: Request,
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: SiteAdmissionFetchRequest | None = None,
    authorization: Annotated[str | None, Header()] = None,
):
    """Fetch and store the cdfinance site-admission snapshot for one land parcel."""
    from datetime import datetime, timezone

    import httpx

    from app.core.cdfinance_site_admission import (
        SOURCE_NAME,
        fetch_group_site_admission,
        normalize_admission_payload,
    )

    body = body or SiteAdmissionFetchRequest()
    land = await _get_land_or_404(land_id, ctx.org_id, db)
    group_id, _ = await _resolve_group_id_for_land(db, land, body.group_id)
    if not group_id:
        raise HTTPException(
            status_code=400,
            detail="需要 groupId（body.group_id 或当前地块的 group_id）",
        )

    existing = (
        await db.execute(
            select(GroupSiteAdmission).where(
                GroupSiteAdmission.land_id == land_id
            )
        )
    ).scalar_one_or_none()
    if existing and not body.force:
        return SiteAdmissionFetchResponse(
            land_id=land_id,
            group_id=existing.group_id,
            status="cached",
            admission=_admission_row_to_out(existing),
            message="已有现场问卷缓存；传 force=true 可重新拉取。",
        )

    token = body.token or authorization
    if not token:
        raise HTTPException(
            status_code=400,
            detail="需要中和农信 Bearer token（Authorization 头或 body.token）",
        )

    try:
        record = await fetch_group_site_admission(
            group_id=group_id,
            bearer_token=token,
            auth_query=body.auth_query,
            hr_base_id=body.hr_base_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        upstream_status = (
            exc.response.status_code if exc.response is not None else 502
        )
        detail = "上游现场问卷 API 调用失败"
        if exc.response is not None:
            try:
                detail = exc.response.text[:300]
            except Exception:
                pass
        logger.warning(
            "cdfinance_site_admission_http_error",
            land_id=land_id,
            group_id=group_id,
            status=upstream_status,
        )
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:
        logger.warning(
            "cdfinance_site_admission_transport_error", error=str(exc)
        )
        raise HTTPException(status_code=502, detail="上游现场问卷 API 网络错误") from exc

    summary = normalize_admission_payload(record)
    now = datetime.now(timezone.utc)
    summary["fetched_at"] = now.isoformat()

    if existing is None:
        existing = GroupSiteAdmission(group_id=group_id, land_id=land_id)
        db.add(existing)

    existing.land_id = land_id
    existing.group_id = str(summary.get("group_id") or group_id)
    existing.source = SOURCE_NAME
    existing.status = summary.get("status")
    existing.score = summary.get("score")
    existing.score_bank = summary.get("score_bank")
    existing.survey_id = summary.get("survey_id")
    existing.answer_id = summary.get("answer_id")
    existing.total_area_mu = summary.get("total_area_mu")
    existing.avg_yield = summary.get("avg_yield")
    existing.mu_profit = summary.get("mu_profit")
    existing.summary_json = summary
    existing.vendor_payload = record
    existing.fetched_at = now
    existing.updated_at = now

    await db.commit()
    await db.refresh(existing)

    logger.info(
        "site_admission_upserted",
        land_id=land_id,
        group_id=existing.group_id,
        score=existing.score,
    )

    return SiteAdmissionFetchResponse(
        land_id=land_id,
        group_id=existing.group_id,
        status="fetched",
        admission=_admission_row_to_out(existing),
        message="已从中和农信拉取并保存现场准入问卷。",
    )
