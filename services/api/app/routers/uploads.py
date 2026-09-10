"""Uploads router - presigned URL for direct-to-storage photo upload."""

from __future__ import annotations

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.logging import logger
from app.core.rate_limit import limiter
from app.middleware.auth import OrgContext, require_roles, org_matches, org_scope
from app.schemas.monitoring import PresignedUploadOut, PresignedUploadRequest

router = APIRouter()

# Dependency: restrict write operations to owner/admin/member (viewers are read-only)
_writer = require_roles("owner", "admin", "member")

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


@router.post("/uploads/presign", response_model=PresignedUploadOut)
@limiter.limit("10/minute")
async def get_presigned_upload(
    request: Request,
    body: PresignedUploadRequest,
    ctx: Annotated[OrgContext, Depends(_writer)],
):
    """Generate a presigned PUT URL for direct-to-storage upload."""
    if body.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported content type. Allowed: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}",
        )
    from datetime import timedelta

    from app.core.storage import get_storage

    # Generate unique object key
    ext = body.filename.rsplit(".", 1)[-1] if "." in body.filename else "jpg"
    org_seg = str(ctx.org_id) if ctx.org_id else "anon"
    object_key = f"photos/{org_seg}/{uuid.uuid4()}.{ext}"

    url = get_storage().presigned_put(
        object_key,
        expires=timedelta(minutes=15),
        content_type=body.content_type,
    )

    logger.info("presigned_upload", object_key=object_key, org_id=org_seg)
    return PresignedUploadOut(upload_url=url, object_key=object_key)
