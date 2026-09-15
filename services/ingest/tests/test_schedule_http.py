"""Beat 定时任务走 Internal HTTP，不打开 SyncSession。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class WeeklyIndexHttpTests(unittest.TestCase):
    def test_http_path_does_not_open_session(self) -> None:
        from app.tasks import backfill as bf

        celery = MagicMock()
        with (
            patch.object(bf, "celery_app", celery),
            patch.object(bf, "get_db_session") as get_db,
            patch.object(bf, "_schedule_via_http", return_value=True),
            patch(
                "agric_satellite_analysis_common.internal_api.weekly_index_prepare",
                return_value={
                    "items": [
                        {
                            "task_name": "app.tasks.ndvi.process_ndvi",
                            "job_id": "job-1",
                            "countdown": 0,
                        }
                    ],
                    "lands_checked": 4,
                    "lands_dispatched": 1,
                    "skipped_agri": 3,
                },
            ),
        ):
            out = bf.schedule_weekly_index_compute.run()

        get_db.assert_not_called()
        celery.send_task.assert_called_once_with(
            "app.tasks.ndvi.process_ndvi", args=["job-1"], countdown=0
        )
        self.assertTrue(out["http"])
        self.assertEqual(out["jobs_dispatched"], 1)


class WeatherScheduleHttpTests(unittest.TestCase):
    def test_http_path_does_not_open_session(self) -> None:
        from app.tasks import weather as wx

        group_mock = MagicMock()
        with (
            patch.object(wx, "_get_db_session") as get_db,
            patch.object(wx, "group", return_value=group_mock),
            patch(
                "agric_satellite_analysis_common.internal_api.internal_api_enabled",
                return_value=True,
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.weather_land_ids",
                return_value={"land_ids": ["L1", "L2"], "batch_size": 50},
            ),
        ):
            out = wx.schedule_daily_weather_fetch.run()

        get_db.assert_not_called()
        self.assertTrue(out["http"])
        self.assertEqual(out["lands"], 2)
        group_mock.apply_async.assert_called()


class OverviewScheduleHttpTests(unittest.TestCase):
    def test_http_required(self) -> None:
        from app.tasks import overview_preagg as ov

        with patch(
            "agric_satellite_analysis_common.internal_api.internal_api_enabled",
            return_value=False,
        ):
            with self.assertRaises(RuntimeError):
                ov.refresh_overview_stats.run()

    def test_http_path(self) -> None:
        from app.tasks import overview_preagg as ov

        with (
            patch(
                "agric_satellite_analysis_common.internal_api.internal_api_enabled",
                return_value=True,
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.refresh_overview_stats",
                return_value={"ok": True, "regions": 3},
            ) as http_call,
        ):
            out = ov.refresh_overview_stats.run(window_days=60)

        http_call.assert_called_once()
        self.assertTrue(out["ok"])
        self.assertEqual(out["regions"], 3)
