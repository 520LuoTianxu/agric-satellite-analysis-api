"""有界HTTP请求体读取工具。"""

from __future__ import annotations

import unicodedata

from fastapi import HTTPException, Request


async def read_limited_body(request: Request, max_bytes: int) -> bytes:
    """分块读取并校验请求体上限，不依赖调用方提供的Content-Length。"""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if declared_length > max_bytes:
            raise HTTPException(status_code=413, detail="request body is too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail="request body is too large")
        body.extend(chunk)
    return bytes(body)


def request_with_body(request: Request, body: bytes) -> Request:
    """把已限长缓存包装成一次性请求流，交由Starlette解析multipart内容。"""
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(request.scope, receive)


def safe_upload_filename(
    filename: str | None, fallback: str, *, max_bytes: int = 180
) -> str:
    """将multipart文件名压成安全短名称，避免路径注入和无界对象键/数据库值。"""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    source = filename or ""
    source = source[max(source.rfind("/"), source.rfind("\\")) + 1 :]
    # 超长目录名不参与后续逐字符清理；只保留足够截成目标字节数的末尾字符。
    source = source[-max_bytes:]
    invalid_chars = set('/\\?#%<>:"|*')
    cleaned = "".join(
        "_"
        if char in invalid_chars or unicodedata.category(char).startswith("C")
        else char
        for char in source
    ).strip()
    # Multipart头部可带孤立代理项；替换后再按UTF-8字节数截断，确保可入库和可存储。
    encoded = cleaned.encode("utf-8", errors="replace")[:max_bytes]
    while encoded:
        try:
            cleaned = encoded.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            encoded = encoded[: exc.start]
    else:
        cleaned = ""
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned
