"""/health 轻量探针：不访问 DB/Redis，卡顿超限才 503，深度检查在 /health/deep。"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

from app.core.health_light import HealthShortCircuitMiddleware, light_health
from app.core.loop_watchdog import watchdog


async def _call(app, path, method="GET"):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app({"type": "http", "method": method, "path": path, "headers": []}, receive, send)
    return sent


class HealthLightTests(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_short_circuits_without_inner_app(self):
        called = []

        async def inner(scope, receive, send):
            called.append(scope["path"])

        mw = HealthShortCircuitMiddleware(inner)
        watchdog.last_beat = time.monotonic()
        for path in ("/health", "/healthz", "/health/"):
            sent = await _call(mw, path)
            self.assertEqual(sent[0]["status"], 200)
            self.assertIn(b"loop_stall_seconds", sent[1]["body"])
        self.assertEqual(called, [])
        await _call(mw, "/health/deep")
        await _call(mw, "/v1/x")
        self.assertEqual(called, ["/health/deep", "/v1/x"])

    def test_stall_only_fails_beyond_limit(self):
        with patch.object(watchdog, "livez_max_stall_seconds", 180.0):
            watchdog.last_beat = time.monotonic() - 60
            status, body = light_health()
            self.assertEqual(status, 200)
            self.assertGreaterEqual(body["loop_stall_seconds"], 60)
            watchdog.last_beat = time.monotonic() - 200
            self.assertEqual(light_health()[0], 503)
        watchdog.last_beat = time.monotonic()

    def test_app_health_does_not_touch_db_or_redis(self):
        from fastapi.testclient import TestClient

        from app import main

        watchdog.last_beat = time.monotonic()
        import app.core.database as dbmod

        class _NoEngine:
            def connect(self):
                raise AssertionError("db")

        with patch.object(dbmod, "engine", _NoEngine()), \
             patch.object(main.aioredis, "from_url", side_effect=AssertionError("redis")):
            client = TestClient(main.app)  # 不进入 lifespan
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "ok")
            self.assertEqual(client.get("/healthz").status_code, 200)
            deep_routes = [getattr(rt, "path", "") for rt in main.app.routes]
            self.assertIn("/health/deep", deep_routes)
