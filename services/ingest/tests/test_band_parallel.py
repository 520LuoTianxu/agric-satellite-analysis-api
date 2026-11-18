"""Unit tests for band-level download pooling (stdlib only)."""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import patch

from app.core.band_parallel import (
    BandReadResult,
    BandReadTimeout,
    GdalSlotWaitTimeout,
    SceneFailFastError,
    band_max_workers,
    band_read_max_attempts,
    band_read_timeout_sec,
    effective_band_workers,
    gdal_read_slot,
    gdal_slot_snapshot,
    gdal_slot_wait_sec,
    reset_band_gdal_limit,
    run_parallel_band_jobs,
    scene_inflight_slot,
    scene_max_inflight,
)
from app.core.cog_http import (
    band_read_hedge_after_sec,
    band_scene_budget_sec,
    band_scene_max_timeouts,
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

    def test_default_inflight_gives_a_started_scene_the_full_cap(self) -> None:
        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "8"}, clear=False):
            os.environ.pop("INGEST_SCENE_MAX_INFLIGHT", None)
            # Thread-pool size may still be 8; only one scene is in IO.
            self.assertEqual(effective_band_workers(13, scene_workers=8), 8)

    def test_two_inflight_scenes_split_the_slot_cap(self) -> None:
        with patch.dict(
            os.environ,
            {"INGEST_BAND_MAX_WORKERS": "8", "INGEST_SCENE_MAX_INFLIGHT": "2"},
        ):
            self.assertEqual(effective_band_workers(13, scene_workers=8), 4)


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

        # cap=4 allows 4 live reads; the previous half-cap orphan reserve is gone.
        with patch.dict(
            os.environ,
            {
                "INGEST_BAND_MAX_WORKERS": "4",
                "GDAL_SLOT_WAIT_SEC": "2",
                "INGEST_SCENE_MAX_INFLIGHT": "4",
            },
        ):
            reset_band_gdal_limit()
            threads = [threading.Thread(target=_scene) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertLessEqual(peak, 4)
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

    def test_band_read_timeout_default_and_disable(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAND_READ_TIMEOUT_SEC", None)
            self.assertEqual(band_read_timeout_sec(), 20.0)
        with patch.dict(os.environ, {"BAND_READ_TIMEOUT_SEC": "0"}):
            self.assertEqual(band_read_timeout_sec(), 0.0)
        with patch.dict(os.environ, {"BAND_READ_TIMEOUT_SEC": "nope"}):
            self.assertEqual(band_read_timeout_sec(), 20.0)

    def test_new_slot_hedge_and_fail_fast_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            for key in (
                "BAND_READ_HEDGE_AFTER_SEC",
                "GDAL_SLOT_WAIT_SEC",
                "BAND_SCENE_MAX_TIMEOUTS",
                "BAND_SCENE_BUDGET_SEC",
                "INGEST_SCENE_MAX_INFLIGHT",
            ):
                os.environ.pop(key, None)
            self.assertEqual(band_read_hedge_after_sec(), 5.0)
            self.assertEqual(gdal_slot_wait_sec(), 20.0)
            self.assertEqual(band_scene_max_timeouts(), 2)
            self.assertEqual(band_scene_budget_sec(), 60.0)
            self.assertEqual(scene_max_inflight(), 1)


class BandReadWallTimeoutTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_band_gdal_limit()

    def test_wall_clock_timeout_skips_without_retry(self) -> None:
        calls = 0
        release_hang = threading.Event()
        logs: list[tuple[str, dict]] = []

        def _fn(_key: str, _value: str) -> int:
            nonlocal calls
            calls += 1
            release_hang.wait(5)
            return 1

        with (
            patch.dict(
                os.environ,
                {
                    "INGEST_BAND_MAX_WORKERS": "1",
                    "BAND_READ_MAX_ATTEMPTS": "3",
                    "BAND_READ_TIMEOUT_SEC": "0.2",
                },
            ),
            patch(
                "app.core.band_parallel._band_log",
                side_effect=lambda event, **fields: logs.append((event, fields)),
            ),
        ):
            reset_band_gdal_limit()
            t0 = time.perf_counter()
            with self.assertRaises(RuntimeError) as ctx:
                run_parallel_band_jobs({"B04": "https://example.test/red.tif"}, _fn)
            wall = time.perf_counter() - t0

        release_hang.set()
        self.assertIn("B04", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, TimeoutError)
        self.assertLess(wall, 1.0)
        self.assertEqual(calls, 1)
        attempts = [fields for event, fields in logs if event == "band_read_attempt_done"]
        self.assertEqual([row["outcome"] for row in attempts], ["failed"])
        self.assertTrue(attempts[0]["timed_out"])
        self.assertEqual(attempts[0]["timeout_sec"], 0.2)

    def test_timeout_releases_slot_immediately(self) -> None:
        started = threading.Event()
        release_hang = threading.Event()
        acquired_after_timeout = threading.Event()

        def _fn(_key: str, _value: int) -> int:
            started.set()
            release_hang.wait(5)
            return 1

        def _probe() -> None:
            with gdal_read_slot():
                acquired_after_timeout.set()

        with patch.dict(
            os.environ,
            {
                "INGEST_BAND_MAX_WORKERS": "1",
                "BAND_READ_MAX_ATTEMPTS": "1",
                "BAND_READ_TIMEOUT_SEC": "0.2",
                "GDAL_SLOT_WAIT_SEC": "1",
            },
        ):
            reset_band_gdal_limit()
            with self.assertRaises(RuntimeError):
                run_parallel_band_jobs({"B08": 1}, _fn)
            self.assertTrue(started.wait(1))
            probe = threading.Thread(target=_probe, daemon=True)
            probe.start()
            self.assertTrue(
                acquired_after_timeout.wait(1),
                "timed-out reads must free the slot immediately",
            )
            release_hang.set()
            probe.join(timeout=2)

    def test_slot_wait_gives_up_at_wait_limit(self) -> None:
        holding = threading.Event()
        release_hold = threading.Event()

        def _hold() -> None:
            with gdal_read_slot(timeout=2):
                holding.set()
                release_hold.wait(5)

        with patch.dict(os.environ, {"INGEST_BAND_MAX_WORKERS": "1"}):
            reset_band_gdal_limit()
            holder = threading.Thread(target=_hold, daemon=True)
            holder.start()
            self.assertTrue(holding.wait(1))
            t0 = time.perf_counter()
            with self.assertRaises(GdalSlotWaitTimeout):
                with gdal_read_slot(timeout=0.2):
                    pass
            wall = time.perf_counter() - t0
            self.assertGreaterEqual(wall, 0.15)
            self.assertLess(wall, 1.0)
            release_hold.set()
            holder.join(timeout=2)

    def test_scene_fail_fast_skips_remaining_bands_after_timeouts(self) -> None:
        started: list[str] = []
        lock = threading.Lock()

        def _fn(key: str, _value: int) -> int:
            with lock:
                started.append(key)
            time.sleep(0.4)
            return 1

        logs: list[tuple[str, dict]] = []
        with (
            patch.dict(
                os.environ,
                {
                    "INGEST_BAND_MAX_WORKERS": "2",
                    "BAND_READ_MAX_ATTEMPTS": "1",
                    "BAND_READ_TIMEOUT_SEC": "0.15",
                    "BAND_SCENE_MAX_TIMEOUTS": "2",
                    "BAND_SCENE_BUDGET_SEC": "30",
                    "GDAL_SLOT_WAIT_SEC": "1",
                },
            ),
            patch(
                "app.core.band_parallel._band_log",
                side_effect=lambda event, **fields: logs.append((event, fields)),
            ),
        ):
            reset_band_gdal_limit()
            with self.assertRaises(RuntimeError) as ctx:
                run_parallel_band_jobs(
                    {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5},
                    _fn,
                    scene_workers=1,
                )
        self.assertIsInstance(ctx.exception.__cause__, (BandReadTimeout, TimeoutError, SceneFailFastError))
        # Two workers start first; after two timeouts the rest must be skipped.
        self.assertLessEqual(len(started), 3)
        self.assertGreaterEqual(len(started), 2)
        self.assertIn("scene_fail_fast", [event for event, _ in logs])

    def test_slot_wait_does_not_fail_fast_the_scene(self) -> None:
        holding = threading.Event()
        release_hold = threading.Event()
        logs: list[tuple[str, dict]] = []

        def _hold() -> None:
            with gdal_read_slot(timeout=5):
                holding.set()
                release_hold.wait(5)

        def _fn(_key: str, _value: int) -> int:
            return 1

        with (
            patch.dict(
                os.environ,
                {
                    "INGEST_BAND_MAX_WORKERS": "1",
                    "INGEST_SCENE_MAX_INFLIGHT": "1",
                    "BAND_READ_MAX_ATTEMPTS": "1",
                    "BAND_READ_TIMEOUT_SEC": "2",
                    "GDAL_SLOT_WAIT_SEC": "0.2",
                    "BAND_SCENE_MAX_TIMEOUTS": "1",
                    "BAND_SCENE_BUDGET_SEC": "30",
                },
            ),
            patch(
                "app.core.band_parallel._band_log",
                side_effect=lambda event, **fields: logs.append((event, fields)),
            ),
            # Force two in-scene workers against a cap of 1 so both bands
            # contend; effective_band_workers would otherwise collapse to 1.
            patch("app.core.band_parallel.effective_band_workers", return_value=2),
        ):
            reset_band_gdal_limit()
            holder = threading.Thread(target=_hold, daemon=True)
            holder.start()
            self.assertTrue(holding.wait(1))
            with self.assertRaises(RuntimeError) as ctx:
                run_parallel_band_jobs({"A": 1, "B": 2}, _fn, scene_workers=2)
            release_hold.set()
            holder.join(timeout=2)

        self.assertIsInstance(ctx.exception.__cause__, GdalSlotWaitTimeout)
        fail_fast = [fields for event, fields in logs if event == "scene_fail_fast"]
        self.assertEqual(fail_fast, [])
        attempts = [fields for event, fields in logs if event == "band_read_attempt_done"]
        slot_waits = [row for row in attempts if row.get("slot_wait_timeout")]
        self.assertTrue(slot_waits)
        self.assertFalse(any(row.get("timed_out") for row in slot_waits))
        self.assertGreaterEqual(len(slot_waits), 2)

    def test_scene_budget_ignores_slot_queue_wait(self) -> None:
        holding = threading.Event()
        release_hold = threading.Event()
        ran = threading.Event()

        def _hold() -> None:
            with gdal_read_slot(timeout=5):
                holding.set()
                release_hold.wait(5)

        def _fn(_key: str, _value: int) -> int:
            ran.set()
            return 7

        with patch.dict(
            os.environ,
            {
                "INGEST_BAND_MAX_WORKERS": "1",
                "INGEST_SCENE_MAX_INFLIGHT": "2",
                "BAND_READ_MAX_ATTEMPTS": "1",
                "BAND_READ_TIMEOUT_SEC": "2",
                "GDAL_SLOT_WAIT_SEC": "2",
                "BAND_SCENE_MAX_TIMEOUTS": "9",
                "BAND_SCENE_BUDGET_SEC": "0.25",
            },
        ):
            reset_band_gdal_limit()
            holder = threading.Thread(target=_hold, daemon=True)
            holder.start()
            self.assertTrue(holding.wait(1))
            result_box: dict[str, dict] = {}
            error_box: list[BaseException] = []

            def _run() -> None:
                try:
                    result_box["out"] = run_parallel_band_jobs({"A": 1}, _fn)
                except BaseException as exc:  # noqa: BLE001
                    error_box.append(exc)

            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            # Queue longer than the 0.25s IO budget, then free the slot.
            time.sleep(0.4)
            release_hold.set()
            worker.join(timeout=3)
            holder.join(timeout=2)
        self.assertFalse(error_box, error_box)
        self.assertEqual(result_box.get("out"), {"A": 7})
        self.assertTrue(ran.is_set())

    def test_scene_inflight_cap_lets_a_started_scene_finish(self) -> None:
        lock = threading.Lock()
        in_io: set[str] = set()
        peak = 0
        started_order: list[str] = []
        finished: dict[str, list[str]] = {"A": [], "B": []}

        def _scene(name: str) -> None:
            def _fn(key: str, _value: int) -> int:
                nonlocal peak
                with lock:
                    in_io.add(name)
                    if name not in started_order:
                        started_order.append(name)
                    peak = max(peak, len(in_io))
                time.sleep(0.12)
                with lock:
                    finished[name].append(str(key))
                    in_io.discard(name)
                return 1

            run_parallel_band_jobs(
                {f"{name}{i}": i for i in range(3)},
                _fn,
                scene_workers=1,
            )

        with patch.dict(
            os.environ,
            {
                "INGEST_BAND_MAX_WORKERS": "8",
                "INGEST_SCENE_MAX_INFLIGHT": "1",
                "BAND_READ_MAX_ATTEMPTS": "1",
                "BAND_READ_TIMEOUT_SEC": "2",
                "GDAL_SLOT_WAIT_SEC": "2",
                "BAND_SCENE_MAX_TIMEOUTS": "9",
                "BAND_SCENE_BUDGET_SEC": "30",
            },
        ):
            reset_band_gdal_limit()
            workers = [
                threading.Thread(target=_scene, args=("A",), daemon=True),
                threading.Thread(target=_scene, args=("B",), daemon=True),
            ]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(timeout=5)
        self.assertEqual(peak, 1)
        self.assertEqual(len(finished["A"]), 3)
        self.assertEqual(len(finished["B"]), 3)
        # The second scene must not have started IO until the first finished.
        self.assertEqual(len(started_order), 2)




if __name__ == "__main__":
    unittest.main()
