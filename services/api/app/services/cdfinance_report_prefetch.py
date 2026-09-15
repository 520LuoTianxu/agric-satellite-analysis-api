# -*- coding: utf-8 -*-
"""Best-effort cdfinance site-admission + NPK prefetch before report PDF jobs.

Called from assessment / season-growth generate so ``group_site_admission`` and
``soil_nutrient_npk`` are fresh in DB when the ingest PDF worker loads facts.
Never stores the Bearer token in job params or MQ extras.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger
from app.models.tables import Field, GroupSiteAdmission, SoilNutrientNpk


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
    s = str(group_id).strip()
    return s or None


def normalize_optional_hr_base_id(hr_base_id: str | int | None) -> str | None:
    """Empty / whitespace → None so env CDFINANCE_HR_BASE_ID remains the fallback."""
    if hr_base_id is None:
        return None
    s = str(hr_base_id).strip()
    return s or None


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
    db: AsyncSession, field: Field, explicit: str | None
) -> tuple[str | None, str | None]:
    from app.core.agri_tags import parse_agri_land_id, parse_cdfinance_group_id

    land_id = parse_agri_land_id(field.tags_json)
    if explicit:
        return explicit, land_id

    tagged = parse_cdfinance_group_id(field.tags_json)
    if tagged:
        return tagged, land_id

    if land_id:
        result = await db.execute(
            sa_text(
                """
                SELECT group_id::text AS group_id
                FROM agri.land_parcels
                WHERE land_id = :land_id
                LIMIT 1
                """
            ),
            {"land_id": land_id},
        )
        row = result.mappings().first()
        if row and row.get("group_id"):
            return str(row["group_id"]), land_id
    return None, land_id


def _field_coords_string_soft(field: Field) -> str | None:
    from geoalchemy2.shape import to_shape
    from shapely.geometry import mapping

    from app.core.cdfinance_soil import geojson_to_coords_string

    if field.geom is None:
        return None
    try:
        gj = mapping(to_shape(field.geom))
        return geojson_to_coords_string(gj)
    except Exception:
        return None


async def _load_agri_admin_and_boundary(
    db: AsyncSession, land_id: str
) -> dict[str, Any]:
    result = await db.execute(
        sa_text(
            """
            SELECT land_id, province_code, province_name, city_code, city_name,
                   county_code, county_name, boundary_geojson
            FROM agri.land_parcels
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
    field: Field,
    *,
    bearer_token: str,
    group_id: str | None = None,
    hr_base_id: str | None = None,
    force: bool = True,
    link_field_tag: bool = True,
) -> dict[str, Any]:
    """Fetch+upsert site admission. Raises on hard failures (caller soft-catches)."""
    from app.core.agri_tags import ensure_cdfinance_group_tag
    from app.core.cdfinance_site_admission import (
        SOURCE_NAME,
        fetch_group_site_admission,
        normalize_admission_payload,
    )

    gid, land_id = await _resolve_group_id(db, field, group_id)
    if not gid:
        return {"status": "skipped", "reason": "no_group_id"}

    existing = (
        await db.execute(
            select(GroupSiteAdmission).where(GroupSiteAdmission.group_id == gid)
        )
    ).scalar_one_or_none()
    field_row = (
        await db.execute(
            select(GroupSiteAdmission).where(GroupSiteAdmission.field_id == field.id)
        )
    ).scalar_one_or_none()

    if not force:
        if field_row:
            return {"status": "cached", "group_id": field_row.group_id}
        if existing and existing.field_id == field.id:
            return {"status": "cached", "group_id": existing.group_id}
        if existing and existing.field_id is None:
            existing.field_id = field.id
            if land_id and not existing.land_id:
                existing.land_id = land_id
            if link_field_tag:
                field.tags_json = ensure_cdfinance_group_tag(field.tags_json, gid)
            await db.flush()
            return {"status": "linked", "group_id": existing.group_id}

    record = await fetch_group_site_admission(
        group_id=gid,
        bearer_token=bearer_token,
        hr_base_id=hr_base_id,
    )
    summary = normalize_admission_payload(record)
    now = datetime.now(timezone.utc)
    summary["fetched_at"] = now.isoformat()

    row = existing or field_row
    if row is None:
        row = GroupSiteAdmission(group_id=gid)
        db.add(row)

    row.field_id = field.id
    row.group_id = str(summary.get("group_id") or gid)
    row.land_id = land_id
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

    if link_field_tag:
        field.tags_json = ensure_cdfinance_group_tag(field.tags_json, row.group_id)

    await db.flush()
    logger.info(
        "report_prefetch_site_admission",
        field_id=str(field.id),
        group_id=row.group_id,
        score=row.score,
    )
    return {"status": "fetched", "group_id": row.group_id}


