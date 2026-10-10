"""暂停领取期间的存活心跳：优先专用端点，旧 API 回退为零匹配 claim。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from app import work_agent as wa


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    if status >= 400:
        r.raise_for_status.side_effect = RuntimeError(f"http {status}")
    return r


class WorkerHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        wa._heartbeat_endpoint_missing = False
        self.patches = [
            patch.object(wa, "queue_depths", return_value={"decloud": 400}),
            patch.object(wa, "claim_types", return_value=["satellite_batch"]),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        wa._heartbeat_endpoint_missing = False

    def test_uses_dedicated_endpoint(self) -> None:
        client = MagicMock()
        client.post.return_value = _resp(200, {"ok": True})
        self.assertEqual(wa.worker_heartbeat(client), "endpoint")
        path, kw = client.post.call_args.args[0], client.post.call_args.kwargs
        self.assertEqual(path, "/v1/internal/work/worker-heartbeat")
        self.assertEqual(kw["json"]["pending_queue_count"], 400)

    def test_falls_back_to_zero_match_claim_on_404_and_remembers(self) -> None:
        client = MagicMock()
        client.post.side_effect = [
            _resp(404),
            _resp(200, {"items": []}),
            _resp(200, {"items": []}),
        ]
        self.assertEqual(wa.worker_heartbeat(client), "claim_fallback")
        self.assertEqual(wa.worker_heartbeat(client), "claim_fallback")
        calls = client.post.call_args_list
        self.assertEqual(calls[1].args[0], "/v1/internal/work/claim")
        self.assertEqual(calls[1].kwargs["json"]["types"], [wa.HEARTBEAT_ONLY_TYPE])
        self.assertEqual(calls[2].args[0], "/v1/internal/work/claim")  # 不再探测端点
        self.assertEqual(len(calls), 3)

    def test_fallback_refuses_unexpected_items(self) -> None:
        client = MagicMock()
        client.post.side_effect = [_resp(404), _resp(200, {"items": [{"id": "x"}]})]
        with self.assertRaises(RuntimeError):
            wa.worker_heartbeat(client)

    def test_endpoint_errors_propagate(self) -> None:
        client = MagicMock()
        client.post.return_value = _resp(500)
        with self.assertRaises(RuntimeError):
            wa.worker_heartbeat(client)

    def test_interval_env(self) -> None:
        with patch.dict(os.environ, {"WORK_CLAIM_PAUSED_HEARTBEAT_SEC": "10"}):
            self.assertEqual(wa.paused_heartbeat_interval_sec(), 10.0)
        with patch.dict(os.environ, {"WORK_CLAIM_PAUSED_HEARTBEAT_SEC": "x"}):
            self.assertEqual(wa.paused_heartbeat_interval_sec(), 30.0)


class LoopHeartbeatTests(unittest.TestCase):
    def test_paused_loop_sends_throttled_heartbeats_and_never_claims(self) -> None:
        ticks = iter([0.0, 5.0, 31.0, 40.0])
        rounds = {"n": 0}

        def paused():
            rounds["n"] += 1
            if rounds["n"] > 4:
                raise KeyboardInterrupt
            return True, 410, 100

        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        with (
            patch.object(wa, "should_run_claim_agent", return_value=True),
            patch.object(wa, "claim_paused_for_backlog", side_effect=paused),
            patch.object(wa, "claim_batch") as claim,
            patch.object(wa, "worker_heartbeat", return_value="endpoint") as hb,
            patch.object(wa, "_client", return_value=client),
            patch.object(wa, "claim_types", return_value=["satellite_batch"]),
            patch.object(wa.time, "sleep"),
            patch.object(wa.time, "monotonic", side_effect=lambda: next(ticks)),
            patch.dict(os.environ, {"WORK_CLAIM_PAUSED_HEARTBEAT_SEC": "30"}),
        ):
            with self.assertRaises(KeyboardInterrupt):
                wa.run_forever()
        claim.assert_not_called()
        self.assertEqual(hb.call_count, 2)  # t=0 与 t=31


if __name__ == "__main__":
    unittest.main()
