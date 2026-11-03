"""Claim 环：Internal API 瞬时错误重试 + 看门狗重建客户端。"""

from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import MagicMock, patch

import httpx

from app import work_agent as wa


def _resp(status=200, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    if status >= 400 and status not in (502, 503, 504):
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"http {status}",
            request=MagicMock(),
            response=r,
        )
    else:
        r.raise_for_status = MagicMock()
    return r


class TimeoutConfigTests(unittest.TestCase):
    def test_connect_timeout_defaults_shorter_than_total(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORK_CLAIM_HTTP_TIMEOUT_SEC", None)
            os.environ.pop("WORK_CLAIM_HTTP_CONNECT_TIMEOUT_SEC", None)
            t = wa.api_http_timeout()
        self.assertEqual(t.read, 20.0)
        self.assertEqual(t.connect, 5.0)
        self.assertEqual(t.pool, 5.0)

    def test_connect_timeout_env_and_clamp(self) -> None:
        with patch.dict(
            os.environ,
            {
                "WORK_CLAIM_HTTP_TIMEOUT_SEC": "8",
                "WORK_CLAIM_HTTP_CONNECT_TIMEOUT_SEC": "30",
            },
        ):
            t = wa.api_http_timeout()
        self.assertEqual(t.read, 8.0)
        self.assertEqual(t.connect, 8.0)  # clamped to total

    def test_watchdog_env(self) -> None:
        with patch.dict(os.environ, {"WORK_CLAIM_WATCHDOG_SEC": "120"}):
            self.assertEqual(wa.claim_watchdog_sec(), 120.0)
        with patch.dict(os.environ, {"WORK_CLAIM_WATCHDOG_SEC": "0"}):
            self.assertEqual(wa.claim_watchdog_sec(), 0.0)
        with patch.dict(os.environ, {"WORK_CLAIM_WATCHDOG_SEC": "5"}):
            self.assertEqual(wa.claim_watchdog_sec(), 15.0)  # floor
        with patch.dict(os.environ, {"WORK_CLAIM_WATCHDOG_SEC": "x"}):
            self.assertEqual(wa.claim_watchdog_sec(), 90.0)


class RetryHelperTests(unittest.TestCase):
    def test_retries_connect_timeout_then_succeeds(self) -> None:
        client = MagicMock()
        ok = _resp(200, {"items": []})
        client.post.side_effect = [
            httpx.ConnectTimeout("tls handshake"),
            httpx.ReadTimeout("read"),
            ok,
        ]
        sleeps: list[float] = []
        out = wa.request_with_retry(
            client,
            "POST",
            "/internal/work/claim",
            retries=3,
            backoff=0.5,
            sleep=sleeps.append,
            json={"x": 1},
        )
        self.assertIs(out, ok)
        self.assertEqual(client.post.call_count, 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_retries_503_then_succeeds(self) -> None:
        client = MagicMock()
        client.post.side_effect = [_resp(503), _resp(200, {"ok": True})]
        sleeps: list[float] = []
        out = wa.request_with_retry(
            client,
            "POST",
            "/x",
            retries=2,
            backoff=0.25,
            sleep=sleeps.append,
        )
        self.assertEqual(out.status_code, 200)
        self.assertEqual(sleeps, [0.25])

    def test_non_transient_does_not_retry(self) -> None:
        client = MagicMock()
        client.post.side_effect = ValueError("boom")
        with self.assertRaises(ValueError):
            wa.request_with_retry(
                client, "POST", "/x", retries=5, backoff=0.1, sleep=lambda _s: None
            )
        self.assertEqual(client.post.call_count, 1)

    def test_is_transient_http_error(self) -> None:
        self.assertTrue(wa.is_transient_http_error(httpx.ConnectTimeout("x")))
        self.assertTrue(wa.is_transient_http_error(httpx.RemoteProtocolError("eof")))
        resp = MagicMock(status_code=503)
        err = httpx.HTTPStatusError("503", request=MagicMock(), response=resp)
        self.assertTrue(wa.is_transient_http_error(err))
        resp400 = MagicMock(status_code=400)
        err400 = httpx.HTTPStatusError("400", request=MagicMock(), response=resp400)
        self.assertFalse(wa.is_transient_http_error(err400))
        self.assertFalse(wa.is_transient_http_error(RuntimeError("x")))


class WatchdogLoopTests(unittest.TestCase):
    def test_watchdog_closes_stale_client_and_loop_continues(self) -> None:
        """卡在 claim_batch 时看门狗 close 客户端，主环换新客户端继续。"""
        clients: list[MagicMock] = []
        hung_entered = threading.Event()
        allow_second = threading.Event()

        def make_client():
            c = MagicMock()
            c.is_closed = False

            def close():
                c.is_closed = True

            c.close.side_effect = close
            clients.append(c)
            return c

        claim_calls = {"n": 0}

        def claim_side_effect(client, **_kwargs):
            claim_calls["n"] += 1
            if claim_calls["n"] == 1:
                hung_entered.set()
                # 真实等待看门狗关闭当前客户端（不用假时钟，避免与看门狗线程打架）
                if not hung_entered.wait(0):
                    pass
                deadline = wa.time.monotonic() + 3.0
                while wa.time.monotonic() < deadline:
                    if getattr(client, "is_closed", False):
                        raise httpx.RemoteProtocolError("client closed by watchdog")
                    wa.time.sleep(0.02)
                raise TimeoutError("claim hung without watchdog")
            if not allow_second.wait(2.0):
                raise TimeoutError("second claim not released")
            raise KeyboardInterrupt

        # 真实时间；看门狗阈值压到下限 15s 太慢，改为直接 patch 函数返回 0.3s
        with (
            patch.object(wa, "should_run_claim_agent", return_value=True),
            patch.object(wa, "claim_paused_for_backlog", return_value=(False, 0, 100)),
            patch.object(wa, "reports_after_base_enabled", return_value=False),
            patch.object(wa, "claim_batch", side_effect=claim_side_effect),
            patch.object(wa, "_client", side_effect=make_client),
            patch.object(wa, "claim_types", return_value=["satellite_batch"]),
            patch.object(wa, "claim_watchdog_sec", return_value=0.3),
            patch.object(wa, "claim_interval_sec", return_value=0.05),
        ):
            # claim_watchdog_sec floor is 15 when read via env; we patched the function
            # but run_forever calls claim_watchdog_sec() once — patch covers that.
            # Override floor by patching the local usage: function returns 0.3, but
            # implementation does max(15, raw) only inside claim_watchdog_sec when
            # reading env. Patched return_value=0.3 bypasses floor. Good.
            def run():
                with self.assertRaises(KeyboardInterrupt):
                    wa.run_forever()

            t = threading.Thread(target=run)
            t.start()
            self.assertTrue(hung_entered.wait(2.0), "claim never hung")
            # 等看门狗触发后放行第二次 claim
            deadline = wa.time.monotonic() + 3.0
            while wa.time.monotonic() < deadline and len(clients) < 2:
                wa.time.sleep(0.05)
            allow_second.set()
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive())

        self.assertGreaterEqual(len(clients), 2)
        self.assertTrue(clients[0].is_closed)
        self.assertGreaterEqual(claim_calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
