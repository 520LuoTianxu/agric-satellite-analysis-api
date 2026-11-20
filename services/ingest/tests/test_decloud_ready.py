"""S1 shared-cache write, ±6 day match, coverage zeros, and cache-only pending flush."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

import numpy as np


def _grid():
    mask = np.zeros((4, 5), dtype=bool)
    mask[1:3, 1:4] = True
    transform = (10.0, 0.0, 500_000.0, 0.0, -10.0, 4_000_000.0)
    return "EPSG:32650", (4, 5), transform, mask


def _write_s2(land_id: str, day: str, grid_key: str, cloud: float = 10.0) -> None:
    from app.core.decloud_cache import put_window_meta, write_window_array

    _, shape, _, mask = _grid()
    stack = np.zeros((13, shape[0], shape[1]), dtype=np.float32)
    ndvi = np.zeros(shape, dtype=np.float32)
    put_window_meta(
        land_id=land_id,
        date_str=day,
        sensor="S2",
        cloud_cover=cloud,
        extra={"grid_key": grid_key},
    )
    write_window_array(
        land_id, day, "S2", grid_key=grid_key, stack=stack, field_mask=mask, ndvi=ndvi
    )


def _write_s1(land_id: str, day: str, grid_key: str, via_helper: bool = False) -> None:
    from app.core.decloud_cache import put_window_meta, write_window_array

    shape = _grid()[1]
    mask = _grid()[3]
    stack = np.ones((2, shape[0], shape[1]), dtype=np.float32)
    if via_helper:
        return
    put_window_meta(
        land_id=land_id,
        date_str=day,
        sensor="S1",
        extra={"grid_key": grid_key},
    )
    write_window_array(land_id, day, "S1", grid_key=grid_key, stack=stack, field_mask=mask)


class S1CacheGridKeyTests(unittest.TestCase):
    def test_s1_cache_grid_key_matches_s2_and_window_grid_key(self) -> None:
        from app.core.decloud_cache import read_window_array, window_grid_key
        from app.tasks.decloud_uncrtaints import (
            cache_optical_s1_window,
            cache_optical_s2_window,
        )

        crs, shape, transform, mask = _grid()
        expected = window_grid_key(crs, shape, transform, mask)
        vv = np.full(shape, -12.5, dtype=np.float32)
        vh = np.full(shape, -18.0, dtype=np.float32)
        s2_stack = np.zeros((13, shape[0], shape[1]), dtype=np.float32)
        ndvi = np.zeros(shape, dtype=np.float32)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"DECLOUD_CACHE_DIR": tmp, "DECLOUD_CACHE_ONLY": "1"}
        ):
            ok = cache_optical_s1_window(
                land_id="LAND-GRID",
                date_str="2026-07-10",
                vv=vv,
                vh=vh,
                target_shape=shape,
                target_transform=transform,
                target_crs=crs,
                field_mask=mask,
                stac_id="S1A_test",
                radiometric_calibration={"kind": "sigma0_lut"},
            )
            self.assertTrue(ok)
            with (
                patch("app.core.uncrtaints.stack_s2_13", return_value=s2_stack),
                patch(
                    "app.tasks.decloud_uncrtaints._ndvi_from_raw_window",
                    return_value=ndvi,
                ),
            ):
                cache_optical_s2_window(
                    land_id="LAND-GRID",
                    date_str="2026-07-15",
                    bands={"B04": vv},
                    band_hrefs={},
                    cloud_cover=40.0,
                    stac_id="S2_test",
                    target_shape=shape,
                    target_transform=transform,
                    target_crs=crs,
                    field_mask=mask,
                )
            s1 = read_window_array("LAND-GRID", "2026-07-10", "S1", expected_grid_key=expected)
            s2 = read_window_array("LAND-GRID", "2026-07-15", "S2", expected_grid_key=expected)
            self.assertIsNotNone(s1)
            self.assertIsNotNone(s2)
            self.assertEqual(s1["stack"].shape, (2, 4, 5))
            np.testing.assert_array_equal(s1["stack"][0], vv)
            # Fresh arrays (not NaN-masked) and identical fingerprint.
            self.assertTrue(np.isfinite(s1["stack"]).all())
            self.assertEqual(
                window_grid_key(crs, shape, transform, mask),
                expected,
            )


class S1DateMatchAndCoverageTests(unittest.TestCase):
    def test_match_within_six_days_legacy_key_and_confirmed_zero(self) -> None:
        from app.core.decloud_cache import window_grid_key
        from app.core.decloud_ready import (
            find_s1_for_s2_date,
            record_s1_coverage,
            s1_window_confirmed_empty,
        )

        crs, shape, transform, mask = _grid()
        key = window_grid_key(crs, shape, transform, mask)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"DECLOUD_CACHE_DIR": tmp}
        ):
            _write_s1("L-MATCH", "2026-07-10", key)
            hit = find_s1_for_s2_date("L-MATCH", date(2026, 7, 15), expected_grid_key=key)
            self.assertTrue(hit["ready"])
            self.assertEqual(hit["s1_date"], date(2026, 7, 10))
            self.assertEqual(hit["delta_days"], 5)

            miss = find_s1_for_s2_date("L-MATCH", date(2026, 7, 20), expected_grid_key=key)
            self.assertFalse(miss["ready"])
            self.assertEqual(miss["kind"], "missing")

            # Legacy decloud buffer: S1 file named after the S2 date.
            _write_s1("L-MATCH", "2026-08-01", key)
            legacy = find_s1_for_s2_date("L-MATCH", "2026-08-01", expected_grid_key=key)
            self.assertTrue(legacy["ready"])
            self.assertEqual(legacy["s1_date"], date(2026, 8, 1))

            record_s1_coverage("L-MATCH", "2026-09-01", "2026-09-30", failed_dates=[])
            self.assertTrue(s1_window_confirmed_empty("L-MATCH", "2026-09-15"))
            zero = find_s1_for_s2_date("L-MATCH", "2026-09-15", expected_grid_key=key)
            self.assertTrue(zero["ready"])
            self.assertEqual(zero["kind"], "confirmed_zero")

            record_s1_coverage(
                "L-MATCH", "2026-10-01", "2026-10-31", failed_dates=["2026-10-12"]
            )
            self.assertFalse(s1_window_confirmed_empty("L-MATCH", "2026-10-15"))
            failed = find_s1_for_s2_date("L-MATCH", "2026-10-15", expected_grid_key=key)
            self.assertFalse(failed["ready"])

            # Incomplete coverage (window hangs off the searched range).
            record_s1_coverage("L-OTHER", "2026-11-10", "2026-11-20", failed_dates=[])
            self.assertFalse(s1_window_confirmed_empty("L-OTHER", "2026-11-10"))


class PendingReadyFlushTests(unittest.TestCase):
    def test_pending_waits_for_s1_then_flushes_once(self) -> None:
        from app.core.decloud_cache import window_grid_key
        from app.core.decloud_ready import (
            assess_target_ready,
            iter_pending_targets,
            mark_pending_status,
            upsert_pending_targets,
        )
        from app.tasks.decloud_uncrtaints import flush_ready_decloud_targets

        crs, shape, transform, mask = _grid()
        key = window_grid_key(crs, shape, transform, mask)
        land_id = "L-PEND"
        env = {
            "DECLOUD_CACHE_DIR": "",  # set below
            "DECLOUD_CACHE_ONLY": "1",
            "DECLOUD_USE_SAR": "1",
            "DECLOUD_INPUT_T": "3",
            "DECLOUD_ENABLED": "1",
        }
        with tempfile.TemporaryDirectory() as tmp:
            env["DECLOUD_CACHE_DIR"] = tmp
            with patch.dict(os.environ, env):
                for day, cloud in (
                    ("2026-07-01", 5.0),
                    ("2026-07-15", 80.0),
                    ("2026-07-20", 8.0),
                ):
                    _write_s2(land_id, day, key, cloud=cloud)
                upsert_pending_targets(
                    land_id,
                    [{"date": "2026-07-15", "stac_cloud": 80.0}],
                    date_from="2026-07-01",
                    date_to="2026-07-31",
                    mq_task_id="task-1",
                    season_months=[7],
                )
                waiting = assess_target_ready(
                    land_id, "2026-07-15", expected_grid_key=key
                )
                self.assertFalse(waiting["ready"])
                self.assertEqual(waiting["reason"], "waiting_s1")
                with patch(
                    "app.tasks.decloud_uncrtaints.enqueue_decloud_parcel_batch"
                ) as enqueue:
                    flushed = flush_ready_decloud_targets(
                        land_id, expected_grid_key=key
                    )
                    enqueue.assert_not_called()
                self.assertEqual(flushed["dispatched"], 0)

                for day in ("2026-07-01", "2026-07-15", "2026-07-20"):
                    _write_s1(land_id, day, key)
                ready = assess_target_ready(
                    land_id, "2026-07-15", expected_grid_key=key
                )
                self.assertTrue(ready["ready"])
                self.assertEqual(len(ready["picked"]), 3)
                with patch(
                    "app.tasks.decloud_uncrtaints.enqueue_decloud_parcel_batch",
                    return_value=True,
                ) as enqueue:
                    flushed = flush_ready_decloud_targets(
                        land_id, expected_grid_key=key
                    )
                    enqueue.assert_called_once()
                    args = enqueue.call_args.kwargs
                    self.assertEqual(args["land_id"], land_id)
                    self.assertEqual([t["date"] for t in args["targets"]], ["2026-07-15"])
                    # Second flush must not re-enqueue while dispatched.
                    flushed_again = flush_ready_decloud_targets(
                        land_id, expected_grid_key=key
                    )
                    self.assertEqual(flushed["dispatched"], 1)
                    self.assertEqual(flushed_again["dispatched"], 0)
                    self.assertEqual(enqueue.call_count, 1)
                mark_pending_status(land_id, "2026-07-15", "done")
                with patch(
                    "app.tasks.decloud_uncrtaints.enqueue_decloud_parcel_batch"
                ) as enqueue:
                    flush_ready_decloud_targets(land_id, expected_grid_key=key)
                    enqueue.assert_not_called()
                statuses = {row["date"]: row["status"] for row in iter_pending_targets(land_id)}
                self.assertEqual(statuses["2026-07-15"], "done")


class CacheOnlyOldMessageTests(unittest.TestCase):
    def _network_guards(self, decloud):
        boom = AssertionError("cache_only must not use STAC/COG")
        return (
            patch.object(decloud, "_search_s2_l2a_windows", side_effect=boom),
            patch.object(decloud, "_buffer_s2_windows", side_effect=boom),
            patch.object(decloud, "_buffer_s1_for_dates", side_effect=boom),
            patch.object(decloud, "_read_s1_for_dates", side_effect=boom),
            patch.object(decloud, "read_bands_windowed_parallel", side_effect=boom),
            patch(
                "app.tasks.sentinel1.search_s1_scenes",
                side_effect=boom,
            ),
        )

    def test_old_message_holds_without_network_when_s1_missing(self) -> None:
        from app.core.decloud_cache import window_grid_key
        from app.core.decloud_ready import iter_pending_targets
        from app.tasks import decloud_uncrtaints as decloud

        crs, shape, transform, mask = _grid()
        key = window_grid_key(crs, shape, transform, mask)
        land_id = "L-OLD"
        context = {
            "land_meta": {"land_id": land_id, "tile_id": "T1", "land_name": land_id},
            "land_geom_geojson": {"type": "Polygon", "coordinates": []},
            "target_transform": transform,
            "target_crs": crs,
            "target_shape": shape,
            "land_mask": mask,
            "bounds": (0, 0, 1, 1),
        }
        loaded: list[tuple[str, str]] = []
        real_read = None

        def counting_read(land, day, sensor, expected_grid_key=None):
            loaded.append((str(sensor), str(day)[:10]))
            return real_read(land, day, sensor, expected_grid_key=expected_grid_key)

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "DECLOUD_CACHE_DIR": tmp,
                "DECLOUD_CACHE_ONLY": "1",
                "DECLOUD_USE_SAR": "1",
                "DECLOUD_ENABLED": "1",
                "DECLOUD_INPUT_T": "3",
            },
        ):
            from app.core import decloud_cache

            real_read = decloud_cache.read_window_array
            for day, cloud in (
                ("2026-07-01", 5.0),
                ("2026-07-15", 80.0),
                ("2026-07-20", 8.0),
            ):
                _write_s2(land_id, day, key, cloud=cloud)
            with ExitStack() as stack:
                for guard in self._network_guards(decloud):
                    stack.enter_context(guard)
                stack.enter_context(patch.object(decloud, "decloud_enabled", return_value=True))
                stack.enter_context(patch.object(decloud, "decloud_cache_only", return_value=True))
                stack.enter_context(patch.object(decloud, "_decloud_http_only", return_value=True))
                get_db = stack.enter_context(patch.object(decloud, "get_db_session"))
                stack.enter_context(patch.object(decloud, "_land_context", return_value=context))
                probe = stack.enter_context(patch.object(decloud, "probe_decloud_backend"))
                infer = stack.enter_context(patch.object(decloud, "_decloud_one_from_buffer"))
                stack.enter_context(
                    patch.object(decloud, "read_window_array", side_effect=counting_read)
                )
                # Old queued payload: land_id, date_from, date_to, targets, mq, months.
                result = decloud.decloud_parcel_batch.run(
                    land_id,
                    "2026-07-01",
                    "2026-07-31",
                    [{"date": "2026-07-15", "stac_cloud": 80.0}],
                    "legacy-mq",
                    [7],
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["held"], 1)
            self.assertEqual(result["waiting_cache"], 1)
            self.assertEqual(result["published"], 0)
            self.assertEqual(result["results"][0]["reason"], "waiting_cache")
            get_db.assert_not_called()
            probe.assert_not_called()
            infer.assert_not_called()
            # Readiness may peek __grid_key__ via np.load, not full stacks.
            stack_loads = [item for item in loaded if item[0] in {"S1", "S2"}]
            self.assertLessEqual(len(stack_loads), 3)
            statuses = {row["date"]: row["status"] for row in iter_pending_targets(land_id)}
            self.assertEqual(statuses["2026-07-15"], "pending")

    def test_old_message_runs_ready_target_loading_only_needed_windows(self) -> None:
        from app.core.decloud_cache import window_grid_key
        from app.core.decloud_ready import iter_pending_targets
        from app.core import decloud_cache
        from app.tasks import decloud_uncrtaints as decloud

        crs, shape, transform, mask = _grid()
        key = window_grid_key(crs, shape, transform, mask)
        land_id = "L-READY"
        context = {
            "land_meta": {"land_id": land_id, "tile_id": "T1", "land_name": land_id},
            "land_geom_geojson": {"type": "Polygon", "coordinates": []},
            "target_transform": transform,
            "target_crs": crs,
            "target_shape": shape,
            "land_mask": mask,
            "bounds": (0, 0, 1, 1),
        }
        loaded: list[tuple[str, str]] = []
        real_read = decloud_cache.read_window_array

        def counting_read(land, day, sensor, expected_grid_key=None):
            loaded.append((str(sensor), str(day)[:10]))
            return real_read(land, day, sensor, expected_grid_key=expected_grid_key)

        extra_s2 = [f"2026-06-{d:02d}" for d in range(1, 31)]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "DECLOUD_CACHE_DIR": tmp,
                "DECLOUD_CACHE_ONLY": "1",
                "DECLOUD_USE_SAR": "1",
                "DECLOUD_ENABLED": "1",
                "DECLOUD_INPUT_T": "3",
            },
        ):
            for day, cloud in (
                ("2026-07-01", 5.0),
                ("2026-07-15", 80.0),
                ("2026-07-20", 8.0),
            ):
                _write_s2(land_id, day, key, cloud=cloud)
            for day in extra_s2:
                _write_s2(land_id, day, key, cloud=4.0)
            for day in ("2026-07-01", "2026-07-15", "2026-07-20"):
                _write_s1(land_id, day, key)
            with ExitStack() as stack:
                for guard in self._network_guards(decloud):
                    stack.enter_context(guard)
                stack.enter_context(patch.object(decloud, "decloud_enabled", return_value=True))
                stack.enter_context(patch.object(decloud, "decloud_cache_only", return_value=True))
                stack.enter_context(patch.object(decloud, "_decloud_http_only", return_value=True))
                stack.enter_context(patch.object(decloud, "_land_context", return_value=context))
                stack.enter_context(patch.object(decloud, "probe_decloud_backend"))
                infer = stack.enter_context(
                    patch.object(
                        decloud,
                        "_decloud_one_from_buffer",
                        return_value={"status": "ok", "published": {"json_url": "oss://x"}},
                    )
                )
                stack.enter_context(
                    patch.object(decloud, "read_window_array", side_effect=counting_read)
                )
                result = decloud.decloud_parcel_batch.run(
                    land_id,
                    "2026-07-01",
                    "2026-07-31",
                    [{"date": "2026-07-15", "stac_cloud": 80.0}],
                    "legacy-mq",
                    [7],
                )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["published"], 1)
            self.assertEqual(result["held"], 0)
            infer.assert_called_once()
            # _decloud_one_from_buffer is mocked so only pre-load S1 happens
            # in _s1_by_date_from_cache (3 files). No 45-date S2 stack load.
            s1_loads = [item for item in loaded if item[0] == "S1"]
            s2_loads = [item for item in loaded if item[0] == "S2"]
            self.assertEqual(len(s1_loads), 3)
            self.assertLessEqual(len(s2_loads), 3)
            statuses = {row["date"]: row["status"] for row in iter_pending_targets(land_id)}
            self.assertEqual(statuses["2026-07-15"], "done")

    def test_schedule_registers_pending_instead_of_enqueueing(self) -> None:
        from app.core.decloud_ready import iter_pending_targets
        from app.tasks.decloud_uncrtaints import schedule_decloud_after_raw

        raw = [
            {
                "date": "2026-07-15",
                "scene_id": "S2X",
                "cloud_cover": 80.0,
                "parcel_cloud_cover_pct": 70.0,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "DECLOUD_CACHE_DIR": tmp,
                "DECLOUD_CACHE_ONLY": "1",
                "DECLOUD_ENABLED": "1",
                "DECLOUD_MODE": "batch",
            },
        ), patch(
            "app.tasks.decloud_uncrtaints.decloud_enabled", return_value=True
        ), patch(
            "app.tasks.decloud_uncrtaints.enqueue_decloud_parcel_batch"
        ) as enqueue, patch(
            "app.tasks.decloud_uncrtaints.enqueue_parcel_decloud"
        ) as enqueue_one:
            result = schedule_decloud_after_raw(
                land_id="L-SCHED",
                date_from="2026-07-01",
                date_to="2026-07-31",
                raw_results=raw,
                mq_task_id="t1",
                season_months=[7],
            )
            enqueue.assert_not_called()
            enqueue_one.assert_not_called()
            self.assertTrue(result.get("cache_only"))
            statuses = {
                row["date"]: row["status"] for row in iter_pending_targets("L-SCHED")
            }
            self.assertEqual(statuses.get("2026-07-15"), "pending")


if __name__ == "__main__":
    unittest.main()
