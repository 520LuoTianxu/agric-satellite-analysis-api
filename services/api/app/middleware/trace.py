"""ASGI middleware that binds X-Trace-Id for the duration of one request."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from agric_satellite_analysis_common.trace import (
    TRACE_HEADER,
    bind_trace_id,
    clear_trace_id,
    get_or_create_trace_id,
)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


def _header_value(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            try:
                return value.decode("latin-1")
            except Exception:
                return None
    return None


class TraceIdMiddleware:
    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header_value(scope, b"x-trace-id") or _header_value(
            scope, b"x-request-id"
        )
        bound = bind_trace_id(incoming) or get_or_create_trace_id()
        encoded = bound.encode("ascii")

        async def send_with_trace(message: MutableMapping[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(key.lower() == b"x-trace-id" for key, _ in headers):
                    headers.append((TRACE_HEADER.encode("ascii"), encoded))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            clear_trace_id()


__all__ = ["TRACE_HEADER", "TraceIdMiddleware"]
