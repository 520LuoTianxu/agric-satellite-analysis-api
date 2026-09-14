"""D4 claim agent guards and type coverage."""

from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from app import work_agent as wa


class ClaimModeGuardTests(unittest.TestCase):
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
            "field_bootstrap",
            "satellite_analysis",
        ):
            self.assertIn(t, wa.DEFAULT_TYPES)
            self.assertIn(t, wa.COMPLETE_ON_DISPATCH_TYPES)


class ProcessItemTests(unittest.TestCase):
    def test_report_does_not_complete_on_dispatch(self) -> None:
        client = MagicMock()
        item = {
            "id": "w1",
            "type": "assessment_report",
            "payload_json": {
                "field_id": "f1",
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
            "payload_json": {"field_id": "f1", "extras": {"days": 7}},
        }
        with patch.object(
            wa,
            "_dispatch_celery",
            return_value={
                "dispatched": ["app.tasks.weather.backfill_weather_for_field"],
                "celery_ids": ["c2"],
                "field_id": "f1",
            },
        ):
            with patch.object(wa, "progress"):
                with patch.object(wa, "complete") as comp:
                    wa.process_item(client, item)
        comp.assert_called_once()
        args, kwargs = comp.call_args
        self.assertEqual(args[1], "w2")
        self.assertEqual(args[2]["phase"], "dispatched")


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
