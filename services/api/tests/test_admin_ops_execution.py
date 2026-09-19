"""管理员执行监控接口的轻量状态汇总测试。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from app.routers.admin_ops import execution_overview


class _FakeResult:
    def __init__(self, *, rows=None, items=None):
        self._rows = list(rows or [])
        self._items = None if items is None else list(items)

    def all(self):
        return self._rows if self._items is None else self._items

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._items)


class _FakeDb:
    def __init__(self, results):
        self._results = iter(results)

    async def execute(self, _statement):
        return next(self._results)


class AdminOpsExecutionTests(IsolatedAsyncioTestCase):
    async def test_execution_overview_counts_all_and_summarizes_recent_rows(self):
        now = datetime.now(timezone.utc)
        job = SimpleNamespace(
            id=uuid.uuid4(),
            land_id="land-1",
            type="ndvi",
            status="running",
            progress_json={"phase": "download", "completed": 2, "large": [1, 2, 3]},
            error=None,
            created_at=now,
            started_at=now,
            finished_at=None,
        )
        work_item = SimpleNamespace(
            id=uuid.uuid4(),
            type="satellite_download",
            status="failed",
            priority=4,
            lease_owner="worker-1",
            lease_until=None,
            attempts=2,
            progress_json={"phase": "download", "completed": 1},
            error="download failed",
            created_at=now,
            updated_at=now,
        )
        db = _FakeDb(
            [
                _FakeResult(rows=[("running", 2), ("failed", 1)]),
                _FakeResult(rows=[("failed", 3), ("done", 5)]),
                _FakeResult(items=[job]),
                _FakeResult(items=[work_item]),
            ]
        )

        out = await execution_overview(None, db, limit=10)

        self.assertEqual(out.job_counts, {"running": 2, "failed": 1, "all": 3})
        self.assertEqual(
            out.work_item_counts, {"failed": 3, "done": 5, "all": 8}
        )
        self.assertEqual(out.jobs[0].progress_summary, {"phase": "download", "completed": 2})
        self.assertEqual(out.work_items[0].error, "download failed")


if __name__ == "__main__":
    import unittest

    unittest.main()
