"""POST /internal/work/worker-heartbeat：只刷新下载机心跳，不领取任务。"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.routers import internal_work as iw


class WorkerHeartbeatTests(unittest.TestCase):
    def test_touches_worker_without_claiming(self) -> None:
        db = AsyncMock()
        body = iw.ClaimRequest(
            worker_name="download-boen",
            types=["satellite_batch"],
            interval_seconds=4,
            queue_name="cpu_compute",
            pending_queue_count=410,
            queue_depths={"decloud": 400},
        )
        with (
            patch.object(iw.wi, "touch_download_worker", AsyncMock()) as touch,
            patch.object(iw.wi, "claim_work_items", AsyncMock()) as claim,
        ):
            out = asyncio.run(iw.worker_heartbeat(body, None, db))
        claim.assert_not_called()
        touch.assert_awaited_once()
        kw = touch.await_args.kwargs
        self.assertEqual(kw["worker_id"], "download-boen")
        self.assertEqual(kw["claim_count"], 0)
        self.assertEqual(kw["claim_types"], ["satellite_batch"])
        self.assertEqual(kw["queue_depths"], {"decloud": 400})
        db.commit.assert_awaited_once()
        self.assertTrue(out.ok)
        self.assertEqual(out.worker_name, "download-boen")

    def test_route_registered(self) -> None:
        paths = {(r.path, tuple(sorted(r.methods))) for r in iw.router.routes}
        self.assertIn(("/internal/work/worker-heartbeat", ("POST",)), paths)


if __name__ == "__main__":
    unittest.main()
