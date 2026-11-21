"""Redis helpers stay on RESP2 and never let a URL select HELLO/RESP3."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.rate_limit import limiter
from app.core.redis_client import (
    REDIS_RESP2_PROTOCOL,
    create_async_redis_client,
    create_sync_redis_client,
)

FROM_URL = "agric_satellite_analysis_common.redis_compat.aioredis.from_url"
SYNC_FROM_URL = "agric_satellite_analysis_common.redis_compat.redis.Redis.from_url"


class CreateRedisClientTests(unittest.TestCase):
    def test_async_from_url_is_called_with_protocol_2(self) -> None:
        sentinel = object()
        with patch(FROM_URL, return_value=sentinel) as from_url:
            client = create_async_redis_client(
                "redis://localhost:6379/0",
                decode_responses=True,
                socket_timeout=5.0,
            )
        self.assertIs(client, sentinel)
        from_url.assert_called_once()
        args, kwargs = from_url.call_args
        self.assertEqual(args, ("redis://localhost:6379/0",))
        self.assertEqual(kwargs["protocol"], REDIS_RESP2_PROTOCOL)
        self.assertEqual(kwargs["protocol"], 2)
        self.assertTrue(kwargs["decode_responses"])
        self.assertEqual(kwargs["socket_timeout"], 5.0)

    def test_sync_from_url_is_called_with_protocol_2(self) -> None:
        sentinel = object()
        with patch(SYNC_FROM_URL, return_value=sentinel) as from_url:
            client = create_sync_redis_client(
                "redis://localhost:6379/0",
                decode_responses=True,
            )
        self.assertIs(client, sentinel)
        args, kwargs = from_url.call_args
        self.assertEqual(args, ("redis://localhost:6379/0",))
        self.assertEqual(kwargs["protocol"], 2)
        self.assertTrue(kwargs["decode_responses"])

    def test_explicit_protocol_and_url_query_cannot_select_resp3(self) -> None:
        with patch(FROM_URL) as from_url:
            create_async_redis_client(
                "redis://localhost:6379/1?protocol=3",
                protocol=3,
            )
        args, kwargs = from_url.call_args
        self.assertEqual(kwargs["protocol"], 2)
        self.assertNotIn("protocol=3", args[0])
        self.assertNotIn("protocol=", args[0])
        with patch(SYNC_FROM_URL) as sync_from_url:
            create_sync_redis_client("redis://localhost:6379/1?protocol=3", protocol=3)
        args, kwargs = sync_from_url.call_args
        self.assertEqual(kwargs["protocol"], 2)
        self.assertNotIn("protocol=", args[0])

    def test_limiter_storage_uses_resp2(self) -> None:
        self.assertEqual(limiter._storage_options.get("protocol"), 2)


class ApiRedisCallSiteTests(unittest.TestCase):
    def test_lifespan_health_and_cache_use_the_shared_helper(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        main = (root / "app" / "main.py").read_text(encoding="utf-8")
        cache = (root / "app" / "services" / "scene_result_cache.py").read_text(
            encoding="utf-8"
        )
        repo = Path(__file__).resolve().parents[3]
        common = repo / "packages" / "agric_satellite_analysis_common" / "agric_satellite_analysis_common"
        progress = (common / "job_progress_redis.py").read_text(encoding="utf-8")
        weather = (common / "weather_daily_limit.py").read_text(encoding="utf-8")
        self.assertIn("create_async_redis_client", main)
        self.assertIn("create_async_redis_client", cache)
        self.assertNotIn("aioredis.from_url", main)
        self.assertNotIn("aioredis.from_url", cache)
        self.assertEqual(cache.count("create_async_redis_client("), 2)
        self.assertIn("create_sync_redis_client", progress)
        self.assertIn("create_sync_redis_client", weather)
        self.assertNotIn("redis.Redis.from_url", progress)
        self.assertNotIn("redis.Redis.from_url", weather)
