# -*- coding: utf-8 -*-
"""Best-effort cdfinance prefetch for canonical land-parcel report jobs.

The report worker receives one canonical land_id. This module deliberately
does not resolve UUID fields, tags, or a second parcel identifier at runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.models.tables import LandParcel, GroupSiteAdmission, SoilNutrientNpk


def normalize_optional_token(token: str | None) -> str | None:
    raw = (token or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw or None


def normalize_optional_group_id(group_id: str | int | None) -> str | None:
    if group_id is None:
        return None
    value = str(group_id).strip()
    return value or None


def normalize_optional_hr_base_id(hr_base_id: str | int | None) -> str | None:
    """Empty / whitespace → None so env CDFINANCE_HR_BASE_ID remains the fallback."""
    if hr_base_id is None:
        return None
    value = str(hr_base_id).strip()
    return value or None


def resolve_request_token(
    *,
    cdfinance_token: str | None = None,
    token: str | None = None,
    authorization: str | None = None,
) -> str | None:
    """Prefer body.cdfinance_token, then body.token, then Authorization header."""
    return (
        normalize_optional_token(cdfinance_token)
        or normalize_optional_token(token)
        or normalize_optional_token(authorization)
    )


async def _resolve_group_id(
    db: AsyncSession, land: LandParcel, explicit: str | None
) -> tuple[str | None, str]:
    """Read group_id from the current parcel row; no tag or parcel mapping is used."""
    if explicit:
        return explicit, land.land_id

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
    return (
        str(row["group_id"]) if row and row.get("group_id") else None,
        land.land_id,
    )


def _land_coords_string_soft(land: LandParcel) -> str | None:
    from app.core.cdfinance_soil import geojson_to_coords_string

    # 供应商请求优先使用规范地块的 GeoJSON 边界，避免再从另一套地块表取形状。
    if isinstance(land.boundary_geojson, dict):
        try:
            return geojson_to_coords_string(land.boundary_geojson)
        except Exception:
            return None

    if land.geom is None:
        return None

    try:
        from geoalchemy2.shape import to_shape
        from shapely.geometry import mapping

        return geojson_to_coords_string(mapping(to_shape(land.geom)))
    except Exception:
        return None


async def _load_land_admin_and_boundary(
    db: AsyncSession, land_id: str
) -> dict[str, Any]:
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


async def prefetch_site_admission(
    db: AsyncSession,
    land: LandParcel,
    *,
    bearer_token: str,
    group_id: str | None = None,
    hr_base_id: str | None = None,
    force: bool = True,
) -> dict[str, Any]:
    """Fetch and upsert site admission for one canonical land parcel."""
    from app.core.cdfinance_site_admission import (
        SOURCE_NAME,
        fetch_group_site_admission,
        normalize_admission_payload,
    )

    resolved_group_id, land_id = await _resolve_group_id(db, land, group_id)
    if not resolved_group_id:
        return {"status": "skipped", "reason": "no_group_id"}

    existing = (
        await db.execute(
            select(GroupSiteAdmission).where(
                GroupSiteAdmission.land_id == land_id
            )
        )
    ).scalar_one_or_none()
    if existing and not force:
        return {"status": "cached", "group_id": existing.group_id}

    record = await fetch_group_site_admission(
        group_id=resolved_group_id,
        bearer_token=bearer_token,
        hr_base_id=hr_base_id,
    )
    summary = normalize_admission_payload(record)
    now = datetime.now(timezone.utc)
    summary["fetched_at"] = now.isoformat()

    row = existing
    if row is None:
        row = GroupSiteAdmission(group_id=resolved_group_id, land_id=land_id)
        db.add(row)

    row.land_id = land_id
    row.group_id = str(summary.get("group_id") or resolved_group_id)
    row.source = SOURCE_NAME
    row.status = summary.get("status")
    row.score = summary.get("score")
    row.score_bank = summary.get("score_bank")
    row.survey_id = summary.get("survey_id")
    row.answer_id = summary.get("answer_id")
    row.total_area_mu = summary.get("total_area_mu")
    row.avg_yield = summary.get("avg_yield")
    row.mu_profit = summary.get("mu_profit")
    row.summary_json = summary
    row.vendor_payload = record
    row.fetched_at = now
    row.updated_at = now

    await db.flush()
    logger.info(
        "report_prefetch_site_admission",
        land_id=land_id,
        group_id=row.group_id,
        score=row.score,
    )
    return {"status": "fetched", "group_id": row.group_id}


async def prefetch_soil_npk(
    db: AsyncSession,
    land: LandParcel,
    *,
    bearer_token: str,
    hr_base_id: str | None = None,
    force: bool = True,
) -> dict[str, Any]:
    """Fetch and upsert vendor NPK for one canonical land parcel."""
    from app.core.cdfinance_soil import (
        SOURCE_NAME,
        analyze_soil_v2,
        build_analysis_body,
        normalize_vendor_payload,
    )

    land_id = land.land_id
    existing = (
        await db.execute(
            select(SoilNutrientNpk).where(SoilNutrientNpk.land_id == land_id)
        )
    ).scalar_one_or_none()
    if existing and not force:
        return {"status": "cached"}

    admin = await _load_land_admin_and_boundary(db, land_id)
    coords = None
    boundary = admin.get("boundary_geojson")
    if isinstance(boundary, dict):
        from app.core.cdfinance_soil import geojson_to_coords_string

        try:
            coords = geojson_to_coords_string(boundary)
        except ValueError:
            coords = None
    if not coords:
        coords = _land_coords_string_soft(land)
    if not coords:
        return {"status": "skipped", "reason": "no_coords"}

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
    payload = await analyze_soil_v2(
        bearer_token=bearer_token, body=req_body, hr_base_id=hr_base_id
    )
    norm = normalize_vendor_payload(payload)
    now = datetime.now(timezone.utc)

    row = existing or SoilNutrientNpk(land_id=land_id)
    if existing is None:
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

    await db.flush()
    logger.info(
        "report_prefetch_soil_npk",
        land_id=land_id,
        tn=row.tn_g_kg,
    )
    return {"status": "fetched"}


async def prefetch_cdfinance_for_report(
    db: AsyncSession,
    land: LandParcel,
    *,
    token: str | None,
    group_id: str | int | None = None,
    hr_base_id: str | int | None = None,
    force: bool = True,
) -> dict[str, Any]:
    """Soft prefetch for report generation; the token is never persisted."""
    out: dict[str, Any] = {
        "token_provided": False,
        "site_admission": None,
        "soil_npk": None,
        "hr_base_id": None,
    }
    bearer = normalize_optional_token(token)
    gid = normalize_optional_group_id(group_id)
    hid = normalize_optional_hr_base_id(hr_base_id)
    if hid:
        out["hr_base_id"] = hid
    if not bearer:
        return out
    out["token_provided"] = True

    try:
        resolved_gid, _ = await _resolve_group_id(db, land, gid)
        if resolved_gid or gid:
            out["site_admission"] = await prefetch_site_admission(
                db,
                land,
                bearer_token=bearer,
                group_id=gid,
                hr_base_id=hid,
                force=force,
            )
        else:
            out["site_admission"] = {"status": "skipped", "reason": "no_group_id"}
    except httpx.HTTPError as exc:
        logger.warning(
            "report_prefetch_site_admission_http",
            land_id=land.land_id,
            error=str(exc),
        )
        out["site_admission"] = {"status": "error", "reason": "http"}
    except Exception as exc:
        logger.warning(
            "report_prefetch_site_admission_failed",
            land_id=land.land_id,
            error=str(exc),
        )
        out["site_admission"] = {"status": "error", "reason": str(exc)[:200]}

    try:
        out["soil_npk"] = await prefetch_soil_npk(
            db, land, bearer_token=bearer, hr_base_id=hid, force=force
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "report_prefetch_soil_npk_http",
            land_id=land.land_id,
            error=str(exc),
        )
        out["soil_npk"] = {"status": "error", "reason": "http"}
    except Exception as exc:
        logger.warning(
            "report_prefetch_soil_npk_failed",
            land_id=land.land_id,
            error=str(exc),
        )
        out["soil_npk"] = {"status": "error", "reason": str(exc)[:200]}

    return out


async def land_has_site_admission(db: AsyncSession, land_id: str) -> bool:
    row = (
        await db.execute(
            select(GroupSiteAdmission.id).where(
                GroupSiteAdmission.land_id == land_id
            )
        )
    ).scalar_one_or_none()
    return row is not None
