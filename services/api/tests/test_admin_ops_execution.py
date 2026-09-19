"""管理员执行监控接口的轻量状态汇总测试。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from app.routers.admin_ops import (
    _build_execution_groups,
    _to_execution_group_out,
    execution_overview,
)


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
            params_json={},
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
            payload_json={},
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

    def test_execution_groups_keep_failed_children_under_parent(self):
        now = datetime.now(timezone.utc)
        parent_id = uuid.uuid4()
        child_ok_id = uuid.uuid4()
        child_failed_id = uuid.uuid4()
        parent = SimpleNamespace(
            id=parent_id,
            land_id=None,
            type="overview_daily",
            status="partial",
            progress_json={"job_count": 2},
            params_json={"job_ids": [str(child_ok_id), str(child_failed_id)]},
            error=None,
            created_at=now,
            started_at=now,
            finished_at=now,
        )
        child_ok = SimpleNamespace(
            id=child_ok_id,
            land_id="land-1",
            type="satellite_batch",
            status="completed",
            progress_json={"completed": 1},
            params_json={"overview_run_id": str(parent_id)},
            error=None,
            created_at=now,
            started_at=now,
            finished_at=now,
        )
        child_failed = SimpleNamespace(
            id=child_failed_id,
            land_id="land-2",
            type="satellite_batch",
            status="failed",
            progress_json={},
            params_json={"overview_run_id": str(parent_id)},
            error="failed land",
            created_at=now,
            started_at=now,
            finished_at=now,
        )
        work_item = SimpleNamespace(
            id=uuid.uuid4(),
            type="satellite_download",
            status="done",
            priority=1,
            lease_owner="worker-1",
            lease_until=None,
            attempts=1,
            progress_json={"completed": 1},
            payload_json={"extras": {"job_id": str(child_ok_id)}},
            error=None,
            created_at=now,
            updated_at=now,
            idempotency_key=None,
            result_json={},
        )

        groups = _build_execution_groups(
            [parent, child_ok, child_failed], [work_item]
        )

        self.assertEqual(len(groups), 1)
        out = _to_execution_group_out(groups[0])
        self.assertEqual(out.type, "overview_daily")
        self.assertEqual(out.status, "partial")
        self.assertEqual(out.child_counts["jobs"], 2)
        self.assertEqual(out.child_counts["work_items"], 1)
        self.assertEqual(out.child_counts["failed"], 1)
        # WorkItem 与 child Job 是同一执行单元的两条记录，进度不能重复计数。
        self.assertEqual(out.child_counts["terminal"], 2)


if __name__ == "__main__":
    import unittest

    unittest.main()
