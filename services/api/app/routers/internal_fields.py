"""Internal field resolve helpers (D2 — download host no direct PG)."""

from __future__ import annotations

import uuid as _uuid
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
