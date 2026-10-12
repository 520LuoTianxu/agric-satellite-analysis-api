"""轻量 /health：k8s/istio 的 livez、readyz 都打主端口 GET /health。

- 不访问 DB/Redis，只读看门狗记录的事件循环卡顿秒数（仅展示）；
- 卡顿超过 LIVEZ_MAX_LOOP_STALL_SECONDS（默认 180s）才返回 503；
- ``HealthShortCircuitMiddleware`` 作为 ASGI 最外层，在进入 CORS/限流/Trace
  中间件、路由和依赖注入之前直接应答，尽量缩短事件循环上的处理路径。

局限（如实说明）：HTTP 连接的接收与响应仍由 uvicorn 的事件循环完成。若事件循环
被同步代码完全卡死，本接口同样无法应答；它只能避免“循环很忙/连接池耗尽/DB 慢”
时探针被连带拖死。
"""

from __future__ import annotations

import json
from typing import Any

HEALTH_PATHS = frozenset({"/health", "/healthz"})


def light_health() -> tuple[int, dict[str, Any]]:
    from app.core.loop_watchdog import watchdog

    stall = round(watchdog.stall_seconds(), 1)
    limit = watchdog.livez_max_stall_seconds
    ok = stall < limit
    return (200 if ok else 503), {
        "status": "ok" if ok else "stalled",
        "loop_stall_seconds": stall,
        "loop_stall_limit_seconds": limit,
        "deep_check": "/health/deep",
    }


class HealthShortCircuitMiddleware:
    """纯 ASGI 中间件：GET/HEAD /health、/healthz 直接返回，不进入路由。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("method") in ("GET", "HEAD")
            and scope.get("path", "").rstrip("/") in HEALTH_PATHS
        ):
            status, payload = light_health()
            body = json.dumps(payload).encode()
            await send({
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b"" if scope.get("method") == "HEAD" else body,
            })
            return
        await self.app(scope, receive, send)
