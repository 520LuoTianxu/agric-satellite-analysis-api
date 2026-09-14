"""Internal field resolve helpers (D2 scaffold — download host no direct PG)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.internal_auth import InternalAuth
from app.models.tables import Field

router = APIRouter(prefix="/internal/fields", tags=["internal-fields"])


class FieldResolveOut(BaseModel):
    field_id: str | None = None
    land_id: str | None = None
    tags: list[Any] | None = None


@router.get("/resolve", response_model=FieldResolveOut)
async def resolve_field(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    field_id: str | None = Query(default=None),
    land_id: str | None = Query(default=None),
    parcel_id: str | None = Query(default=None),
):
    """Map field_id ↔ agri land_id using fields.tags_json (agri:<land_id>)."""
    lid = land_id or parcel_id
    fid = field_id
    tags: list[Any] | None = None

    if fid and not lid:
        try:
            import uuid as _uuid
            fid_uuid = _uuid.UUID(str(fid))
        except ValueError as e:
            raise HTTPException(status_code=400, detail="invalid field_id") from e
        row = (
            await db.execute(select(Field).where(Field.id == fid_uuid))
        ).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="field not found")
        tags = row.tags_json if isinstance(row.tags_json, list) else None
        if tags:
            for tag in tags:
                if isinstance(tag, str) and tag.startswith("agri:"):
                    lid = tag[5:].strip() or lid
                    break
        return FieldResolveOut(field_id=str(row.id), land_id=lid, tags=tags)

    if lid and not fid:
        result = await db.execute(
            text(
                """
                SELECT id::text, tags_json
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
        return FieldResolveOut(field_id=row[0], land_id=lid, tags=tags)

    if fid and lid:
        return FieldResolveOut(field_id=fid, land_id=lid, tags=None)

    raise HTTPException(status_code=400, detail="Provide field_id and/or land_id")
