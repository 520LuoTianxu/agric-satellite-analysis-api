"""Redis scene-result cache tests using an in-memory async client stub."""

import asyncio
import unittest
from unittest.mock import patch

from app.services import scene_result_cache as cache


class _FakeRedis:
    def __init__(self) -> None:
        self.eval_args = None
        self.closed = False

    async def eval(self, *args):
        self.eval_args = args
        return 1

    async def aclose(self) -> None:
        self.closed = True


class SceneResultCacheTests(unittest.TestCase):
    def test_enqueue_uses_one_day_ttl_and_deduplicating_script(self) -> None:
        fake = _FakeRedis()
        with patch.object(cache.aioredis, "from_url", return_value=fake):
            result = asyncio.run(
                cache.enqueue_scene_result(
                    {
                        "status": "success",
                        "oss_urls": {"2026-09-16_S2": "https://oss.test/scene.json"},
                        "extras": {
                            "land_id": "L1",
                            "date": "2026-09-16",
                            "sensor": "S2",
                            "scene_id": "scene-1",
                        },
                    }
                )
            )

        self.assertTrue(result["queued"])
        self.assertEqual(result["ttl_seconds"], 86400)
        self.assertEqual(fake.eval_args[1], 2)
        self.assertEqual(fake.eval_args[5], str(cache.SCENE_RESULT_TTL_SECONDS))
        self.assertTrue(fake.closed)

    def test_scene_upsert_stats_must_confirm_database_write(self) -> None:
        self.assertTrue(cache._scene_upsert_succeeded({"scene_upserts": 1}))
        self.assertTrue(
            cache._scene_upsert_succeeded({"oss": {"scene_upserts": 1}})
        )
        self.assertFalse(cache._scene_upsert_succeeded({"oss": {"scene_upserts": 0}}))


if __name__ == "__main__":
    unittest.main()
