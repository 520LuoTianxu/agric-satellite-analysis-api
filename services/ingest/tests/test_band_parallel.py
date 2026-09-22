"""Unit tests for band-level download pooling (stdlib only)."""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

from app.core.band_parallel import (
    BandReadResult,
    band_max_workers,
    band_read_max_attempts,
    effective_band_workers,
    gdal_read_slot,
    reset_band_gdal_limit,
    run_parallel_band_jobs,
)


class BandMaxWorkersTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_band_gdal_limit()

    def test_default_is_8(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INGEST_BAND_MAX_WORKERS", None)
            self.assertEqual(band_max_workers(), 8)

    def test_parses_positive_int(self) -> None:
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "8"}):
            self.assertEqual(band_max_workers(), 8)

    def test_rejects_zero_and_junk(self) -> None:
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "0"}):
            self.assertEqual(band_max_workers(), 1)
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "nope"}):
            self.assertEqual(band_max_workers(), 8)

    def test_band_read_attempts_default_and_bounds(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAND_READ_MAX_ATTEMPTS", None)
            self.assertEqual(band_read_max_attempts(), 3)
        with patch.dict(os.environ, {"BAND_READ_MAX_ATTEMPTS": "99"}):
            self.assertEqual(band_read_max_attempts(), 10)


class EffectiveBandWorkersTests(unittest.TestCase):
    def test_single_band_is_always_one(self) -> None:
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "16"}):
            self.assertEqual(effective_band_workers(1, scene_workers=8), 1)

    def test_lone_scene_uses_all_bands_up_to_cap(self) -> None:
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "16"}):
            self.assertEqual(effective_band_workers(7, scene_workers=1), 7)
            self.assertEqual(effective_band_workers(20, scene_workers=1), 16)

    def test_nested_eight_scenes_still_overlap_agri_bands(self) -> None:
        # cap 16, 8 scene workers -> 4 band threads (32 thread budget / 8).
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "16"}):
            self.assertEqual(effective_band_workers(7, scene_workers=8), 4)

    def test_nested_does_not_drop_to_serial_when_scenes_equal_cap(self) -> None:
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "16"}):
            self.assertEqual(effective_band_workers(7, scene_workers=16), 2)


class RunParallelBandJobsTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_band_gdal_limit()

    def test_empty_dict(self) -> None:
        self.assertEqual(run_parallel_band_jobs({}, lambda k, v: v), {})

    def test_direct_reads_share_pool_limit_and_release_on_error(self) -> None:
        entered = threading.Event()
        finished = threading.Event()

        def direct_read():
            with gdal_read_slot():
                entered.set()
            finished.set()

        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "1"}):
            reset_band_gdal_limit()
            with self.assertRaises(ValueError):
                with gdal_read_slot():
                    # 池内回调会再次进入读取函数，单名额时也必须能复用当前名额。
                    result = run_parallel_band_jobs({"a": 1}, lambda k, v: v)
                    self.assertEqual(result, {"a": 1})
                    worker = threading.Thread(target=direct_read, daemon=True)
                    worker.start()
                    self.assertFalse(entered.wait(0.05))
                    raise ValueError("read failed")
            self.assertTrue(finished.wait(2))
            worker.join(timeout=2)

    def test_preserves_key_order(self) -> None:
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "8"}):
            reset_band_gdal_limit()
            items = {"B02": 2, "B04": 4, "B08": 8}
            out = run_parallel_band_jobs(items, lambda k, v: v * 10, scene_workers=1)
            self.assertEqual(list(out.keys()), ["B02", "B04", "B08"])
            self.assertEqual(out, {"B02": 20, "B04": 40, "B08": 80})

    def test_overlapping_reads_when_workers_gt_one(self) -> None:
        started: list[float] = []
        lock = threading.Lock()

        def _fn(key: str, value: int) -> int:
            with lock:
                started.append(time.perf_counter())
            time.sleep(0.12)
            return value

        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "8"}):
            reset_band_gdal_limit()
            t0 = time.perf_counter()
            out = run_parallel_band_jobs({"a": 1, "b": 2, "c": 3}, _fn, scene_workers=1)
            wall = time.perf_counter() - t0
        self.assertEqual(out, {"a": 1, "b": 2, "c": 3})
        self.assertEqual(len(started), 3)
        self.assertLess(max(started) - min(started), 0.08)
        self.assertLess(wall, 0.30)

    def test_gdal_cap_is_shared_across_nested_scene_calls(self) -> None:
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def _fn(key: str, value: int) -> int:
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.06)
            with lock:
                in_flight -= 1
            return value

        def _scene() -> None:
            run_parallel_band_jobs({"a": 1, "b": 2}, _fn, scene_workers=1)

        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "2"}):
            reset_band_gdal_limit()
            threads = [threading.Thread(target=_scene) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertLessEqual(peak, 2)
        self.assertGreaterEqual(peak, 2)

    def test_first_failure_raises(self) -> None:
        def _fn(key: str, value: int) -> int:
            if key == "b":
                raise ValueError("boom")
            time.sleep(0.05)
            return value

        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "8"}):
            reset_band_gdal_limit()
            with self.assertRaises(RuntimeError) as ctx:
                run_parallel_band_jobs({"a": 1, "b": 2, "c": 3}, _fn, scene_workers=1)
        self.assertIn("b", str(ctx.exception))

    def test_retry_releases_gdal_slot_and_logs_profiled_context(self) -> None:
        calls = 0
        acquired_during_backoff = threading.Event()
        logs: list[tuple[str, dict]] = []

        def _fn(_key: str, _value: str):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("request timed out")
            return BandReadResult(42, io_ms=12, reproject_ms=3)

        def _backoff(_seconds: float) -> None:
            def _probe() -> None:
                with gdal_read_slot():
                    acquired_during_backoff.set()

            probe = threading.Thread(target=_probe, daemon=True)
            probe.start()
            self.assertTrue(acquired_during_backoff.wait(1))
            probe.join(timeout=1)

        with (
            patch.dict(
                os.environ,
                {
                    "INGEST_BAND_MAX_WORKERS": "1",
                    "BAND_READ_MAX_ATTEMPTS": "3",
                    "BAND_READ_RETRY_DELAYS_SEC": "1,3",
                },
            ),
            patch("app.core.band_parallel.time.sleep", side_effect=_backoff),
            patch(
                "app.core.band_parallel._band_log",
                side_effect=lambda event, **fields: logs.append((event, fields)),
            ),
        ):
            reset_band_gdal_limit()
            result = run_parallel_band_jobs(
                {"B04": "https://example.test/red.tif?sig=secret"},
                _fn,
                log_context={
                    "job_id": "job",
                    "scene_id": "scene",
                    "date": "2026-09-22",
                    "sensor": "S2",
                },
            )

        self.assertEqual(result, {"B04": 42})
        self.assertEqual(calls, 2)
        self.assertTrue(acquired_during_backoff.is_set())
        attempts = [fields for event, fields in logs if event == "band_read_attempt_done"]
        self.assertEqual([row["outcome"] for row in attempts], ["retry", "success"])
        self.assertEqual(attempts[0]["host"], "example.test")
        self.assertNotIn("secret", str(attempts))
        self.assertEqual(attempts[1]["io_ms"], 12)
        self.assertEqual(attempts[1]["reproject_ms"], 3)
        self.assertEqual(attempts[1]["job_id"], "job")


if __name__ == "__main__":
    unittest.main()
