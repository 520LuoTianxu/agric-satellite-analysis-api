"""Unit tests for band-level download pooling (stdlib only)."""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

from app.core.band_parallel import (
    band_max_workers,
    effective_band_workers,
    reset_band_gdal_limit,
    run_parallel_band_jobs,
)


class BandMaxWorkersTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_band_gdal_limit()

    def test_default_is_16(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INGEST_BAND_MAX_WORKERS", None)
            self.assertEqual(band_max_workers(), 16)

    def test_parses_positive_int(self) -> None:
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "8"}):
            self.assertEqual(band_max_workers(), 8)

    def test_rejects_zero_and_junk(self) -> None:
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "0"}):
            self.assertEqual(band_max_workers(), 1)
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "nope"}):
            self.assertEqual(band_max_workers(), 16)


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
            out = run_parallel_band_jobs(
                {"a": 1, "b": 2, "c": 3}, _fn, scene_workers=1
            )
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


if __name__ == "__main__":
    unittest.main()
