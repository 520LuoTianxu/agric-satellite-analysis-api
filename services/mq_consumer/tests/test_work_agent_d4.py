"""D4 claim agent guards and type coverage."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from app import work_agent as wa


class ClaimModeGuardTests(unittest.TestCase):
    def test_claim_sends_configured_worker_name(self) -> None:
        client = MagicMock()
        client.post.return_value.json.return_value = {"items": []}
        with patch.dict(
            os.environ,
            {"WORKER_NAME": "download-east-01", "WORKER_ID": "legacy-id"},
            clear=False,
        ):
            with (
                patch.object(wa, "weather_daily_limit_reached", return_value=False),
                patch.object(wa, "queue_depths", return_value={"ingest": 7}),
            ):
                self.assertEqual(wa.claim_batch(client), [])
        request = client.post.call_args
        self.assertEqual(request.kwargs["json"]["worker_name"], "download-east-01")
        self.assertEqual(request.kwargs["json"]["worker_id"], "download-east-01")
        self.assertEqual(request.kwargs["json"]["queue_name"], "cpu_compute")
        self.assertEqual(request.kwargs["json"]["pending_queue_count"], 7)
        self.assertEqual(request.kwargs["json"]["queue_depths"], {"ingest": 7})

    def test_should_run_only_claim(self) -> None:
        with patch.dict(os.environ, {"WORK_QUEUE_MODE": "legacy"}, clear=False):
            self.assertEqual(wa.work_queue_mode(), "legacy")
            self.assertFalse(wa.should_run_claim_agent())
        with patch.dict(os.environ, {"WORK_QUEUE_MODE": "dual"}, clear=False):
            self.assertEqual(wa.work_queue_mode(), "dual")
            self.assertFalse(wa.should_run_claim_agent())
        with patch.dict(os.environ, {"WORK_QUEUE_MODE": "claim"}, clear=False):
            self.assertTrue(wa.should_run_claim_agent())

    def test_run_forever_refuses_dual(self) -> None:
        with patch.dict(os.environ, {"WORK_QUEUE_MODE": "dual"}, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                wa.run_forever()
            self.assertIn("double-dispatch", str(ctx.exception))

    def test_default_types_cover_data_paths(self) -> None:
        for t in (
            "weather_backfill",
            "soil_fetch",
            "land_bootstrap",
            "satellite_analysis",
        ):
            self.assertIn(t, wa.DEFAULT_TYPES)
            self.assertIn(t, wa.COMPLETE_ON_DISPATCH_TYPES)
        self.assertIn("admin_task", wa.DEFAULT_TYPES)
        self.assertIn("admin_task", wa.COMPLETE_ON_DISPATCH_TYPES)

    def test_weather_type_is_removed_after_daily_api_limit(self) -> None:
        with patch.dict(
            os.environ,
            {"WORK_CLAIM_TYPES": "weather_backfill,soil_fetch"},
            clear=False,
        ):
            with patch.object(wa, "weather_daily_limit_reached", return_value=True):
                self.assertEqual(wa.claim_types(), ["soil_fetch"])

    def test_empty_claim_type_list_does_not_claim_all_types(self) -> None:
        client = MagicMock()
        with patch.object(wa, "claim_types", return_value=[]):
            self.assertEqual(wa.claim_batch(client), [])
        client.post.assert_not_called()

    def test_explicit_claim_types_also_respect_daily_api_limit(self) -> None:
        client = MagicMock()
        client.post.return_value.json.return_value = {"items": []}
        with (
            patch.object(wa, "weather_daily_limit_reached", return_value=True),
            patch.object(wa, "queue_depths", return_value={}),
        ):
            self.assertEqual(
                wa.claim_batch(client, types=["weather_backfill", "soil_fetch"]),
                [],
            )
        self.assertEqual(client.post.call_args.kwargs["json"]["types"], ["soil_fetch"])

    def test_admin_task_dispatch_does_not_require_land_id(self) -> None:
        result = MagicMock(id="celery-admin-1")
        item = {
            "id": "w-admin-1",
            "type": "admin_task",
            "payload_json": {
                "admin_task_run_id": "run-1",
                "task_name": "app.tasks.weather.schedule_daily_weather_fetch",
                "kwargs": {},
            },
        }
        with patch.object(wa.celery_client, "send_task", return_value=result) as send:
            dispatched = wa._dispatch_celery(item)
        send.assert_called_once_with(
            "app.tasks.weather.schedule_daily_weather_fetch",
            kwargs={},
            queue="cpu_compute",
        )
        self.assertEqual(dispatched["celery_id"], "celery-admin-1")
        self.assertEqual(dispatched["admin_task_run_id"], "run-1")


class ProcessItemTests(unittest.TestCase):
    def test_report_does_not_complete_on_dispatch(self) -> None:
        client = MagicMock()
        item = {
            "id": "w1",
            "type": "assessment_report",
            "payload_json": {
                "land_id": "f1",
                "extras": {"job_id": "j1"},
            },
        }
        with patch.object(
            wa,
            "_dispatch_celery",
            return_value={
                "dispatched": ["x"],
                "celery_id": "c1",
                "job_id": "j1",
            },
        ):
            with patch.object(wa, "progress") as prog:
                with patch.object(wa, "complete") as comp:
                    wa.process_item(client, item)
        prog.assert_called_once()
        comp.assert_not_called()

    def test_fanout_completes_on_dispatch(self) -> None:
        client = MagicMock()
        item = {
            "id": "w2",
            "type": "weather_backfill",
            "payload_json": {"land_id": "f1", "extras": {"days": 7}},
        }
        with patch.object(
            wa,
            "_dispatch_celery",
            return_value={
                "dispatched": ["app.tasks.weather.backfill_weather_for_land"],
                "celery_ids": ["c2"],
                "land_id": "f1",
            },
        ):
            with patch.object(wa, "progress"):
                with patch.object(wa, "complete") as comp:
                    wa.process_item(client, item)
        comp.assert_called_once()
        args, kwargs = comp.call_args
        self.assertEqual(args[1], "w2")
        self.assertEqual(args[2]["phase"], "dispatched")

    def test_admin_task_reports_running_monitors_and_completes(self) -> None:
        client = MagicMock()
        item = {
            "id": "w-admin-2",
            "type": "admin_task",
            "payload_json": {
                "admin_task_run_id": "run-2",
                "task_name": "app.tasks.weather.schedule_daily_weather_fetch",
                "kwargs": {},
            },
        }
        result = {
            "dispatched": ["app.tasks.weather.schedule_daily_weather_fetch"],
            "celery_id": "celery-admin-2",
            "admin_task_run_id": "run-2",
        }
        with patch.object(wa, "_dispatch_celery", return_value=result):
            with patch.object(wa, "progress"):
                with patch.object(wa, "complete") as comp:
                    with patch.object(wa, "report_admin_task_status") as report:
                        with patch.object(wa, "start_admin_task_monitor") as monitor:
                            wa.process_item(client, item)
        report.assert_called_once_with(
            client, "run-2", "celery-admin-2", "running"
        )
        monitor.assert_called_once_with(client, "run-2", "celery-admin-2")
        comp.assert_called_once()

    def test_dispatch_failure_retries_until_third_claim(self) -> None:
        for attempts, should_retry in ((1, True), (2, True), (3, False)):
            client = MagicMock()
            item = {
                "id": f"w-fail-{attempts}",
                "type": "satellite_batch",
                "attempts": attempts,
                "payload_json": {
                    "land_id": "f1",
                    "extras": {"job_id": "j1"},
                },
            }
            with (
                patch.object(
                    wa,
                    "_dispatch_celery",
                    side_effect=RuntimeError("redis unavailable"),
                ),
                patch.object(wa, "fail") as fail,
            ):
                wa.process_item(client, item)
            fail.assert_called_once_with(
                client, f"w-fail-{attempts}", "redis unavailable", retry=should_retry
            )


class MainEntrypointTests(unittest.TestCase):
    def test_dual_uses_mq_not_claim(self) -> None:
        from app import main as mq_main

        with patch.object(mq_main, "work_queue_mode", return_value="dual"):
            with patch.object(mq_main, "should_run_claim_agent", return_value=False):
                with patch.object(mq_main, "run_claim_agent") as claim:
                    with patch.object(mq_main, "_run_mq") as mq:
                        mq_main.main()
        claim.assert_not_called()
        mq.assert_called_once()

    def test_claim_uses_agent(self) -> None:
        from app import main as mq_main

        with patch.object(mq_main, "work_queue_mode", return_value="claim"):
            with patch.object(mq_main, "should_run_claim_agent", return_value=True):
                with patch.object(mq_main, "run_claim_agent") as claim:
                    with patch.object(mq_main, "_run_mq") as mq:
                        mq_main.main()
        claim.assert_called_once()
        mq.assert_not_called()


if __name__ == "__main__":
    unittest.main()
