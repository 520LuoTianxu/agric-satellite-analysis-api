"""事件循环看门狗、claim 并发限制与 task-run 状态幂等回报。"""

from __future__ import annotations

import asyncio
import time
import unittest
import urllib.error
import urllib.request
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.core.loop_watchdog import LoopWatchdog


class LoopWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_thread_health_server_reports_loop_stall(self):
        dog = LoopWatchdog(interval=0.05, stall_log_seconds=0.2,
                           livez_max_stall_seconds=0.5, readyz_max_stall_seconds=0.2)
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        dog.start(port=port)
        try:
            await asyncio.sleep(0.1)

            def get(path):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code

            self.assertEqual(await asyncio.to_thread(get, "/livez"), 200)
            # 同步阻塞事件循环：独立线程端口仍能应答，并如实报告卡顿。
            time.sleep(0.8)
            self.assertEqual(get("/readyz"), 503)
            self.assertEqual(get("/livez"), 503)
            self.assertIn("test_thread_health_server_reports_loop_stall", dog.loop_stack())
            await asyncio.sleep(0.2)
            self.assertEqual(await asyncio.to_thread(get, "/livez"), 200)
        finally:
            await dog.stop()


class TaskRunStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_mismatched_celery_id_is_ignored_with_200(self):
        from app.routers import internal_admin as ia

        run = SimpleNamespace(id=uuid.uuid4(), celery_task_id="new", status="running")
        db = MagicMock()
        db.get = AsyncMock(return_value=run)
        db.commit = AsyncMock()
        body = ia.AdminTaskStatusRequest(status="failed", celery_task_id="old")
        out = await ia.update_task_status(run.id, body, None, db)
        self.assertTrue(out["ignored"])
        self.assertEqual(run.status, "running")
        db.commit.assert_not_called()


class ClaimConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_returns_503_when_slots_busy(self):
        from app.routers import internal_work as iw

        iw._claim_semaphore = asyncio.Semaphore(1)
        await iw._claim_semaphore.acquire()
        try:
            with patch.object(iw.settings, "work_claim_wait_seconds", 0.05):
                with self.assertRaises(HTTPException) as ctx:
                    await iw.claim_work(MagicMock(), None)
            self.assertEqual(ctx.exception.status_code, 503)
        finally:
            iw._claim_semaphore = None