async def prefetch_soil_npk(
    db: AsyncSession,
    field: Field,
    *,
    bearer_token: str,
    hr_base_id: str | None = None,
    force: bool = True,
) -> dict[str, Any]:
    """Fetch+upsert vendor NPK. Raises on hard failures (caller soft-catches)."""
    from app.core.agri_tags import parse_agri_land_id
    from app.core.cdfinance_soil import (
        SOURCE_NAME,
        analyze_soil_v2,
        build_analysis_body,
        geojson_to_coords_string,
        normalize_vendor_payload,
    )

    existing = (
        await db.execute(
            select(SoilNutrientNpk).where(SoilNutrientNpk.field_id == field.id)
        )
    ).scalar_one_or_none()
    if existing and not force:
        return {"status": "cached"}

    land_id = parse_agri_land_id(field.tags_json)
    admin: dict[str, Any] = {}
    coords: str | None = None
    if land_id:
        admin = await _load_agri_admin_and_boundary(db, land_id)
        bj = admin.get("boundary_geojson")
        if bj:
            try:
                coords = geojson_to_coords_string(bj if isinstance(bj, dict) else None)
            except ValueError:
                coords = None
    if not coords:
        coords = _field_coords_string_soft(field)
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

    row = existing or SoilNutrientNpk(field_id=field.id)
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
        field_id=str(field.id),
        land_id=land_id,
        tn=row.tn_g_kg,
    )
    return {"status": "fetched"}


async def prefetch_cdfinance_for_report(
    db: AsyncSession,
    field: Field,
    *,
    token: str | None,
    group_id: str | int | None = None,
    hr_base_id: str | int | None = None,
    force: bool = True,
) -> dict[str, Any]:
    """Soft prefetch: never raises; returns status map for logging / job params.

    - token + resolvable group_id → site admission upsert
    - token → NPK upsert (best-effort)
    Token is never returned or persisted.
    Request ``hr_base_id`` overrides env ``CDFINANCE_HR_BASE_ID`` when set.
    """
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
        resolved_gid, _ = await _resolve_group_id(db, field, gid)
        if resolved_gid or gid:
            out["site_admission"] = await prefetch_site_admission(
                db,
                field,
                bearer_token=bearer,
                group_id=gid,
                hr_base_id=hid,
                force=force,
            )
        else:
            out["site_admission"] = {"status": "skipped", "reason": "no_group_id"}
    except httpx.HTTPError as e:
        logger.warning(
            "report_prefetch_site_admission_http",
            field_id=str(field.id),
            error=str(e),
        )
        out["site_admission"] = {"status": "error", "reason": "http"}
    except Exception as e:
        logger.warning(
            "report_prefetch_site_admission_failed",
            field_id=str(field.id),
            error=str(e),
        )
        out["site_admission"] = {"status": "error", "reason": str(e)[:200]}

    try:
        out["soil_npk"] = await prefetch_soil_npk(
            db, field, bearer_token=bearer, hr_base_id=hid, force=force
        )
    except httpx.HTTPError as e:
        logger.warning(
            "report_prefetch_soil_npk_http",
            field_id=str(field.id),
            error=str(e),
        )
        out["soil_npk"] = {"status": "error", "reason": "http"}
    except Exception as e:
        logger.warning(
            "report_prefetch_soil_npk_failed",
            field_id=str(field.id),
            error=str(e),
        )
        out["soil_npk"] = {"status": "error", "reason": str(e)[:200]}

    return out


async def field_has_site_admission(db: AsyncSession, field_id: uuid.UUID) -> bool:
    row = (
        await db.execute(
            select(GroupSiteAdmission.id).where(GroupSiteAdmission.field_id == field_id)
        )
    ).scalar_one_or_none()
    return row is not None
