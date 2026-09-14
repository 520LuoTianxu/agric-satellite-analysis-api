"""D4: publish_api_task enqueues work_items when dual/claim."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException


class PublishEnqueueTests(unittest.TestCase):
    def test_claim_mode_enqueues_and_skips_mq(self) -> None:
        from app.mq_publish import publish_api_task

        with patch(
            "app.services.work_items.should_enqueue_work_items", return_value=True
        ), patch(
            "app.services.work_items.should_publish_mq", return_value=False
        ), patch(
            "app.services.work_items.enqueue_work_item_sync", return_value="work-1"
        ) as enq, patch(
            "app.services.work_items.CLAIMABLE_TYPES",
            frozenset({"weather_backfill"}),
        ), patch(
            "app.services.work_items.work_item_idempotency_key",
            return_value="weather_backfill:t1",
        ):
            tid = publish_api_task(
                type="weather_backfill",
                field_id="f1",
                extras={"days": 3},
                task_id="t1",
            )
        self.assertEqual(tid, "t1")
        enq.assert_called_once()
        self.assertEqual(enq.call_args.kwargs["type"], "weather_backfill")

    def test_legacy_does_not_enqueue(self) -> None:
        from app.mq_publish import publish_api_task

        with patch(
            "app.services.work_items.should_enqueue_work_items", return_value=False
        ), patch(
            "app.services.work_items.should_publish_mq", return_value=True
        ), patch(
            "app.services.work_items.enqueue_work_item_sync"
        ) as enq, patch(
            "openfarm_common.settings.settings"
        ) as common_settings, patch(
            "openfarm_common.mq.publish_task"
        ) as pub, patch(
            "openfarm_common.mq_schemas.TaskMessage", MagicMock()
        ):
            common_settings.cloudamqp_url = "amqps://example"
            tid = publish_api_task(
                type="weather_backfill", field_id="f1", extras={"days": 1}
            )
        enq.assert_not_called()
        pub.assert_called_once()
        self.assertTrue(tid)

    def test_claim_enqueue_failure_raises(self) -> None:
        from app.mq_publish import publish_api_task

        with patch(
            "app.services.work_items.should_enqueue_work_items", return_value=True
        ), patch(
            "app.services.work_items.should_publish_mq", return_value=False
        ), patch(
            "app.services.work_items.enqueue_work_item_sync",
            side_effect=RuntimeError("db down"),
        ), patch(
            "app.services.work_items.CLAIMABLE_TYPES",
            frozenset({"soil_fetch"}),
        ), patch(
            "app.services.work_items.work_item_idempotency_key",
            return_value="soil_fetch:t2",
        ):
            with self.assertRaises(HTTPException) as ctx:
                publish_api_task(
                    type="soil_fetch",
                    field_id="f1",
                    extras={"job_id": "j1"},
                    task_id="t2",
                )
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
