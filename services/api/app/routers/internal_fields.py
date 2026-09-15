"""Internal field resolve helpers (D2 — download host no direct PG)."""

from __future__ import annotations

import asyncio
import uuid as _uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import Field as FieldModel

router = APIRouter(prefix="/internal/fields", tags=["internal-fields"])


class FieldResolveOut(BaseModel):
    field_id: str | None = None
    land_id: str | None = None
    tags: list[Any] | None = None
    name: str | None = None


class FieldGeomOut(BaseModel):
    field_id: str
    land_id: str | None = None
    name: str | None = None
    area_ha: float | None = None
    centroid_lon: float | None = None
    centroid_lat: float | None = None
    geojson: dict[str, Any] | None = None


def _land_id_from_tags(tags: Any) -> str | None:
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("agri:"):
            lid = tag[5:].strip()
            if lid:
                return lid
    return None


def _parse_field_uuid(fid: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(str(fid))
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid field_id") from e


@router.get("/resolve", response_model=FieldResolveOut)
async def resolve_field(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    field_id: str | None = Query(default=None),
    land_id: str | None = Query(default=None),
    parcel_id: str | None = Query(default=None),
):
    """Map field_id ↔ agri land_id using fields.tags_json (exact ``agri:<land_id>``).

    Prefer exact jsonb array membership over LIKE to avoid prefix false positives.
    """
    lid = (land_id or parcel_id or "").strip() or None
    fid = (field_id or "").strip() or None

    if fid and not lid:
        fid_uuid = _parse_field_uuid(fid)
        row = (
            await db.execute(
                select(FieldModel).where(
                    FieldModel.id == fid_uuid,
                    FieldModel.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="field not found")
        tags = row.tags_json if isinstance(row.tags_json, list) else None
        return FieldResolveOut(
            field_id=str(row.id),
            land_id=_land_id_from_tags(tags),
            tags=tags,
            name=row.name,
        )

    if lid and not fid:
        # Exact tag match: tags_json @> '["agri:<land_id>"]'
        result = await db.execute(
            text(
                """
                SELECT id::text, tags_json, name
                FROM fields
                WHERE deleted_at IS NULL
                  AND tags_json @> CAST(:tag AS jsonb)
                ORDER BY created_at DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"tag": f'["agri:{lid}"]'},
        )
        row = result.first()
        if not row:
            # Fallback: LIKE for legacy non-array / stringified tags
            result = await db.execute(
                text(
                    """
                    SELECT id::text, tags_json, name
                    FROM fields
                    WHERE deleted_at IS NULL
                      AND tags_json::text LIKE :pat
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT 1
                    """
                ),
                {"pat": f"%agri:{lid}%"},
            )
            row = result.first()
        if not row:
            raise HTTPException(status_code=404, detail="land_id not mapped to field")
        tags = row[1] if isinstance(row[1], list) else None
        # Verify exact agri: tag when tags parse as list (avoid LIKE false positive)
        if tags is not None:
            exact = _land_id_from_tags(tags)
            if exact != lid:
                # LIKE matched a prefix/suffix; treat as miss
                raise HTTPException(
                    status_code=404, detail="land_id not mapped to field"
                )
        return FieldResolveOut(
            field_id=row[0],
            land_id=lid,
            tags=tags,
            name=row[2] if len(row) > 2 else None,
        )

    if fid and lid:
        # Validate field exists when both provided; return as-is if found.
        fid_uuid = _parse_field_uuid(fid)
        row = (
            await db.execute(
                select(FieldModel).where(
                    FieldModel.id == fid_uuid,
                    FieldModel.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="field not found")
        tags = row.tags_json if isinstance(row.tags_json, list) else None
        return FieldResolveOut(
            field_id=str(row.id),
            land_id=lid,
            tags=tags,
            name=row.name,
        )

    raise HTTPException(status_code=400, detail="Provide field_id and/or land_id")


@router.get("/{field_id}/geom", response_model=FieldGeomOut)
async def field_geom(
    field_id: str,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    include_geojson: int = Query(0, ge=0, le=1),
):
    """Centroid (+ optional GeoJSON) for weather/soil task bootstrap."""
    fid_uuid = _parse_field_uuid(field_id)
    cols = """
        id::text AS field_id,
        name,
        area_ha::float AS area_ha,
        tags_json,
        ST_X(ST_Centroid(geom))::float AS centroid_lon,
        ST_Y(ST_Centroid(geom))::float AS centroid_lat
    """
    if include_geojson:
        cols += ", ST_AsGeoJSON(geom)::json AS geojson"
    else:
        cols += ", NULL::json AS geojson"

    result = await db.execute(
        text(
            f"""
            SELECT {cols}
            FROM fields
            WHERE id = CAST(:fid AS uuid)
              AND deleted_at IS NULL
            """
        ),
        {"fid": str(fid_uuid)},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="field not found")
    tags = row["tags_json"] if isinstance(row["tags_json"], list) else None
    return FieldGeomOut(
        field_id=row["field_id"],
        land_id=_land_id_from_tags(tags),
        name=row["name"],
        area_ha=row["area_ha"],
        centroid_lon=row["centroid_lon"],
        centroid_lat=row["centroid_lat"],
        geojson=row["geojson"] if include_geojson else None,
    )



class FieldTagsPatch(BaseModel):
    """Set agri / cdfinance tags without requiring agri.land_parcels."""

    land_id: str | None = None
    group_id: str | None = None
    tags: list[str] | None = None


@router.patch("/{field_id}/tags", response_model=FieldResolveOut)
async def patch_field_tags(
    field_id: str,
    body: FieldTagsPatch,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Merge agri / cdfinance group tags into ``fields.tags_json``.

    Ops / download-machine can fix tagging without a user JWT.
    Does not require an ``agri.land_parcels`` row.
    """
    from app.core.agri_tags import (
        ensure_agri_land_tag,
        ensure_cdfinance_group_tag,
        iter_tag_strings,
        parse_agri_land_id,
    )

    fid_uuid = _parse_field_uuid(field_id)
    row = (
        await db.execute(
            select(FieldModel).where(
                FieldModel.id == fid_uuid,
                FieldModel.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="field not found")

    if body.tags is not None:
        tags = list(iter_tag_strings(body.tags))
    else:
        tags = list(iter_tag_strings(row.tags_json))

    if body.land_id is not None:
        tags = ensure_agri_land_tag(tags, body.land_id)
    if body.group_id is not None:
        gid = str(body.group_id).strip()
        # Replace any prior group tag (mirror frontend withAgriFieldTags).
        tags = [
            t
            for t in tags
            if not t.startswith("cdfinance_group:") and not t.startswith("group:")
        ]
        if gid:
            tags = ensure_cdfinance_group_tag(tags, gid)

    row.tags_json = tags
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    out_tags = row.tags_json if isinstance(row.tags_json, list) else tags
    return FieldResolveOut(
        field_id=str(row.id),
        land_id=parse_agri_land_id(out_tags),
        tags=out_tags if isinstance(out_tags, list) else None,
        name=row.name,
    )


# ── D4.1: assessment / season-growth / readiness bundles ─────────────


class DataReadinessOut(BaseModel):
    field_id: str
    land_id: str | None = None
    weather_rows: int = 0
    soil_ok: bool = False
    s2_dates: int = 0
    s1_dates: int = 0
    span_days: int | None = None


def _sync_load_assessment_bundle(field_id: str) -> dict[str, Any]:
    """Run ingest-equivalent load_field_bundle on API SyncSession (no HTTP recurse)."""
    from app.core.database_sync import SyncSession
    from app.reports.land_assessment.data_loader import load_field_bundle

    fid = _parse_field_uuid(field_id)
    session = SyncSession()
    try:
        return load_field_bundle(session, fid, allow_http=False)
    finally:
        session.close()


def _tag_land_id(tags: Any) -> str | None:
    return _land_id_from_tags(tags)


@router.get("/{field_id}/assessment-bundle")
async def assessment_bundle(
    field_id: str,
    _: InternalAuth,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    """JSON sufficient for compute_assessment / PDF (same shape as load_field_bundle).

    ``date_from`` / ``date_to`` are accepted for forward compatibility; the current
    loader uses crop-season lookback rather than an explicit window.
    """
    del date_from, date_to  # reserved
    try:
        bundle = await asyncio.to_thread(_sync_load_assessment_bundle, field_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"assessment-bundle failed: {e}"
        ) from e
    return bundle


@router.get("/{field_id}/data-readiness", response_model=DataReadinessOut)
async def data_readiness(
    field_id: str,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    # 让 FastAPI 先完成 ISO 日期校验并传递 date，避免 asyncpg 将字符串绑定到 DATE 参数时失败。
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
):
    """Weather row count + soil profile + agri S2/S1 coverage for bootstrap wait."""
    fid = _parse_field_uuid(field_id)
    field_row = (
        await db.execute(
            select(FieldModel).where(
                FieldModel.id == fid,
                FieldModel.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not field_row:
        raise HTTPException(status_code=404, detail="field not found")
    land_id = _tag_land_id(field_row.tags_json)

    weather_params: dict[str, Any] = {"fid": str(fid)}
    weather_sql = "SELECT count(*)::int AS n FROM weather_daily WHERE field_id = CAST(:fid AS uuid)"
    if date_from:
        weather_sql += " AND date >= CAST(:d0 AS date)"
        weather_params["d0"] = date_from
    if date_to:
        weather_sql += " AND date <= CAST(:d1 AS date)"
        weather_params["d1"] = date_to
    weather_rows = int(
        (await db.execute(text(weather_sql), weather_params)).scalar() or 0
    )

    soil_ok = bool(
        (
            await db.execute(
                text(
                    "SELECT 1 FROM soil_profiles WHERE field_id = CAST(:fid AS uuid) LIMIT 1"
                ),
                {"fid": str(fid)},
            )
        ).scalar()
    )

    s2_dates = 0
    s1_dates = 0
    span_days = None
    if land_id and date_from and date_to:
        if date_to >= date_from:
            span_days = (date_to - date_from).days + 1
            cov = (
                (
                    await db.execute(
                        text(
                            """
                        SELECT
                          COUNT(DISTINCT date) FILTER (WHERE sensor = 'S2') AS s2_dates,
                          COUNT(DISTINCT date) FILTER (WHERE sensor = 'S1') AS s1_dates
                        FROM agri.parcel_scene_products
                        WHERE land_id = :land_id
                          AND date >= CAST(:d0 AS date)
                          AND date <= CAST(:d1 AS date)
                          AND COALESCE(scene_id, '') NOT LIKE '%_decloud'
                          AND COALESCE(pixel_data->>'source', '') <> 'uncrtaints_decloud'
                        """
                        ),
                        {
                            "land_id": land_id,
                            "d0": date_from,
                            "d1": date_to,
                        },
                    )
                )
                .mappings()
                .first()
            )
            s2_dates = int((cov or {}).get("s2_dates") or 0)
            s1_dates = int((cov or {}).get("s1_dates") or 0)

    return DataReadinessOut(
        field_id=str(fid),
        land_id=land_id,
        weather_rows=weather_rows,
        soil_ok=soil_ok,
        s2_dates=s2_dates,
        s1_dates=s1_dates,
        span_days=span_days,
    )


def _sync_load_season_growth_inputs(
    field_id: str, date_from: str, date_to: str
) -> dict[str, Any]:
    """Field meta + agri S2/S1 rows + classic indices for season-growth facts."""
    from datetime import date as _date

    from sqlalchemy import text as sa_text

    from app.core.agri_classify import (
        CLOUD_MAX_PCT,
        cloud_pct,
        is_official_optical_product,
        parse_s1_relative_orbit,
    )
    from app.core.agri_tags import parse_agri_land_id
    from app.core.database_sync import SyncSession
    from app.models.tables import Field as FieldTbl
    from app.models.tables import FieldStat, RasterLayer

    start = _date.fromisoformat(date_from[:10])
    end = _date.fromisoformat(date_to[:10])
    if end < start:
        raise ValueError("date_to must be >= date_from")

    fid = _parse_field_uuid(field_id)
    session = SyncSession()
    try:
        field = session.get(FieldTbl, fid)
        if not field or field.deleted_at is not None:
            raise ValueError(f"Field not found: {field_id}")
        land_id = parse_agri_land_id(field.tags_json)
        field_meta = {
            "field_id": str(fid),
            "field_name": field.name or "地块",
            "land_id": land_id,
            "crop_type": field.crop_type,
            "area_ha": float(field.area_ha) if field.area_ha is not None else None,
            "tags": field.tags_json if isinstance(field.tags_json, list) else [],
        }

        def _num(v: Any) -> float | None:
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            if f != f or f in (float("inf"), float("-inf")):
                return None
            return f

        def _iso(d: Any) -> str:
            if hasattr(d, "isoformat"):
                return d.isoformat()[:10]
            return str(d)[:10]

        s2_rows: list[dict[str, Any]] = []
        s1_rows: list[dict[str, Any]] = []
        classic_indices: list[dict[str, Any]] = []

        if land_id:
            rows = (
                session.execute(
                    sa_text(
                        """
                        SELECT date, scene_id, ndvi_avg, evi_avg, mndwi_avg, ndmi_avg,
                               parcel_cloud_cover_pct, cloud_cover,
                               pixel_data->>'source' AS source,
                               pixel_data->>'decloud_quality' AS decloud_quality,
                               rgb_url, large_rgb_url, rgb_oss_key,
                               pixel_data->>'format' AS pixel_format,
                               CASE
                                 WHEN jsonb_typeof(pixel_data->'pixels') = 'array'
                                 THEN jsonb_array_length(pixel_data->'pixels')
                                 ELSE 0
                               END AS pixel_n
                        FROM agri.parcel_scene_products
                        WHERE land_id = :land_id AND sensor = 'S2'
                          AND date >= :start_date AND date <= :end_date
                        ORDER BY date
                        """
                    ),
                    {
                        "land_id": land_id,
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                    },
                )
                .mappings()
                .all()
            )
            for r in rows:
                cloud = cloud_pct(r["parcel_cloud_cover_pct"], r["cloud_cover"])
                official = is_official_optical_product(
                    source=r.get("source"),
                    scene_id=r.get("scene_id"),
                    parcel_cloud_cover_pct=r["parcel_cloud_cover_pct"],
                    cloud_cover=r["cloud_cover"],
                    decloud_quality=r.get("decloud_quality"),
                    cloud_max_pct=CLOUD_MAX_PCT,
                )
                s2_rows.append(
                    {
                        "date": _iso(r["date"]),
                        "scene_id": r.get("scene_id"),
                        "ndvi_avg": _num(r["ndvi_avg"]),
                        "evi_avg": _num(r["evi_avg"]),
                        "ndmi_avg": _num(r["ndmi_avg"]),
                        "mndwi_avg": _num(r["mndwi_avg"]),
                        "parcel_cloud_cover_pct": _num(r["parcel_cloud_cover_pct"]),
                        "cloud_cover": _num(r["cloud_cover"]),
                        "cloud_pct": cloud,
                        "decloud_quality": r.get("decloud_quality"),
                        "source": r.get("source"),
                        "official": bool(official),
                        "clear": cloud is not None and cloud <= CLOUD_MAX_PCT,
                        "rgb_url": r.get("rgb_url") or None,
                        "large_rgb_url": r.get("large_rgb_url") or None,
                        "rgb_oss_key": r.get("rgb_oss_key") or None,
                        "pixel_format": r.get("pixel_format"),
                        "pixel_n": int(r["pixel_n"] or 0)
                        if r.get("pixel_n") is not None
                        else 0,
                    }
                )

            s1_raw = (
                session.execute(
                    sa_text(
                        """
                        SELECT date, scene_id, vv_avg, vh_avg,
                               pixel_data->>'relative_orbit' AS relative_orbit,
                               rgb_url, large_rgb_url, rgb_oss_key
                        FROM agri.parcel_scene_products
                        WHERE land_id = :land_id AND sensor = 'S1'
                          AND date >= :start_date AND date <= :end_date
                        ORDER BY date
                        """
                    ),
                    {
                        "land_id": land_id,
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                    },
                )
                .mappings()
                .all()
            )
            for r in s1_raw:
                orbit = r.get("relative_orbit")
                if orbit is None and r.get("scene_id"):
                    orbit = parse_s1_relative_orbit(r.get("scene_id"))
                s1_rows.append(
                    {
                        "date": _iso(r["date"]),
                        "scene_id": r.get("scene_id"),
                        "vv_avg": _num(r["vv_avg"]),
                        "vh_avg": _num(r["vh_avg"]),
                        "relative_orbit": orbit,
                        "rgb_url": r.get("rgb_url") or None,
                        "large_rgb_url": r.get("large_rgb_url") or None,
                        "rgb_oss_key": r.get("rgb_oss_key") or None,
                    }
                )
        else:
            from sqlalchemy import select as sa_select

            rows = session.execute(
                sa_select(
                    FieldStat.date,
                    RasterLayer.layer_type,
                    FieldStat.mean,
                    FieldStat.median,
                    FieldStat.quality_score,
                )
                .join(RasterLayer, RasterLayer.id == FieldStat.layer_id)
                .where(
                    FieldStat.field_id == fid,
                    FieldStat.date >= start,
                    FieldStat.date <= end,
                )
                .order_by(FieldStat.date)
            ).all()
            for r in rows:
                classic_indices.append(
                    {
                        "date": _iso(r.date),
                        "layer_type": r.layer_type,
                        "mean": _num(r.mean),
                        "median": _num(r.median),
                        "quality_score": _num(r.quality_score) or 0.5,
                    }
                )

        # Prior-year window (same DOY span, previous year) for YoY — optional.
        prior_s2: list[dict[str, Any]] = []
        try:
            prior_start = start.replace(year=start.year - 1)
        except ValueError:
            prior_start = start.replace(year=start.year - 1, day=28)
        try:
            prior_end = end.replace(year=end.year - 1)
        except ValueError:
            prior_end = end.replace(year=end.year - 1, day=28)
        if land_id:
            rows = (
                session.execute(
                    sa_text(
                        """
                        SELECT date, scene_id, ndvi_avg, ndmi_avg,
                               parcel_cloud_cover_pct, cloud_cover,
                               pixel_data->>'source' AS source,
                               pixel_data->>'decloud_quality' AS decloud_quality
                        FROM agri.parcel_scene_products
                        WHERE land_id = :land_id AND sensor = 'S2'
                          AND date >= :start_date AND date <= :end_date
                        ORDER BY date
                        """
                    ),
                    {
                        "land_id": land_id,
                        "start_date": prior_start.isoformat(),
                        "end_date": prior_end.isoformat(),
                    },
                )
                .mappings()
                .all()
            )
            for r in rows:
                cloud = cloud_pct(r["parcel_cloud_cover_pct"], r["cloud_cover"])
                official = is_official_optical_product(
                    source=r.get("source"),
                    scene_id=r.get("scene_id"),
                    parcel_cloud_cover_pct=r["parcel_cloud_cover_pct"],
                    cloud_cover=r["cloud_cover"],
                    decloud_quality=r.get("decloud_quality"),
                    cloud_max_pct=CLOUD_MAX_PCT,
                )
                prior_s2.append(
                    {
                        "date": _iso(r["date"]),
                        "scene_id": r.get("scene_id"),
                        "ndvi_avg": _num(r["ndvi_avg"]),
                        "ndmi_avg": _num(r["ndmi_avg"]),
                        "cloud_pct": cloud,
                        "official": bool(official),
                        "clear": cloud is not None and cloud <= CLOUD_MAX_PCT,
                    }
                )

        return {
            "field": field_meta,
            "land_id": land_id,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "s2_rows": s2_rows,
            "s1_rows": s1_rows,
            "classic_indices": classic_indices,
            "prior_s2_rows": prior_s2,
            "prior_window": {
                "start_date": prior_start.isoformat(),
                "end_date": prior_end.isoformat(),
            },
        }
    finally:
        session.close()


@router.get("/{field_id}/season-growth-inputs")
async def season_growth_inputs(
    field_id: str,
    _: InternalAuth,
    date_from: str = Query(..., min_length=8, max_length=32),
    date_to: str = Query(..., min_length=8, max_length=32),
):
    """Raw rows for season-growth ``build_season_facts`` without download PG."""
    try:
        payload = await asyncio.to_thread(
            _sync_load_season_growth_inputs, field_id, date_from, date_to
        )
    except ValueError as e:
        msg = str(e)
        code = 404 if "not found" in msg.lower() else 400
        raise HTTPException(status_code=code, detail=msg) from e
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"season-growth-inputs failed: {e}"
        ) from e
    return payload
