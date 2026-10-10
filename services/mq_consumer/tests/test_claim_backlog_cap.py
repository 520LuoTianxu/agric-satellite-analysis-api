"""本机积压上限：WORK_CLAIM_MAX_LOCAL_BACKLOG 达到后暂停领取，低于后恢复。"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from app import work_agent as wa


class _FakeRedis:
    def __init__(self, lists, unacked):
        self.lists = lists
        self.unacked = unacked

    def scan_iter(self, match, count=100):
        prefix = match.rstrip("*")
        return [k.encode() for k in self.lists if k.startswith(prefix) and k != prefix]

    def llen(self, key):
        return self.lists.get(key, 0)

    def hlen(self, key):
        assert key == "unacked"
        return self.unacked

    def close(self):
        pass


def _redis_module(fake):
    mod = types.ModuleType("redis")
    mod.Redis = MagicMock()
    mod.Redis.from_url.return_value = fake
    return mod


class MaxLocalBacklogEnvTests(unittest.TestCase):
    def test_default_and_parsing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORK_CLAIM_MAX_LOCAL_BACKLOG", None)
            self.assertEqual(wa.max_local_backlog(), 100)
        for raw, want in (("250", 250), ("0", 0), ("-5", 0), ("abc", 100)):
            with patch.dict(os.environ, {"WORK_CLAIM_MAX_LOCAL_BACKLOG": raw}):
                self.assertEqual(wa.max_local_backlog(), want)


class LocalBacklogTests(unittest.TestCase):
    def test_counts_ready_priority_subqueues_and_unacked(self):
        fake = _FakeRedis(
            {
                "satellite_download": 30,
                "satellite_download\x06\x163": 5,
                "cpu_compute": 2,
                "other_queue": 999,  # 未配置的队列不计入
            },
            unacked=7,
        )
        with (
            patch.dict(sys.modules, {"redis": _redis_module(fake)}),
            patch.dict(
                os.environ, {"CELERY_QUEUE_NAMES": "satellite_download,cpu_compute"}
            ),
        ):
            self.assertEqual(wa.local_backlog(), 30 + 5 + 2 + 7)

    def test_probe_failure_returns_none(self):
        mod = types.ModuleType("redis")
        mod.Redis = MagicMock()
        mod.Redis.from_url.side_effect = OSError("down")
        with patch.dict(sys.modules, {"redis": mod}):
            self.assertIsNone(wa.local_backlog())


class PausedDecisionTests(unittest.TestCase):
    def test_pause_at_or_above_cap(self):
        with patch.dict(os.environ, {"WORK_CLAIM_MAX_LOCAL_BACKLOG": "100"}):
            with patch.object(wa, "local_backlog", return_value=100):
                self.assertEqual(wa.claim_paused_for_backlog(), (True, 100, 100))
            with patch.object(wa, "local_backlog", return_value=99):
                self.assertEqual(wa.claim_paused_for_backlog(), (False, 99, 100))
            # 探测失败：不限流，保持原有领取行为
            with patch.object(wa, "local_backlog", return_value=None):
                self.assertEqual(wa.claim_paused_for_backlog(), (False, None, 100))

    def test_zero_disables_without_probing(self):
        probe = MagicMock()
        with (
            patch.dict(os.environ, {"WORK_CLAIM_MAX_LOCAL_BACKLOG": "0"}),
            patch.object(wa, "local_backlog", probe),
        ):
            self.assertEqual(wa.claim_paused_for_backlog(), (False, None, 0))
        probe.assert_not_called()


class LoopTests(unittest.TestCase):
    def test_loop_skips_claim_while_paused_then_resumes(self):
        decisions = iter([(True, 150, 100), (True, 120, 100), (False, 80, 100)])
        claims = MagicMock(side_effect=[[], KeyboardInterrupt()])
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        with (
            patch.object(
                wa,
                "claim_paused_for_backlog",
                side_effect=lambda: next(decisions, (False, 10, 100)),
            ),
            patch.object(wa, "claim_batch", claims),
            patch.object(wa, "_client", return_value=client),
            patch.object(wa.time, "sleep"),
            patch.object(wa, "claim_types", return_value=["satellite_batch"]),
            patch.object(wa, "should_run_claim_agent", return_value=True),
        ):
            with self.assertRaises(KeyboardInterrupt):
                wa.run_forever()
        self.assertEqual(claims.call_count, 2)  # 两次暂停期间未领取


if __name__ == "__main__":
    unittest.main()
