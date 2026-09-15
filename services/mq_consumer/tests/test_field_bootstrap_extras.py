"""Unit tests: land_bootstrap forwards date_from/days into Celery kwargs."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import handler as handler_mod  # noqa: E402


class _Task:
    def __init__(self, extras):
        self.task_id = "t-1"
        self.extras = extras


class LandBootstrapExtrasTests(unittest.TestCase):
    def test_forwards_date_window_and_days(self) -> None:
        sends: list[tuple] = []

        def fake_send(name, args=None, kwargs=None, queue=None):
            sends.append((name, args or [], kwargs or {}, queue))
            return SimpleNamespace(id=f"celery-{len(sends)}")

        with (
            patch.object(handler_mod.celery_client, "send_task", side_effect=fake_send),
            patch.object(handler_mod, "publish_task_result") as pub,
        ):
            info = handler_mod._dispatch_land_bootstrap(
                _Task(
                    {
                        "date_from": "2023-09-14",
                        "date_to": "2026-09-14",
                        "days": 1096,
                    }
                ),
                "land-1",
            )

        self.assertIn("app.tasks.weather.backfill_weather_for_land", info["dispatched"])
        self.assertIn(
            "app.tasks.backfill.backfill_indices_for_land", info["dispatched"]
        )
        weather = next(s for s in sends if "weather" in s[0])
        self.assertEqual(weather[2].get("days"), 1096)
        indices = next(s for s in sends if "backfill_indices" in s[0])
        self.assertEqual(indices[2].get("date_from"), "2023-09-14")
        self.assertEqual(indices[2].get("date_to"), "2026-09-14")
        pub.assert_called_once()
        self.assertEqual(info.get("days"), 1096)

    def test_derives_days_from_date_from(self) -> None:
        sends: list[tuple] = []

        def fake_send(name, args=None, kwargs=None, queue=None):
            sends.append((name, args or [], kwargs or {}, queue))
            return SimpleNamespace(id=f"celery-{len(sends)}")

        with (
            patch.object(handler_mod.celery_client, "send_task", side_effect=fake_send),
            patch.object(handler_mod, "publish_task_result"),
        ):
            info = handler_mod._dispatch_land_bootstrap(
                _Task({"date_from": "2026-09-01", "date_to": "2026-09-14"}),
                "land-1",
            )
        weather = next(s for s in sends if "weather" in s[0])
        self.assertEqual(weather[2].get("days"), 13)
        self.assertEqual(info.get("days"), 13)


    def test_indices_use_the_same_land_id(self) -> None:
        sends: list[tuple] = []

        def fake_send(name, args=None, kwargs=None, queue=None):
            sends.append((name, args or [], kwargs or {}, queue))
            return SimpleNamespace(id=f"celery-{len(sends)}")

        with (
            patch.object(handler_mod.celery_client, "send_task", side_effect=fake_send),
            patch.object(handler_mod, "publish_task_result"),
        ):
            info = handler_mod._dispatch_land_bootstrap(
                _Task({"date_from": "2024-09-14", "date_to": "2026-09-14", "days": 730}),
                "land-1",
            )
        indices = next(s for s in sends if "backfill_indices" in s[0])
        self.assertEqual(indices[1], ["land-1"])
        self.assertNotIn("allow_agri", indices[2])
        self.assertIn("app.tasks.agri_bridge.bridge_after_backfill", info["dispatched"])

    def test_followup_assessment_enqueued_after_pulls(self) -> None:
        sends: list[tuple] = []

        def fake_send(name, args=None, kwargs=None, queue=None):
            sends.append((name, args or [], kwargs or {}, queue))
            return SimpleNamespace(id=f"celery-{len(sends)}")

        with (
            patch.object(handler_mod.celery_client, "send_task", side_effect=fake_send),
            patch.object(handler_mod, "publish_task_result"),
        ):
            info = handler_mod._dispatch_land_bootstrap(
                _Task(
                    {
                        "date_from": "2024-09-14",
                        "date_to": "2026-09-14",
                        "days": 730,
                        "followup_assessment": {
                            "job_id": "job-1",
                            "mq_task_id": "mq-assess-1",
                            "crop_type": "corn",
                            "date_from": "2024-09-14",
                            "date_to": "2026-09-14",
                            "years": 2,
                        },
                    }
                ),
                "land-1",
            )
        assess = next(s for s in sends if "assessment_report" in s[0])
        self.assertTrue(assess[2].get("pull_data"))
        self.assertEqual(assess[2].get("job_id"), "job-1")
        self.assertIn("wait_celery_ids", assess[2])
        # weather + soil + indices (+ bridge) queued before assessment kwargs snapshot
        self.assertGreaterEqual(len(assess[2]["wait_celery_ids"]), 3)
        self.assertIn(
            "app.tasks.assessment_report.generate_assessment_report", info["dispatched"]
        )

    def test_followup_season_growth_enqueued_after_pulls(self) -> None:
        sends: list[tuple] = []

        def fake_send(name, args=None, kwargs=None, queue=None):
            sends.append((name, args or [], kwargs or {}, queue))
            return SimpleNamespace(id=f"celery-{len(sends)}")

        with (
            patch.object(handler_mod.celery_client, "send_task", side_effect=fake_send),
            patch.object(handler_mod, "publish_task_result"),
        ):
            info = handler_mod._dispatch_land_bootstrap(
                _Task(
                    {
                        "date_from": "2026-06-01",
                        "date_to": "2026-09-30",
                        "days": 121,
                        "followup_season_growth": {
                            "job_id": "job-sg-1",
                            "mq_task_id": "mq-sg-1",
                            "start_date": "2026-06-01",
                            "end_date": "2026-09-30",
                            "crops": ["corn"],
                            "label": "S1",
                        },
                    }
                ),
                "land-1",
            )
        sg = next(s for s in sends if "season_growth_report" in s[0])
        self.assertTrue(sg[2].get("pull_data"))
        self.assertEqual(sg[2].get("job_id"), "job-sg-1")
        self.assertEqual(sg[2].get("start_date"), "2026-06-01")
        self.assertEqual(sg[2].get("end_date"), "2026-09-30")
        self.assertIn("wait_celery_ids", sg[2])
        self.assertGreaterEqual(len(sg[2]["wait_celery_ids"]), 3)
        self.assertIn(
            "app.tasks.season_growth_report.generate_season_growth_report",
            info["dispatched"],
        )


if __name__ == "__main__":
    unittest.main()
