"""Unit tests for Redis hot-path job progress (mocked redis)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _FakeRedis:
    """Minimal in-memory stand-in for redis.Redis hash + pipeline ops."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, str]] = {}
        self.ttls: dict[str, int] = {}

    def ping(self) -> bool:
        return True

    def hset(self, key, mapping=None, **kwargs):
        bucket = self.store.setdefault(key, {})
        if mapping:
            bucket.update({str(k): str(v) for k, v in mapping.items()})
        return 1

    def hgetall(self, key):
        return dict(self.store.get(key, {}))

    def hincrby(self, key, field, amount):
        bucket = self.store.setdefault(key, {})
        cur = int(bucket.get(field, "0"))
        cur += int(amount)
        bucket[field] = str(cur)
        return cur

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def delete(self, key):
        self.store.pop(key, None)
        return 1

    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, client: _FakeRedis) -> None:
        self.client = client
        self.ops: list = []

    def hincrby(self, key, field, amount):
        self.ops.append(("hincrby", key, field, amount))
        return self

    def hset(self, key, *args, **kwargs):
        # redis-py: hset(name, key, value) or hset(name, mapping=...)
        if args:
            field, value = args[0], args[1]
            self.ops.append(("hset", key, {field: value}))
        else:
            self.ops.append(("hset", key, kwargs.get("mapping") or {}))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        results = []
        for op in self.ops:
            if op[0] == "hincrby":
                results.append(self.client.hincrby(op[1], op[2], op[3]))
            elif op[0] == "hset":
                results.append(self.client.hset(op[1], mapping=op[2]))
            elif op[0] == "expire":
                results.append(self.client.expire(op[1], op[2]))
        self.ops.clear()
        return results


class JobProgressRedisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = _FakeRedis()
        # Import after path is ready (ingest tests run with services/ingest on path).
        from openfarm_common import job_progress_redis as jpr

        self.jpr = jpr
        jpr.reset_client_for_tests()
        self._patcher = patch.object(jpr, "_get_client", return_value=self.fake)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self.jpr.reset_client_for_tests()

    def test_set_total_and_read(self) -> None:
        ok = self.jpr.set_total("job-1", 10, workers=4)
        self.assertTrue(ok)
        snap = self.jpr.read_progress("job-1")
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap["total"], 10)
        self.assertEqual(snap["done"], 0)
        self.assertEqual(snap["workers"], 4)
        self.assertEqual(snap["current_step"], "process_scenes")

    def test_mark_and_incr(self) -> None:
        self.jpr.set_total("job-2", 3, workers=2)
        self.jpr.mark_scene_progress(
            "job-2",
            "download_bands",
            scene=1,
            total_scenes=3,
            scene_id="S2A",
        )
        done = self.jpr.incr_done("job-2")
        self.assertEqual(done, 1)
        snap = self.jpr.read_progress("job-2")
        assert snap is not None
        self.assertEqual(snap["done"], 1)
        self.assertEqual(snap["current_step"], "download_bands")
        self.assertEqual(snap["scene_id"], "S2A")

        self.jpr.incr_done("job-2", failed=True)
        snap = self.jpr.read_progress("job-2")
        assert snap is not None
        self.assertEqual(snap["done"], 2)
        self.assertEqual(snap["failed"], 1)

    def test_apply_and_merge(self) -> None:
        self.jpr.set_total("job-3", 5, workers=2)
        self.jpr.incr_done("job-3")
        base = {"current_step": "scene_search", "steps": {"scene_search": {"status": "completed"}}}
        merged = self.jpr.merge_progress_for_api("job-3", base)
        assert merged is not None
        self.assertEqual(merged["total_scenes"], 5)
        self.assertEqual(merged["scenes_done"], 1)
        self.assertEqual(merged["progress_source"], "redis")
        self.assertIn("process_scenes", merged["steps"])
        self.assertEqual(merged["steps"]["scene_search"]["status"], "completed")

    def test_flush_to_job(self) -> None:
        from app.core import job_progress_redis as ingest_jpr

        self.jpr.set_total("job-4", 8, workers=3)
        self.jpr.incr_done("job-4")
        job = SimpleNamespace(id="job-4", progress_json={"steps": {}})
        session = MagicMock()
        with patch.object(ingest_jpr, "flag_modified"):
            out = ingest_jpr.flush_to_job(session, job, current_step="process_scenes")
        self.assertEqual(out["total_scenes"], 8)
        self.assertEqual(out["scenes_done"], 1)
        session.commit.assert_called()
        self.assertEqual(job.progress_json["workers"], 3)

    def test_degrade_when_client_none(self) -> None:
        self._patcher.stop()
        with patch.object(self.jpr, "_get_client", return_value=None):
            self.assertFalse(self.jpr.set_total("x", 1))
            self.assertFalse(self.jpr.mark_scene_progress("x", "download_bands"))
            self.assertIsNone(self.jpr.incr_done("x"))
            self.assertIsNone(self.jpr.read_progress("x"))
            self.assertEqual(
                self.jpr.merge_progress_for_api("x", {"a": 1}),
                {"a": 1},
            )
        self._patcher = patch.object(self.jpr, "_get_client", return_value=self.fake)
        self._patcher.start()


if __name__ == "__main__":
    unittest.main()
