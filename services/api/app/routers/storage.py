"""Object storage admin / parcel-product APIs (MinIO or Aliyun OSS)."""

from __future__ import annotations

import base64
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.logging import logger
from app.core.storage import get_storage, parcel_product_prefix
from app.tasks.storage_tasks import put_bytes_via_storage
from app.middleware.auth import OrgContext, require_roles

router = APIRouter()

_reader = require_roles("owner", "admin", "member", "viewer")
_writer = require_roles("owner", "admin", "member")

_ALLOWED_PUT_PREFIXES = ("photos/", "cogs/")


def _allowed_key(key: str) -> bool:
    if not key or key.startswith("/") or ".." in key.split("/"):
        return False
    parcel = parcel_product_prefix()
    if parcel and key.startswith(parcel):
        return True
    return any(key.startswith(p) for p in _ALLOWED_PUT_PREFIXES)


class PullParcelProductsBody(BaseModel):
    keys: list[str] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=5000)


@router.get("/storage/backend")
async def storage_backend_info(ctx: Annotated[OrgContext, Depends(_reader)]):
    """Return active storage backend metadata."""
    storage = get_storage()
    return {
        "backend": storage.backend,
        "bucket": storage.bucket,
        "parcel_prefix": parcel_product_prefix(),
    }


@router.get("/storage/objects")
async def list_storage_objects(
    ctx: Annotated[OrgContext, Depends(_reader)],
    prefix: str | None = Query(default=None),
    suffix: str = Query(default=""),
    limit: int = Query(default=100, ge=0, le=5000),
):
    """List object keys under a prefix."""
    storage = get_storage()
    if prefix is None:
        prefix = parcel_product_prefix() if storage.backend == "oss" else ""
    keys = storage.list_keys(prefix=prefix, suffix=suffix, limit=limit)
    return {
        "backend": storage.backend,
        "bucket": storage.bucket,
        "prefix": prefix,
        "suffix": suffix,
        "count": len(keys),
        "keys": keys,
    }


@router.get("/storage/object")
async def get_storage_object(
    ctx: Annotated[OrgContext, Depends(_reader)],
    key: str = Query(...),
    download: int = Query(default=0),
):
    """Return object metadata, optionally streaming the body as an attachment."""
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    storage = get_storage()
    if not storage.exists(key):
        raise HTTPException(status_code=404, detail=f"Object not found: {key}")

    meta = {
        "backend": storage.backend,
        "bucket": storage.bucket,
        "key": key,
        "uri": storage.uri_for(key),
        "public_url": storage.public_url(key),
        "exists": True,
    }
    if download != 1:
        return meta

    data = storage.get_bytes(key)
    filename = key.rsplit("/", 1)[-1] or "download"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Storage-Key": key,
        },
    )


@router.put("/storage/object")
async def put_storage_object(
    request: Request,
    ctx: Annotated[OrgContext, Depends(_writer)],
    key: str = Query(...),
):
    """Upload raw request body to an allowed key prefix."""
    if not _allowed_key(key):
        raise HTTPException(
            status_code=400,
            detail=(
                "key must be under photos/, cogs/, or the configured parcel "
                f"prefix ({parcel_product_prefix()!r})"
            ),
        )
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    content_type = request.headers.get("content-type") or "application/octet-stream"
    result = put_bytes_via_storage(key, body, content_type=content_type)
    logger.info(
        "storage_put_object", key=key, bytes=len(body), backend=result["backend"]
    )
    return {
        "ok": True,
        "key": key,
        "bytes": len(body),
        "uri": result["uri"],
    }


@router.post("/storage/parcel-products/pull")
async def pull_parcel_products(
    ctx: Annotated[OrgContext, Depends(_reader)],
    body: PullParcelProductsBody | None = None,
):
    """Pull parcel-product JSON from the fixed OSS/MinIO prefix (summary only).

    Fetches each object via get_bytes but does **not** dump payloads in the
    response — returns counts and a small sample of keys for later PG ingest.
    """
    body = body or PullParcelProductsBody()
    storage = get_storage()
    prefix = parcel_product_prefix()
    limit = body.limit or 100

    if body.keys:
        keys = [k for k in body.keys if k]
        if limit:
            keys = keys[:limit]
    else:
        keys = storage.list_keys(prefix=prefix, suffix=".json", limit=limit)

    ok = 0
    failed = 0
    total_bytes = 0
    errors: list[dict[str, str]] = []
    sample_keys: list[str] = []

    for key in keys:
        if prefix and not key.startswith(prefix):
            failed += 1
            errors.append({"key": key, "error": "key outside parcel prefix"})
            continue
        try:
            data = storage.get_bytes(key)
            json.loads(data)
            ok += 1
            total_bytes += len(data)
            if len(sample_keys) < 20:
                sample_keys.append(key)
        except Exception as exc:  # noqa: BLE001 — summarize per-key failures
            failed += 1
            if len(errors) < 20:
                errors.append({"key": key, "error": str(exc)[:200]})

    logger.info(
        "parcel_products_pull",
        listed=len(keys),
        ok=ok,
        failed=failed,
        backend=storage.backend,
    )
    return {
        "backend": storage.backend,
        "bucket": storage.bucket,
        "prefix": prefix,
        "requested": len(keys),
        "ok": ok,
        "failed": failed,
        "total_bytes": total_bytes,
        "sample_keys": sample_keys,
        "errors": errors,
    }


@router.post("/storage/parcel-products/put")
async def put_parcel_product(
    request: Request, ctx: Annotated[OrgContext, Depends(_writer)]
):
    """Write a parcel-product object under the configured parcel prefix only.

    Accepts either:
    - JSON body: ``{ "key": "...", "content_base64": "..." }``
    - multipart form: ``key`` + ``file`` and/or ``content_base64``
    """
    content_type = (request.headers.get("content-type") or "").lower()
    object_key: str | None = None
    raw: bytes | None = None

    if "application/json" in content_type:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")
        object_key = payload.get("key")
        b64 = payload.get("content_base64")
        if not b64:
            raise HTTPException(status_code=400, detail="content_base64 is required")
        try:
            raw = base64.b64decode(b64)
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail=f"invalid content_base64: {exc}"
            ) from exc
    else:
        form = await request.form()
        object_key = form.get("key")
        if isinstance(object_key, UploadFile):
            object_key = None
        b64 = form.get("content_base64")
        if isinstance(b64, str) and b64:
            try:
                raw = base64.b64decode(b64)
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail=f"invalid content_base64: {exc}"
                ) from exc
        else:
            upload = form.get("file")
            if isinstance(upload, UploadFile):
                raw = await upload.read()

    if not object_key or not isinstance(object_key, str):
        raise HTTPException(status_code=400, detail="key is required")
    if raw is None:
        raise HTTPException(
            status_code=400, detail="file or content_base64 is required"
        )

    prefix = parcel_product_prefix()
    if not prefix or not object_key.startswith(prefix):
        raise HTTPException(
            status_code=400, detail=f"key must start with parcel prefix {prefix!r}"
        )

    result = put_bytes_via_storage(object_key, raw, content_type="application/json")
    logger.info(
        "parcel_product_put", key=object_key, bytes=len(raw), backend=result["backend"]
    )
    return {
        "ok": True,
        "key": object_key,
        "bytes": len(raw),
        "uri": result["uri"],
    }
