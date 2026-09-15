"""D3: complete → result_apply upsert path (assessment job + weather domain)."""

from __future__ import annotations

import asyncio
import unittest
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

from app.services import work_items as wi


class _FakeItem:
    def __init__(self, **kwargs: Any):
        self.id = kwargs.get("id", uuid.uuid4())
        self.type = kwargs.get("type", "assessment_report")
        self.status = kwargs.get("status", "leased")
        self.priority = kwargs.get("priority", 0)
        self.lease_owner = kwargs.get("lease_owner", "w1")
        self.lease_until = kwargs.get("lease_until")
        self.attempts = kwargs.get("attempts", 1)
        self.payload_json = kwargs.get("payload_json", {})
        self.result_json = kwargs.get("result_json")
        self.error = kwargs.get("error")
        self.progress_json = kwargs.get("progress_json")
        self.updated_at = kwargs.get("updated_at")


class CompleteApplyTests(unittest.TestCase):
    def test_complete_invokes_apply_complete_result(self) -> None:
        job_id = str(uuid.uuid4())
        item = _FakeItem()
        db = AsyncMock()
        db.get = AsyncMock(return_value=item)
        db.flush = AsyncMock()

        result = {
            "kind": "assessment_report",
            "job_id": job_id,
            "object_key": "reports/x.pdf",
            "public_url": "https://example.com/x.pdf",
            "filename": "t.pdf",
            "score": 88,
            "grade": "A",
        }

        with patch(
            "openfarm_common.result_apply.apply_complete_result",
            return_value={"domain": {"assessment_report": {"job_updated": True}}},
        ) as apply_mock:
            out = asyncio.run(
                wi.complete_work_item(db, item.id, result=result, worker_id="w1")
            )

        self.assertEqual(out.status, "done")
        apply_mock.assert_called_once_with(result)
        self.assertIn("_apply", out.result_json)
        self.assertEqual(
            out.result_json["_apply"]["domain"]["assessment_report"]["job_updated"],
            True,
        )

    def test_complete_skips_apply_on_dispatch_ack(self) -> None:
        item = _FakeItem()
        db = AsyncMock()
        db.get = AsyncMock(return_value=item)
        db.flush = AsyncMock()

        with patch(
            "openfarm_common.result_apply.apply_complete_result",
            return_value={"skipped": True, "reason": "dispatch_ack"},
        ) as apply_mock:
            asyncio.run(
                wi.complete_work_item(
                    db,
                    item.id,
                    result={"dispatched": ["x"], "celery_id": "abc"},
                    worker_id="w1",
                )
            )
        apply_mock.assert_called_once()


class ResultApplyUnitTests(unittest.TestCase):
    def test_assessment_payload_updates_job(self) -> None:
        from openfarm_common import result_apply as ra

        job_id = str(uuid.uuid4())
        with patch.object(ra, "_apply_assessment_job_progress", return_value=True) as m:
            stats = ra.apply_result_envelope(
                {
                    "kind": "assessment_report",
                    "job_id": job_id,
                    "object_key": "k",
                    "score": 1,
                    "grade": "B",
                }
            )
        m.assert_called_once()
        self.assertIn("domain", stats)
        self.assertTrue(stats["domain"]["assessment_report"]["job_updated"])

    def test_weather_payload_calls_apply_weather(self) -> None:
        from openfarm_common import result_apply as ra

        with patch.object(ra, "apply_weather_payload", return_value=3) as m:
            stats = ra.apply_result_envelope(
                {
                    "kind": "weather_daily",
                    "land_id": "L1",
                    "rows": [{"land_id": "L1", "date": "2026-01-01"}],
                }
            )
        m.assert_called_once()
        self.assertEqual(stats["domain"]["weather_rows"], 3)


class InternalResultsRouterTests(unittest.TestCase):
    def test_envelope_from_body_merges(self) -> None:
        from app.routers.internal_results import ApplyRequest, _envelope_from_body

        body = ApplyRequest(
            result={"rows": []},
            kind="weather_daily",
            status="success",
        )
        env = _envelope_from_body(body)
        self.assertEqual(env["kind"], "weather_daily")
        self.assertEqual(env["status"], "success")


class HttpWritesFlagTests(unittest.TestCase):
    def test_default_pg_writes(self) -> None:
        from openfarm_common import internal_api as ia
        import os
        from unittest.mock import patch as p

        with p.dict(
            os.environ,
            {
                "API_BASE_URL": "http://api:8000",
                "INTERNAL_API_TOKEN": "t",
                "INGEST_PG_WRITES": "1",
                "WORK_QUEUE_MODE": "legacy",
                "INGEST_HTTP_WRITES": "0",
            },
            clear=False,
        ):
            self.assertTrue(ia.ingest_pg_writes_enabled())
            self.assertFalse(ia.http_writes_enabled())

    def test_force_http_when_pg_writes_off(self) -> None:
        from openfarm_common import internal_api as ia
        import os
        from unittest.mock import patch as p

        with p.dict(
            os.environ,
            {
                "API_BASE_URL": "http://api:8000",
                "INTERNAL_API_TOKEN": "t",
                "INGEST_PG_WRITES": "0",
                "WORK_QUEUE_MODE": "legacy",
            },
            clear=False,
        ):
            self.assertFalse(ia.ingest_pg_writes_enabled())
            self.assertTrue(ia.http_writes_enabled())

    def test_claim_mode_enables_http_writes(self) -> None:
        from openfarm_common import internal_api as ia
        import os
        from unittest.mock import patch as p

        with p.dict(
            os.environ,
            {
                "API_BASE_URL": "http://api:8000",
                "INTERNAL_API_TOKEN": "t",
                "INGEST_PG_WRITES": "1",
                "WORK_QUEUE_MODE": "claim",
            },
            clear=False,
        ):
            self.assertTrue(ia.http_writes_enabled())


if __name__ == "__main__":
    unittest.main()
