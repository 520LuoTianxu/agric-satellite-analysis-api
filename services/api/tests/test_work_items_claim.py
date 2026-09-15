"""Unit tests for work_items claim / complete helpers."""

from __future__ import annotations

import asyncio
import unittest
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

from app.services import work_items as wi


class WorkQueueModeTests(unittest.TestCase):
    def test_default_legacy(self) -> None:
        with patch.object(wi.settings, "work_queue_mode", "legacy"):
            self.assertEqual(wi.work_queue_mode(), "legacy")
            self.assertFalse(wi.should_enqueue_work_items())
            self.assertTrue(wi.should_publish_mq())

    def test_claim_mode(self) -> None:
        with patch.object(wi.settings, "work_queue_mode", "claim"):
            self.assertTrue(wi.should_enqueue_work_items())
            self.assertFalse(wi.should_publish_mq())

    def test_dual_mode(self) -> None:
        with patch.object(wi.settings, "work_queue_mode", "dual"):
            self.assertTrue(wi.should_enqueue_work_items())
            self.assertTrue(wi.should_publish_mq())

    def test_invalid_falls_back_legacy(self) -> None:
        with patch.object(wi.settings, "work_queue_mode", "wat"):
            self.assertEqual(wi.work_queue_mode(), "legacy")


class _FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeItem:
    def __init__(self, **kwargs: Any):
        self.id = kwargs.get("id", uuid.uuid4())
        self.type = kwargs.get("type", "assessment_report")
        self.status = kwargs.get("status", "pending")
        self.priority = kwargs.get("priority", 0)
        self.lease_owner = kwargs.get("lease_owner")
        self.lease_until = kwargs.get("lease_until")
        self.attempts = kwargs.get("attempts", 0)
        self.payload_json = kwargs.get("payload_json", {})
        self.result_json = kwargs.get("result_json")
        self.error = kwargs.get("error")
        self.progress_json = kwargs.get("progress_json")
        self.updated_at = kwargs.get("updated_at")


class ClaimCompleteTests(unittest.TestCase):
    def test_claim_marks_leased(self) -> None:
        item = _FakeItem()
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_FakeResult([]), _FakeResult([item])])
        db.flush = AsyncMock()

        with patch.object(wi.settings, "work_reaper_on_claim", True):
            with patch.object(wi.settings, "work_lease_seconds", 600):
                with patch.object(wi.settings, "work_claim_default_limit", 1):
                    rows = asyncio.run(wi.claim_work_items(db, worker_id="w1", limit=1))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "leased")
        self.assertEqual(rows[0].lease_owner, "w1")
        self.assertEqual(rows[0].attempts, 1)
        self.assertIsNotNone(rows[0].lease_until)

    def test_complete_from_leased(self) -> None:
        item = _FakeItem(status="leased", lease_owner="w1", attempts=1)
        db = AsyncMock()
        db.get = AsyncMock(return_value=item)
        db.flush = AsyncMock()

        out = asyncio.run(
            wi.complete_work_item(db, item.id, result={"ok": True}, worker_id="w1")
        )
        self.assertEqual(out.status, "done")
        self.assertEqual(out.result_json, {"ok": True})
        self.assertIsNone(out.lease_owner)

    def test_complete_owner_mismatch(self) -> None:
        item = _FakeItem(status="leased", lease_owner="other")
        db = AsyncMock()
        db.get = AsyncMock(return_value=item)

        with self.assertRaises(ValueError):
            asyncio.run(wi.complete_work_item(db, item.id, worker_id="w1"))

    def test_fail_sets_failed(self) -> None:
        item = _FakeItem(status="leased", lease_owner="w1")
        db = AsyncMock()
        db.get = AsyncMock(return_value=item)
        db.flush = AsyncMock()

        out = asyncio.run(wi.fail_work_item(db, item.id, error="boom", worker_id="w1"))
        self.assertEqual(out.status, "failed")
        self.assertEqual(out.error, "boom")

    def test_fail_retry_returns_pending(self) -> None:
        item = _FakeItem(status="leased", lease_owner="w1")
        db = AsyncMock()
        db.get = AsyncMock(return_value=item)
        db.flush = AsyncMock()

        out = asyncio.run(
            wi.fail_work_item(db, item.id, error="temp", worker_id="w1", retry=True)
        )
        self.assertEqual(out.status, "pending")


class InternalAuthTests(unittest.TestCase):
    def test_missing_token_config(self) -> None:
        from fastapi import HTTPException
        from app.middleware.internal_auth import require_internal_token
        from app.core import config

        with patch.object(config.settings, "internal_api_token", ""):
            with self.assertRaises(HTTPException) as ctx:
                require_internal_token(authorization="Bearer x")
            self.assertEqual(ctx.exception.status_code, 503)

    def test_bad_bearer(self) -> None:
        from fastapi import HTTPException
        from app.middleware.internal_auth import require_internal_token
        from app.core import config

        with patch.object(config.settings, "internal_api_token", "secret"):
            with self.assertRaises(HTTPException) as ctx:
                require_internal_token(authorization="Bearer nope")
            self.assertEqual(ctx.exception.status_code, 401)

    def test_ok(self) -> None:
        from app.middleware.internal_auth import require_internal_token
        from app.core import config

        with patch.object(config.settings, "internal_api_token", "secret"):
            require_internal_token(authorization="Bearer secret")


if __name__ == "__main__":
    unittest.main()


class ClaimAgentGuardTests(unittest.TestCase):
    def test_should_run_claim_agent_only_claim(self) -> None:
        with patch.object(wi.settings, "work_queue_mode", "legacy"):
            self.assertFalse(wi.should_run_claim_agent())
        with patch.object(wi.settings, "work_queue_mode", "dual"):
            self.assertFalse(wi.should_run_claim_agent())
            self.assertTrue(wi.should_enqueue_work_items())
            self.assertTrue(wi.should_publish_mq())
        with patch.object(wi.settings, "work_queue_mode", "claim"):
            self.assertTrue(wi.should_run_claim_agent())
            self.assertFalse(wi.should_publish_mq())

    def test_claimable_types_include_data_pulls(self) -> None:
        for t in (
            "field_bootstrap",
            "satellite_analysis",
            "agri_bridge",
            "weather_backfill",
            "soil_fetch",
            "assessment_report",
            "season_growth_report",
        ):
            self.assertIn(t, wi.CLAIMABLE_TYPES)
        self.assertIn("field_bootstrap", wi.COMPLETE_ON_DISPATCH_TYPES)
        self.assertNotIn("assessment_report", wi.COMPLETE_ON_DISPATCH_TYPES)

    def test_idempotency_key_prefers_job_id(self) -> None:
        key = wi.work_item_idempotency_key(
            "assessment_report",
            task_id="tid-1",
            extras={"job_id": "job-9"},
        )
        self.assertEqual(key, "assessment_report:job-9")
        key2 = wi.work_item_idempotency_key(
            "weather_backfill", task_id="tid-2", extras={}
        )
        self.assertEqual(key2, "weather_backfill:tid-2")
