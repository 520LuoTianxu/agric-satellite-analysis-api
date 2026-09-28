"""Object storage admin / parcel-product APIs (Aliyun OSS)."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.concurrency import run_in_threadpool

from app.core.logging import logger
from app.core.request_body import read_limited_body, request_with_body
from app.core.storage import (
    ObjectTooLargeError,
    get_storage,
    parcel_product_prefix,
)
from app.tasks.storage_tasks import put_bytes_via_storage
from app.middleware.auth import OrgContext, require_roles

router = APIRouter()

_reader = require_roles("owner", "admin", "member", "viewer")
_writer = require_roles("owner", "admin", "member")

_WRITABLE_STORAGE_PREFIXES = ("cogs/default/",)
# API直传仅承接有界中小对象；大栅格应由已有的存储任务/文件上传链路处理。
_MAX_DIRECT_PUT_BYTES = 64 * 1024 * 1024
_MAX_PARCEL_PRODUCT_BYTES = 8 * 1024 * 1024
_MAX_PARCEL_PRODUCT_REQUEST_BYTES = 12 * 1024 * 1024
_MAX_PULL_OBJECT_BYTES = 8 * 1024 * 1024
_MAX_PULL_PAGE_BYTES = 64 * 1024 * 1024
_MAX_PULL_REQUEST_BYTES = 8 * 1024 * 1024


def _safe_storage_path(path: str, *, allow_trailing_slash: bool = False) -> bool:
    """拒绝路径穿越、反斜杠和控制字符，避免对象键绕过命名空间边界。"""
    if not path or path.startswith("/") or "\\" in path:
        return False
    try:
        if len(path.encode("utf-8")) > 1023:
            return False
    except UnicodeEncodeError:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return False
    parts = path.split("/")
    if allow_trailing_slash and parts[-1] == "":
        parts = parts[:-1]
    return bool(parts) and all(part and part not in {".", ".."} for part in parts)


def _writable_storage_prefixes() -> tuple[str, ...]:
    """只开放当前匿名部署实际使用的对象写入命名空间。"""
    prefixes = [
        prefix
        for prefix in _WRITABLE_STORAGE_PREFIXES
        if _safe_storage_path(prefix, allow_trailing_slash=True)
    ]
    parcel = parcel_product_prefix()
    if parcel and _safe_storage_path(parcel, allow_trailing_slash=True):
        prefixes.append(parcel)
    return tuple(dict.fromkeys(prefixes))


def _readable_storage_prefixes() -> tuple[str, ...]:
    """通用读取不暴露照片；照片继续由既有公开媒体URL按需展示。"""
    prefixes = ["cogs/default/"]
    parcel = parcel_product_prefix()
    if parcel and _safe_storage_path(parcel, allow_trailing_slash=True):
        prefixes.append(parcel)
    return tuple(dict.fromkeys(prefixes))


def _allowed_key(key: str) -> bool:
    if not _safe_storage_path(key):
        return False
    return any(key.startswith(prefix) for prefix in _writable_storage_prefixes())


def _allowed_read_key(key: str) -> bool:
    if not _safe_storage_path(key):
        return False
    return any(key.startswith(prefix) for prefix in _readable_storage_prefixes())


def _allowed_list_prefix(prefix: str) -> bool:
    if not _safe_storage_path(prefix, allow_trailing_slash=True):
        return False
    # 列表前缀必须落在当前命名空间内，不能从父目录枚举旧组织或其它业务对象。
    return any(prefix.startswith(allowed) for allowed in _readable_storage_prefixes())


class PullParcelProductsBody(BaseModel):
    keys: list[str] = Field(default_factory=list, max_length=5000)
    limit: int | None = Field(default=None, ge=1, le=5000)

    @field_validator("keys")
    @classmethod
    def _bound_key_lengths(cls, keys: list[str]) -> list[str]:
        if any(len(key) > 1024 for key in keys):
            raise ValueError("each key must be at most 1024 characters")
        return keys


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
    limit: int = Query(default=100, ge=1, le=5000),
):
    """List object keys under a prefix."""
    storage = get_storage()
    if prefix is None:
        prefix = parcel_product_prefix() or "cogs/default/"
    if prefix in {allowed.rstrip("/") for allowed in _readable_storage_prefixes()}:
        prefix = f"{prefix}/"
    if not _allowed_list_prefix(prefix):
        raise HTTPException(status_code=400, detail="prefix is outside readable storage namespaces")
    if len(suffix) > 32 or any(ord(char) < 32 or ord(char) == 127 for char in suffix):
        raise HTTPException(status_code=400, detail="suffix is invalid")
    listed_keys = await run_in_threadpool(
        storage.list_keys, prefix=prefix, suffix=suffix, limit=limit + 1
    )
    truncated = len(listed_keys) > limit
    keys = listed_keys[:limit]
    return {
        "backend": storage.backend,
        "bucket": storage.bucket,
        "prefix": prefix,
        "suffix": suffix,
        "count": len(keys),
        "truncated": truncated,
        "keys": keys,
    }


@router.get("/storage/object")
async def get_storage_object(
    ctx: Annotated[OrgContext, Depends(_reader)],
    key: str = Query(...),
    download: int = Query(default=0),
):
    """Return object metadata, optionally streaming the body as an attachment."""
    if not _allowed_read_key(key):
        # 隐藏不在公开命名空间内的对象是否存在，避免通用接口成为任意键探测器。
        raise HTTPException(status_code=404, detail="Object not found")
    storage = get_storage()
    if not await run_in_threadpool(storage.exists, key):
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

    filename = key.rsplit("/", 1)[-1] or "download"
    return StreamingResponse(
        storage.iter_bytes(key),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f'attachment; filename="download"; filename*=UTF-8\'\'{quote(filename)}'
            ),
            "X-Storage-Key": quote(key, safe="/"),
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
                "key must be under cogs/default/ or the configured parcel "
                f"prefix ({parcel_product_prefix()!r})"
            ),
        )
    body = await read_limited_body(request, _MAX_DIRECT_PUT_BYTES)
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    content_type = request.headers.get("content-type") or "application/octet-stream"
    result = await run_in_threadpool(
        put_bytes_via_storage, key, body, content_type=content_type
    )
    logger.info(
        "storage_put_object", key=key, bytes=len(body), backend=result["backend"]
    )
    return {
        "ok": True,
        "key": key,
        "bytes": len(body),
        "uri": result["uri"],
    }


@router.post(
    "/storage/parcel-products/pull",
    openapi_extra={
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "keys": {
                                "type": "array",
                                "items": {"type": "string", "maxLength": 1024},
                                "maxItems": 5000,
                                "default": [],
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 5000,
                            },
                        },
                    }
                }
            },
        }
    },
)
async def pull_parcel_products(
    ctx: Annotated[OrgContext, Depends(_reader)],
    request: Request,
):
    """Pull parcel-product JSON from the fixed OSS prefix (summary only).

    Fetches each object via get_bytes but does **not** dump payloads in the
    response — returns counts and a small sample of keys for later PG ingest.
    """
    raw_body = await read_limited_body(request, _MAX_PULL_REQUEST_BYTES)
    if raw_body:
        try:
            body = PullParcelProductsBody.model_validate_json(raw_body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="invalid parcel-product request") from exc
    else:
        body = PullParcelProductsBody()
    return await run_in_threadpool(_pull_parcel_products, body)


def _pull_parcel_products(body: PullParcelProductsBody) -> dict:
    """在线程池内有界读取产品JSON，避免OSS等待和解析阻塞API事件循环。"""
    storage = get_storage()
    prefix = parcel_product_prefix()
    if not prefix:
        raise HTTPException(status_code=503, detail="parcel product prefix is not configured")
    limit = body.limit or 100

    if body.keys:
        keys = [k for k in body.keys if k]
        truncated = len(keys) > limit
        if limit:
            keys = keys[:limit]
    else:
        listed_keys = storage.list_keys(
            prefix=prefix, suffix=".json", limit=limit + 1
        )
        truncated = len(listed_keys) > limit
        keys = listed_keys[:limit]

    ok = 0
    failed = 0
    total_bytes = 0
    read_bytes = 0
    processed = 0
    errors: list[dict[str, str]] = []
    sample_keys: list[str] = []

    for index, key in enumerate(keys):
        remaining = _MAX_PULL_PAGE_BYTES - read_bytes
        if remaining <= 0:
            truncated = truncated or index < len(keys)
            break
        processed += 1
        if not _safe_storage_path(key) or not key.startswith(prefix):
            failed += 1
            if len(errors) < 20:
                errors.append({"key": key[:1024], "error": "key outside parcel prefix"})
            continue
        read_limit = min(_MAX_PULL_OBJECT_BYTES, remaining)
        try:
            data = storage.get_bytes(key, max_bytes=read_limit)
        except ObjectTooLargeError:
            # 超大对象最多消耗当前单对象额度加1字节；累计预算不足时停止后续读取。
            read_bytes += read_limit + 1
            failed += 1
            if len(errors) < 20:
                errors.append({"key": key, "error": "object exceeds read budget"})
            if read_limit < _MAX_PULL_OBJECT_BYTES:
                truncated = True
                break
            continue
        except Exception as exc:  # noqa: BLE001 — summarize per-key storage failures
            # 读取异常时按本次上限计入预算，防止反复失败的对象绕过页面流量上限。
            read_bytes += read_limit
            failed += 1
            if len(errors) < 20:
                errors.append({"key": key, "error": "object read failed"})
            logger.warning("parcel_product_pull_item_failed", key=key, error=str(exc)[:200])
            continue

        read_bytes += len(data)
        try:
            json.loads(data)
        except (ValueError, RecursionError):
            failed += 1
            if len(errors) < 20:
                errors.append({"key": key, "error": "invalid JSON"})
            continue
        ok += 1
        total_bytes += len(data)
        if len(sample_keys) < 20:
            sample_keys.append(key)

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
        "processed": processed,
        "ok": ok,
        "failed": failed,
        "total_bytes": total_bytes,
        "read_bytes": read_bytes,
        "truncated": truncated,
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
    request_body = await read_limited_body(
        request, _MAX_PARCEL_PRODUCT_REQUEST_BYTES
    )

    if "application/json" in content_type:
        try:
            payload = json.loads(request_body)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")
        object_key = payload.get("key")
        b64 = payload.get("content_base64")
        if not b64:
            raise HTTPException(status_code=400, detail="content_base64 is required")
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="invalid content_base64"
            ) from exc
    else:
        form = await request_with_body(request, request_body).form(
            max_files=1,
            max_fields=3,
            max_part_size=_MAX_PARCEL_PRODUCT_REQUEST_BYTES,
        )
        try:
            object_key = form.get("key")
            if isinstance(object_key, StarletteUploadFile):
                object_key = None
            b64 = form.get("content_base64")
            if isinstance(b64, str) and b64:
                try:
                    raw = base64.b64decode(b64, validate=True)
                except (binascii.Error, TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400, detail="invalid content_base64"
                    ) from exc
            else:
                upload = form.get("file")
                if isinstance(upload, StarletteUploadFile):
                    raw = await upload.read(_MAX_PARCEL_PRODUCT_BYTES + 1)
        finally:
            await form.close()

    if not object_key or not isinstance(object_key, str):
        raise HTTPException(status_code=400, detail="key is required")
    if raw is None:
        raise HTTPException(
            status_code=400, detail="file or content_base64 is required"
        )
    if len(raw) > _MAX_PARCEL_PRODUCT_BYTES:
        raise HTTPException(status_code=413, detail="parcel product exceeds the 8 MiB limit")

    prefix = parcel_product_prefix()
    if not prefix or not _safe_storage_path(object_key) or not object_key.startswith(prefix):
        raise HTTPException(
            status_code=400, detail=f"key must start with parcel prefix {prefix!r}"
        )

    result = await run_in_threadpool(
        put_bytes_via_storage,
        object_key,
        raw,
        content_type="application/json",
    )
    logger.info(
        "parcel_product_put", key=object_key, bytes=len(raw), backend=result["backend"]
    )
    return {
        "ok": True,
        "key": object_key,
        "bytes": len(raw),
        "uri": result["uri"],
    }
