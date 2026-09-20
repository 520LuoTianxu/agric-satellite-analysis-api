"""Beat 定时任务走 Internal HTTP，不打开 SyncSession。"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

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
            patch.object(wx, "weather_daily_limit_reached", return_value=False),
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

        payload = {
            "window_from": "2026-07-20",
            "window_to": "2026-09-18",
            "crop": None,
            "lands": [
                {
                    "land_id": "L1",
                    "area_mu": 10,
                    "province_code": "11",
                    "province_name": "北京",
                    "city_code": None,
                    "city_name": None,
                    "county_code": None,
                    "county_name": None,
                    "s2": None,
                    "s1": [],
                    "weak": False,
                }
            ],
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        internal_client = MagicMock()
        internal_client.__enter__.return_value = MagicMock()
        internal_client.__exit__.return_value = False
        oss_client = MagicMock()
        oss_client.__enter__.return_value = oss_client
        oss_client.__exit__.return_value = False
        oss_response = MagicMock(content=raw)
        oss_client.get.return_value = oss_response

        with (
            patch(
                "agric_satellite_analysis_common.internal_api.internal_api_enabled",
                return_value=True,
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.refresh_overview_stats",
                return_value={
                    "ok": True,
                    "status": "batch",
                    "window_from": "2026-07-20",
                    "window_to": "2026-09-18",
                    "crop": None,
                    "land_count": 1,
                    "next_cursor": "L1",
                    "done": True,
                    "oss_url": "https://oss.test/input.json",
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                },
            ) as http_call,
            patch(
                "agric_satellite_analysis_common.internal_api.finalize_overview_stats",
                return_value={"ok": True, "status": "success", "regions": 2},
            ),
            patch.object(ov, "internal_client", return_value=internal_client),
            patch.object(ov.httpx, "Client", return_value=oss_client),
            patch.object(ov, "get_storage", return_value=MagicMock()),
        ):
            out = ov.refresh_overview_stats.run(window_days=60)

        http_call.assert_called_once()
        self.assertTrue(out["ok"])
        self.assertEqual(out["regions"], 2)
        self.assertEqual(out["land_count"], 1)
        oss_client.get.assert_called_once_with("https://oss.test/input.json")

    def test_oss_batches_are_downloaded_concurrently(self) -> None:
        from app.tasks import overview_preagg as ov

        class FakeResponse:
            def __init__(self, content: bytes):
                self.content = content

            def raise_for_status(self) -> None:
                return None

        raw_by_url: dict[str, bytes] = {}
        batches: list[dict] = []
        for index in range(3):
            url = f"https://oss.test/input-{index}.json"
            payload = {
                "window_from": "2026-07-20",
                "window_to": "2026-09-18",
                "crop": None,
                "lands": [
                    {
                        "land_id": f"L{index}",
                        "area_mu": 10,
                        "province_code": "11",
                        "province_name": "北京",
                        "city_code": None,
                        "city_name": None,
                        "county_code": None,
                        "county_name": None,
                        "s2": None,
                        "s1": [],
                        "weak": False,
                    }
                ],
            }
            raw = json.dumps(payload, separators=(",", ":")).encode()
            raw_by_url[url] = raw
            batches.append(
                {
                    "ok": True,
                    "status": "batch",
                    "window_from": "2026-07-20",
                    "window_to": "2026-09-18",
                    "crop": None,
                    "land_count": 1,
                    "next_cursor": f"L{index}",
                    "done": index == 2,
                    "oss_url": url,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )

        class FakeOssClient:
            def __init__(self) -> None:
                self._lock = threading.Lock()
                self.active = 0
                self.max_active = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, url: str) -> FakeResponse:
                with self._lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    time.sleep(0.05)
                    return FakeResponse(raw_by_url[url])
                finally:
                    with self._lock:
                        self.active -= 1

        internal_client = MagicMock()
        internal_client.__enter__.return_value = MagicMock()
        internal_client.__exit__.return_value = False
        oss_client = FakeOssClient()

        with (
            patch(
                "agric_satellite_analysis_common.internal_api.internal_api_enabled",
                return_value=True,
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.refresh_overview_stats",
                side_effect=batches,
            ) as http_call,
            patch(
                "agric_satellite_analysis_common.internal_api.finalize_overview_stats",
                return_value={"ok": True, "status": "success", "regions": 2},
            ),
            patch.object(ov, "internal_client", return_value=internal_client),
            patch.object(ov.httpx, "Client", return_value=oss_client),
            patch.object(ov, "get_storage", return_value=MagicMock()),
        ):
            out = ov.refresh_overview_stats.run(window_days=60, oss_workers=3)

        self.assertEqual(http_call.call_count, 3)
        self.assertGreaterEqual(oss_client.max_active, 2)
        self.assertEqual(out["land_count"], 3)
        self.assertEqual(out["batch_count"], 3)

    def test_transient_oss_error_is_retried(self) -> None:
        from app.tasks import overview_preagg as ov

        payload = {
            "window_from": "2026-07-20",
            "window_to": "2026-09-18",
            "crop": None,
            "lands": [],
        }
        raw = json.dumps(payload, separators=(",", ":")).encode()
        url = "https://oss.test/retry.json"
        batch = {
            "ok": True,
            "status": "batch",
            "window_from": "2026-07-20",
            "window_to": "2026-09-18",
            "crop": None,
            "land_count": 0,
            "next_cursor": "L1",
            "done": True,
            "oss_key": "overview/preagg/input/retry.json",
            "oss_url": url,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

        class FakeResponse:
            content = raw

            def raise_for_status(self) -> None:
                return None

        class RetryOssClient:
            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, request_url: str) -> FakeResponse:
                self.calls += 1
                if self.calls == 1:
                    raise httpx.ConnectError(
                        "temporary network error",
                        request=httpx.Request("GET", request_url),
                    )
                return FakeResponse()

        internal_client = MagicMock()
        internal_client.__enter__.return_value = MagicMock()
        internal_client.__exit__.return_value = False
        oss_client = RetryOssClient()

        with (
            patch(
                "agric_satellite_analysis_common.internal_api.internal_api_enabled",
                return_value=True,
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.refresh_overview_stats",
                return_value=batch,
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.finalize_overview_stats",
                return_value={"ok": True, "status": "success", "regions": 1},
            ),
            patch.object(ov, "internal_client", return_value=internal_client),
            patch.object(ov.httpx, "Client", return_value=oss_client),
            patch.object(ov, "get_storage", return_value=MagicMock()),
            patch.object(ov.time, "sleep"),
            patch.object(ov.random, "random", return_value=0.0),
        ):
            out = ov.refresh_overview_stats.run(oss_retries=2)

        self.assertEqual(oss_client.calls, 2)
        self.assertEqual(out["batch_count"], 1)


class DailySatelliteScheduleTests(unittest.TestCase):
    def setUp(self):
        from app.tasks import overview_preagg

        self.task = overview_preagg.refresh_daily_satellite
        self.enabled = patch(
            "agric_satellite_analysis_common.internal_api.internal_api_enabled",
            return_value=True,
        )
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def test_download_discovery_then_ingestion_check(self):
        with (
            patch(
                "agric_satellite_analysis_common.internal_api.daily_satellite_prepare",
                return_value={"run_id": "run-1"},
            ) as prepare,
            patch(
                "agric_satellite_analysis_common.internal_api.daily_satellite_finalize",
                return_value={"status": "completed", "regions": 5},
            ) as finalize,
        ):
            out = self.task.run(as_of="2026-09-16")
        prepare.assert_called_once_with(as_of="2026-09-16")
        finalize.assert_called_once_with("run-1")
        self.assertEqual(out["status"], "completed")

    def test_pending_results_delay_retry_without_recreating_batch(self):
        with (
            patch(
                "agric_satellite_analysis_common.internal_api.daily_satellite_prepare"
            ) as prepare,
            patch(
                "agric_satellite_analysis_common.internal_api.daily_satellite_finalize",
                return_value={"status": "running"},
            ),
            patch.object(
                self.task, "retry", side_effect=RuntimeError("delayed")
            ) as retry,
        ):
            with self.assertRaisesRegex(RuntimeError, "delayed"):
                self.task.run(run_id="run-1", as_of="2026-09-16")
        prepare.assert_not_called()
        self.assertEqual(retry.call_args.kwargs["countdown"], 300)
        self.assertEqual(
            retry.call_args.kwargs["kwargs"], {"run_id": "run-1", "as_of": "2026-09-16"}
        )

    def test_dispatch_error_retry_keeps_original_statistical_day(self):
        with (
            patch(
                "agric_satellite_analysis_common.internal_api.daily_satellite_prepare",
                side_effect=RuntimeError("HTTP error"),
            ),
            patch.object(
                self.task, "retry", side_effect=RuntimeError("delayed")
            ) as retry,
        ):
            with self.assertRaisesRegex(RuntimeError, "delayed"):
                self.task.run(as_of="2026-09-16")
        self.assertEqual(
            retry.call_args.kwargs["kwargs"], {"run_id": None, "as_of": "2026-09-16"}
        )

    def test_partial_batch_is_reported_without_polling_forever(self):
        with (
            patch(
                "agric_satellite_analysis_common.internal_api.daily_satellite_finalize",
                return_value={"status": "partial"},
            ),
            patch.object(self.task, "retry") as retry,
        ):
            out = self.task.run(run_id="run-1", as_of="2026-09-16")
        self.assertEqual(out["status"], "partial")
        retry.assert_not_called()

    def test_internal_http_is_required(self):
        with patch(
            "agric_satellite_analysis_common.internal_api.internal_api_enabled",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "API_BASE_URL"):
                self.task.run()
