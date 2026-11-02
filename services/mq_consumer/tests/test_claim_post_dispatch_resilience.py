"""Claim 环：派发成功后 progress/MQ 失败不能卡住租约或触发双派发。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app import work_agent as wa


class PostDispatchResilienceTests(unittest.TestCase):
    def test_complete_still_runs_when_progress_raises(self) -> None:
        client = MagicMock()
        item = {
            "id": "w-boot-1",
            "type": "land_bootstrap",
            "attempts": 1,
            "payload_json": {"land_id": "29461", "extras": {}},
        }
        result = {
            "dispatched": ["app.tasks.weather.backfill_weather_for_land"],
            "celery_ids": ["c1"],
            "land_id": "29461",
        }
        with (
            patch.object(wa, "_dispatch_celery", return_value=result),
            patch.object(wa, "progress", side_effect=TimeoutError("progress hung")),
            patch.object(wa, "complete") as comp,
            patch.object(wa, "fail") as fail,
        ):
            wa.process_item(client, item)
        comp.assert_called_once()
        fail.assert_not_called()

    def test_dispatch_failure_still_fails_lease(self) -> None:
        client = MagicMock()
        item = {
            "id": "w-boot-2",
            "type": "land_bootstrap",
            "attempts": 1,
            "payload_json": {"land_id": "29461", "extras": {}},
        }
        with (
            patch.object(wa, "_dispatch_celery", side_effect=RuntimeError("redis down")),
            patch.object(wa, "fail") as fail,
            patch.object(wa, "complete") as comp,
        ):
            wa.process_item(client, item)
        fail.assert_called_once()
        comp.assert_not_called()

    def test_bootstrap_publish_failure_still_completes(self) -> None:
        """handler 内 MQ publish 失败不得阻止 Celery 派发结果回到 process_item。"""
        from app import handler as h
        from agric_satellite_analysis_common.mq_schemas import TaskMessage

        task = TaskMessage(
            task_id="t1",
            type="land_bootstrap",
            land_id="29461",
            extras={"skip_indices": True},
        )
        async_result = MagicMock(id="celery-w")
        with (
            patch.object(h, "_send_task", return_value=async_result) as send,
            patch.object(
                h, "publish_task_result", side_effect=TimeoutError("amqp/db hung")
            ) as pub,
        ):
            info = h._dispatch_land_bootstrap(task, "29461")
        self.assertEqual(info["celery_ids"], ["celery-w", "celery-w"])
        self.assertIn("app.tasks.weather.backfill_weather_for_land", info["dispatched"])
        pub.assert_called_once()
        kwargs = pub.call_args.kwargs
        self.assertFalse(kwargs.get("collect_parcel_urls", True))
        self.assertGreaterEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
