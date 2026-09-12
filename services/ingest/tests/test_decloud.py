"""Stdlib tests for decloud flag, trigger, quality scoring, and official gate."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import numpy as _numpy_probe  # noqa: F401
except ImportError:
    pass

from app.core.decloud import (
    DECLOUD_SOURCE,
    DecloudQualityInputs,
    batch_neighbors_ready,
    cloudy_targets_from_raw,
    decloud_season_months,
    filter_scenes_outside_season_high_cloud,
    date_in_decloud_season,
    decloud_backend,
    decloud_cloud_min_pct,
    decloud_drought_exclusion_flags,
    decloud_enabled,
    decloud_mode,
    decloud_oss_sensor,
    decloud_pixel_payload,
    decloud_quality_metrics,
    decloud_s2_extra_assets,
    decloud_scene_id,
    decloud_stac_cloud_max_pct,
    fallback_lonlat_pixels,
    geojson_ring_centroid,
    pick_temporal_scenes,
    plan_decloud_after_raw,
    score_decloud,
    should_enqueue_per_scene_decloud,
    should_persist_decloud_product,
    should_trigger_decloud,
    window_array_tmp_path,
)
from app.core.agri_classify import (
    is_decloud_product,
    is_official_optical_product,
    official_s2_sql,
)


class DecloudFlagTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECLOUD_ENABLED", None)
            self.assertFalse(decloud_enabled())

    def test_explicit_on(self) -> None:
        with patch.dict(os.environ, {"DECLOUD_ENABLED": "1"}):
            self.assertTrue(decloud_enabled())

    def test_cloud_min_aligns_with_drought_30(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECLOUD_CLOUD_MIN_PCT", None)
            self.assertEqual(decloud_cloud_min_pct(), 30.0)

    def test_stac_max_default_is_100(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECLOUD_STAC_CLOUD_MAX_PCT", None)
            self.assertEqual(decloud_stac_cloud_max_pct(), 100.0)

    def test_backend_dummy(self) -> None:
        with patch.dict(os.environ, {"DECLOUD_BACKEND": "dummy"}):
            self.assertEqual(decloud_backend(), "dummy")

    def test_mode_defaults_to_batch(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECLOUD_MODE", None)
            self.assertEqual(decloud_mode(), "batch")

    def test_mode_per_scene(self) -> None:
        with patch.dict(os.environ, {"DECLOUD_MODE": "per_scene"}):
            self.assertEqual(decloud_mode(), "per_scene")

    def test_extra_s2_assets_are_parcel_bands(self) -> None:
        extras = decloud_s2_extra_assets()
        self.assertIn("B01", extras)
        self.assertIn("B8A", extras)
        self.assertNotIn("B10", extras)

    def test_product_identity_is_additive(self) -> None:
        self.assertEqual(
            decloud_scene_id("2024-07-15"), "stac_bridge_2024-07-15_S2_decloud"
        )
        self.assertEqual(decloud_oss_sensor(), "S2_decloud")
        self.assertNotEqual(decloud_oss_sensor(), "S2")


class TriggerTests(unittest.TestCase):
    def test_over_30_flag_triggers(self) -> None:
        self.assertTrue(should_trigger_decloud(cloud_cover_over_30=True))

    def test_parcel_cloud_over_30_triggers(self) -> None:
        self.assertTrue(should_trigger_decloud(parcel_cloud_cover_pct=42.0))

    def test_clear_parcel_does_not_trigger(self) -> None:
        self.assertFalse(
            should_trigger_decloud(
                cloud_cover_over_30=False,
                parcel_cloud_cover_pct=12.0,
                cloud_cover=8.0,
            )
        )

    def test_stac_cloud_over_threshold(self) -> None:
        self.assertTrue(should_trigger_decloud(cloud_cover=55.0))

    def test_parcel_or_stac_above_min(self) -> None:
        self.assertTrue(
            should_trigger_decloud(parcel_cloud_cover_pct=42.0, cloud_cover=10.0)
        )
        self.assertTrue(
            should_trigger_decloud(parcel_cloud_cover_pct=10.0, cloud_cover=42.0)
        )
        self.assertFalse(
            should_trigger_decloud(
                cloud_cover_over_30=False,
                parcel_cloud_cover_pct=10.0,
                cloud_cover=12.0,
            )
        )

    def test_stac_85_and_95_and_100_trigger_over_100_does_not(self) -> None:
        self.assertTrue(should_trigger_decloud(cloud_cover=85.0))
        self.assertTrue(should_trigger_decloud(cloud_cover=95.0))
        self.assertTrue(should_trigger_decloud(cloud_cover=100.0))
        self.assertFalse(should_trigger_decloud(cloud_cover=100.1))


class QualityScoreTests(unittest.TestCase):
    def test_healthy_reconstruction_is_good(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.18,
                rgb_mean_raw=0.42,
                rgb_std=0.08,
                rgb_std_raw=0.09,
                ndvi_mean=0.62,
                neighbor_ndvi_mean=0.65,
            )
        )
        self.assertEqual(result.quality, "good")
        self.assertTrue(result.is_official)
        self.assertGreaterEqual(result.score, 0.7)
        self.assertEqual(result.reasons, [])

    def test_rgb_still_bright_is_bad(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.52,
                rgb_mean_raw=0.55,
                rgb_std=0.07,
                rgb_std_raw=0.08,
                ndvi_mean=0.4,
                neighbor_ndvi_mean=0.45,
            )
        )
        self.assertEqual(result.quality, "bad")
        self.assertFalse(result.is_official)
        self.assertIn("rgb_still_bright", result.reasons)

    def test_tiny_rgb_delta_is_not_good(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.40,
                rgb_mean_raw=0.41,
                rgb_std=0.07,
                rgb_std_raw=0.08,
                ndvi_mean=0.35,
                neighbor_ndvi_mean=0.40,
            )
        )
        self.assertIn(result.quality, ("fair", "bad"))
        self.assertFalse(result.is_official)
        self.assertIn("tiny_rgb_delta", result.reasons)

    def test_spatial_std_collapse_is_bad(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.20,
                rgb_mean_raw=0.40,
                rgb_std=0.008,
                rgb_std_raw=0.09,
                ndvi_mean=0.5,
                neighbor_ndvi_mean=0.55,
            )
        )
        self.assertEqual(result.quality, "bad")
        self.assertIn("spatial_std_collapse", result.reasons)

    def test_ndvi_far_below_neighbors_is_bad(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.18,
                rgb_mean_raw=0.40,
                rgb_std=0.07,
                rgb_std_raw=0.08,
                ndvi_mean=0.12,
                neighbor_ndvi_mean=0.60,
            )
        )
        self.assertEqual(result.quality, "bad")
        self.assertIn("ndvi_far_below_neighbors", result.reasons)

    def test_missing_neighbors_does_not_penalize(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.18,
                rgb_mean_raw=0.40,
                rgb_std=0.08,
                rgb_std_raw=0.09,
                ndvi_mean=0.55,
                neighbor_ndvi_mean=None,
            )
        )
        self.assertEqual(result.quality, "good")


class OfficialGateTests(unittest.TestCase):
    def test_raw_clear_is_official(self) -> None:
        self.assertTrue(
            is_official_optical_product(
                source="stac_direct",
                scene_id="stac_bridge_2024-07-01_S2",
                cloud_cover_over_30=False,
                parcel_cloud_cover_pct=12.0,
            )
        )

    def test_raw_cloudy_is_not_official(self) -> None:
        self.assertFalse(
            is_official_optical_product(
                source="stac_direct",
                scene_id="stac_bridge_2024-07-01_S2",
                cloud_cover_over_30=True,
                parcel_cloud_cover_pct=None,
            )
        )

    def test_good_decloud_is_official(self) -> None:
        self.assertTrue(
            is_official_optical_product(
                source=DECLOUD_SOURCE,
                scene_id="stac_bridge_2024-07-01_S2_decloud",
                decloud_quality="good",
                cloud_cover_over_30=False,
            )
        )

    def test_fair_and_bad_decloud_are_audit_only(self) -> None:
        for q in ("fair", "bad"):
            self.assertFalse(
                is_official_optical_product(
                    source=DECLOUD_SOURCE,
                    scene_id="stac_bridge_2024-07-01_S2_decloud",
                    decloud_quality=q,
                    cloud_cover_over_30=False,
                    parcel_cloud_cover_pct=0.0,
                ),
                msg=q,
            )

    def test_scene_id_suffix_detects_decloud(self) -> None:
        self.assertTrue(is_decloud_product(None, "stac_bridge_2024-07-01_S2_decloud"))
        self.assertFalse(is_decloud_product("stac_direct", "stac_bridge_2024-07-01_S2"))

    def test_sql_predicate_requires_good(self) -> None:
        sql = official_s2_sql("s")
        self.assertIn("uncrtaints_decloud", sql)
        self.assertIn("decloud_quality", sql)
        self.assertIn("'good'", sql)
        self.assertIn("_decloud", sql)


class BatchPlanTests(unittest.TestCase):
    """Raw is stored first; official cloudy-date decloud waits for neighbors."""

    _cloudy_raw = {
        "date": "2024-07-15",
        "scene_id": "stac_bridge_2024-07-15_S2",
        "cloud_cover": 55.0,
        "cloud_cover_over_30": True,
        "parcel_cloud_cover_pct": 42.0,
    }
    _clear_raw = {
        "date": "2024-07-08",
        "scene_id": "stac_bridge_2024-07-08_S2",
        "cloud_cover": 8.0,
        "cloud_cover_over_30": False,
        "parcel_cloud_cover_pct": 12.0,
    }

    def test_disabled_stores_raw_only(self) -> None:
        plan = plan_decloud_after_raw(
            enabled=False,
            mode="batch",
            raw_results=[self._cloudy_raw],
            cached_neighbor_counts={"2024-07-15": 3},
            input_t=3,
        )
        self.assertTrue(plan.store_raw)
        self.assertFalse(plan.batch)
        self.assertEqual(plan.per_scene_dates, ())
        self.assertEqual(plan.hold_decloud_dates, ())

    def test_batch_holds_decloud_until_job_buffer(self) -> None:
        plan = plan_decloud_after_raw(
            enabled=True,
            mode="batch",
            raw_results=[self._clear_raw, self._cloudy_raw],
            cached_neighbor_counts={"2024-07-15": 1, "2024-07-08": 1},
            input_t=3,
        )
        self.assertTrue(plan.store_raw)
        self.assertEqual(plan.per_scene_dates, ())
        self.assertTrue(plan.batch)
        self.assertEqual([t["date"] for t in plan.batch_targets], ["2024-07-15"])
        self.assertEqual(plan.hold_decloud_dates, ("2024-07-15",))

    def test_batch_still_waits_when_neighbors_already_cached(self) -> None:
        """Default path is one batch after all raw, not per-scene scrape."""
        plan = plan_decloud_after_raw(
            enabled=True,
            mode="batch",
            raw_results=[self._cloudy_raw],
            cached_neighbor_counts={"2024-07-15": 4},
            input_t=3,
        )
        self.assertEqual(plan.per_scene_dates, ())
        self.assertTrue(plan.batch)
        self.assertIn("2024-07-15", plan.hold_decloud_dates)

    def test_per_scene_only_when_neighbors_cached(self) -> None:
        plan = plan_decloud_after_raw(
            enabled=True,
            mode="per_scene",
            raw_results=[self._cloudy_raw],
            cached_neighbor_counts={"2024-07-15": 3},
            input_t=3,
        )
        self.assertEqual(plan.per_scene_dates, ("2024-07-15",))
        self.assertFalse(plan.batch)
        self.assertEqual(plan.hold_decloud_dates, ())

    def test_per_scene_falls_back_to_batch_without_neighbors(self) -> None:
        plan = plan_decloud_after_raw(
            enabled=True,
            mode="per_scene",
            raw_results=[self._cloudy_raw],
            cached_neighbor_counts={"2024-07-15": 1},
            input_t=3,
        )
        self.assertEqual(plan.per_scene_dates, ())
        self.assertTrue(plan.batch)
        self.assertEqual(plan.hold_decloud_dates, ("2024-07-15",))

    def test_clear_raw_is_not_a_decloud_target(self) -> None:
        targets = cloudy_targets_from_raw([self._clear_raw, self._cloudy_raw])
        self.assertEqual([t["date"] for t in targets], ["2024-07-15"])

    def test_neighbors_ready_requires_input_t(self) -> None:
        self.assertFalse(batch_neighbors_ready(2, 3))
        self.assertTrue(batch_neighbors_ready(3, 3))
        self.assertFalse(
            should_enqueue_per_scene_decloud(
                mode="batch", cached_neighbor_count=5, input_t=3
            )
        )
        self.assertTrue(
            should_enqueue_per_scene_decloud(
                mode="per_scene", cached_neighbor_count=3, input_t=3
            )
        )

    def test_pick_temporal_repeats_when_short(self) -> None:
        from datetime import date

        scenes = [
            {"date": date(2024, 7, 1), "id": "a"},
            {"date": date(2024, 7, 15), "id": "b"},
        ]
        picked = pick_temporal_scenes(scenes, date(2024, 7, 15), 3)
        self.assertEqual(len(picked), 3)
        self.assertEqual(picked[-1]["id"], "b")

    def test_pick_temporal_keeps_target_last(self) -> None:
        from datetime import date

        scenes = [
            {"date": date(2024, 7, 1), "id": "before"},
            {"date": date(2024, 7, 15), "id": "target"},
            {"date": date(2024, 7, 22), "id": "after"},
        ]
        picked = pick_temporal_scenes(scenes, date(2024, 7, 15), 3)
        self.assertEqual([p["id"] for p in picked], ["before", "after", "target"])

    def test_fair_decloud_plan_does_not_mark_official(self) -> None:
        """Fair/bad stay stored; quality gate only blocks official drought use."""
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.40,
                rgb_mean_raw=0.41,
                rgb_std=0.07,
                rgb_std_raw=0.08,
                ndvi_mean=0.35,
                neighbor_ndvi_mean=0.40,
            )
        )
        self.assertFalse(result.is_official)
        self.assertNotEqual(result.quality, "good")
        self.assertTrue(
            should_persist_decloud_product(
                has_reconstruction=True,
                pixel_count=0,
                has_finite_index=True,
            )
        )


    def test_offseason_cloudy_not_decloud_target(self) -> None:
        winter = {
            "date": "2024-12-15",
            "scene_id": "stac_bridge_2024-12-15_S2",
            "cloud_cover": 70.0,
            "cloud_cover_over_30": True,
            "parcel_cloud_cover_pct": 55.0,
        }
        targets = cloudy_targets_from_raw([winter, self._cloudy_raw])
        self.assertEqual([t["date"] for t in targets], ["2024-07-15"])

    def test_plan_skips_offseason_cloudy(self) -> None:
        winter = {
            "date": "2025-01-10",
            "scene_id": "stac_bridge_2025-01-10_S2",
            "cloud_cover": 80.0,
            "cloud_cover_over_30": True,
            "parcel_cloud_cover_pct": 60.0,
        }
        plan = plan_decloud_after_raw(
            enabled=True,
            mode="batch",
            raw_results=[winter, self._cloudy_raw],
            cached_neighbor_counts={},
            input_t=3,
        )
        self.assertEqual([t["date"] for t in plan.batch_targets], ["2024-07-15"])

    def test_filter_scenes_drops_offseason_high_cloud(self) -> None:
        scenes = [
            {"date": "2024-07-15", "cloud_cover": 80.0},
            {"date": "2024-12-01", "cloud_cover": 80.0},
            {"date": "2024-12-08", "cloud_cover": 10.0},
        ]
        kept, skipped = filter_scenes_outside_season_high_cloud(
            scenes, season_months=(6, 7, 8, 9)
        )
        self.assertEqual(skipped, 1)
        self.assertEqual([s["date"] for s in kept], ["2024-07-15", "2024-12-08"])



class PersistProductTests(unittest.TestCase):
    """Fair/bad reconstructions are stored; drought still skips them."""

    def test_persist_when_reconstruction_exists_even_without_pixels(self) -> None:
        self.assertTrue(
            should_persist_decloud_product(
                has_reconstruction=True, pixel_count=0, has_finite_index=False
            )
        )
        self.assertFalse(
            should_persist_decloud_product(
                has_reconstruction=False, pixel_count=0, has_finite_index=False
            )
        )

    def test_fair_bad_drought_flags_keep_cloud_over_30(self) -> None:
        over_30, parcel = decloud_drought_exclusion_flags(False)
        self.assertTrue(over_30)
        self.assertGreaterEqual(parcel, 30.0)
        self.assertFalse(
            is_official_optical_product(
                source=DECLOUD_SOURCE,
                scene_id="stac_bridge_2024-07-01_S2_decloud",
                decloud_quality="fair",
                cloud_cover_over_30=over_30,
                parcel_cloud_cover_pct=parcel,
            )
        )
        self.assertFalse(
            is_official_optical_product(
                source=DECLOUD_SOURCE,
                scene_id="stac_bridge_2024-07-01_S2_decloud",
                decloud_quality="bad",
                cloud_cover_over_30=True,
                parcel_cloud_cover_pct=100.0,
            )
        )

    def test_good_decloud_flags_do_not_invent_zero_parcel(self) -> None:
        over_30, parcel = decloud_drought_exclusion_flags(True)
        self.assertFalse(over_30)
        self.assertIsNone(parcel)
        self.assertTrue(
            is_official_optical_product(
                source=DECLOUD_SOURCE,
                scene_id="stac_bridge_2024-07-01_S2_decloud",
                decloud_quality="good",
                cloud_cover_over_30=over_30,
                parcel_cloud_cover_pct=parcel,
                cloud_cover=62.0,
            )
        )

    def test_pixel_payload_includes_quality_and_reasons(self) -> None:
        payload = decloud_pixel_payload(
            quality="bad",
            score=0.2,
            reasons=["ndvi_far_below_neighbors"],
            raw_scene_id="stac_bridge_2024-07-01_S2",
            pixels=[{"lon": 1.0, "lat": 2.0, "NDVI": 0.1, "clear": 0}],
        )
        self.assertEqual(payload["decloud_quality"], "bad")
        self.assertEqual(payload["decloud_reasons"], ["ndvi_far_below_neighbors"])
        self.assertEqual(payload["source"], DECLOUD_SOURCE)
        self.assertEqual(payload["pixels"][0]["NDVI"], 0.1)

    def test_pixel_payload_stores_quality_metrics_for_audit(self) -> None:
        metrics = decloud_quality_metrics(
            DecloudQualityInputs(
                rgb_mean=0.12,
                rgb_mean_raw=0.40,
                rgb_std=0.05,
                rgb_std_raw=0.08,
                ndvi_mean=0.0,
                neighbor_ndvi_mean=0.79,
            )
        )
        self.assertEqual(metrics["ndvi_mean"], 0.0)
        self.assertEqual(metrics["neighbor_ndvi_mean"], 0.79)
        self.assertEqual(metrics["ndvi_gap"], 0.79)
        payload = decloud_pixel_payload(
            quality="bad",
            score=0.6,
            reasons=["ndvi_far_below_neighbors"],
            raw_scene_id="stac_bridge_2024-07-01_S2",
            pixels=[{"lon": 1.0, "lat": 2.0, "NDVI": 0.0, "clear": 0}],
            metrics=metrics,
        )
        self.assertEqual(payload["decloud_metrics"]["ndvi_gap"], 0.79)
        # fair/bad still carry pixel values for non-drought use
        self.assertEqual(payload["pixels"][0]["NDVI"], 0.0)

    def test_fallback_pixels_from_zonal_means(self) -> None:
        pixels = fallback_lonlat_pixels(
            pixels=[],
            index_avgs={"NDVI": 0.12, "NDMI": 0.02},
            lon=116.4,
            lat=39.9,
        )
        self.assertEqual(len(pixels), 1)
        self.assertEqual(pixels[0]["NDVI"], 0.12)
        stub = fallback_lonlat_pixels(
            pixels=[],
            index_avgs={},
            lon=116.4,
            lat=39.9,
            allow_zero_stub=True,
        )
        self.assertEqual(stub[0]["NDVI"], 0.0)

    def test_geojson_centroid(self) -> None:
        geom = {
            "type": "Polygon",
            "coordinates": [
                [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]
            ],
        }
        lon, lat = geojson_ring_centroid(geom)
        self.assertAlmostEqual(lon, 0.8)
        self.assertAlmostEqual(lat, 0.8)


class WindowCacheTests(unittest.TestCase):
    def test_catalog_roundtrip_and_neighbor_count(self) -> None:
        import tempfile
        from datetime import date

        from app.core.decloud_cache import (
            get_window_meta,
            neighbor_counts_for_dates,
            put_window_meta,
            usable_s2_count,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DECLOUD_CACHE_DIR": tmp}):
                put_window_meta(
                    land_id="13691",
                    date_str="2024-07-08",
                    sensor="S2",
                    cloud_cover=10.0,
                    stac_id="s2-a",
                    band_hrefs={"B04": "s3://x/B04.tif"},
                )
                put_window_meta(
                    land_id="13691",
                    date_str=date(2024, 7, 15),
                    sensor="S2",
                    cloud_cover=55.0,
                    band_hrefs={"B04": "s3://x/B04b.tif"},
                )
                put_window_meta(
                    land_id="13691",
                    date_str="2024-07-15",
                    sensor="S1",
                    band_hrefs={"vv": "s3://x/vv.tif"},
                )
                got = get_window_meta("13691", "2024-07-15", "S2")
                self.assertIsNotNone(got)
                self.assertEqual(got["cloud_cover"], 55.0)
                self.assertEqual(usable_s2_count("13691", "2024-07-15", 45), 2)
                counts = neighbor_counts_for_dates(
                    "13691", ["2024-07-15"], lookback_days=45
                )
                self.assertGreaterEqual(counts["2024-07-15"], 2)

    def test_missing_hrefs_without_array_are_not_usable(self) -> None:
        import tempfile

        from app.core.decloud_cache import list_cached_s2, put_window_meta

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DECLOUD_CACHE_DIR": tmp}):
                put_window_meta(
                    land_id="1",
                    date_str="2024-01-01",
                    sensor="S2",
                    has_array=False,
                )
                self.assertEqual(list_cached_s2("1"), [])

    def test_npz_tmp_path_still_ends_with_npz(self) -> None:
        path = Path("/tmp/cache/2024-07-15_S2.npz")
        tmp = window_array_tmp_path(path)
        self.assertTrue(str(tmp).endswith(".npz"))
        self.assertNotEqual(tmp, path)
        self.assertFalse(str(tmp).endswith(".npz.tmp"))
        self.assertIn(".writing.npz", tmp.name)
        self.assertIn(str(os.getpid()), tmp.name)

    def test_write_window_array_roundtrip_no_double_suffix(self) -> None:
        """savez must receive a .npz name so numpy does not write *.npz.tmp.npz."""
        import json
        import sys
        import tempfile
        from types import ModuleType

        class _NpzFile:
            def __init__(self, data: dict) -> None:
                self.files = list(data)
                self._data = data

            def __enter__(self) -> "_NpzFile":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def __getitem__(self, key: str) -> object:
                return self._data[key]

        savez_paths: list[str] = []

        def savez_compressed(file: object, **packed: object) -> None:
            target = Path(os.fspath(file))
            savez_paths.append(str(target))
            if not str(target).endswith(".npz"):
                target = Path(str(target) + ".npz")
            target.write_text(json.dumps(packed), encoding="utf-8")

        def load(file: object) -> _NpzFile:
            return _NpzFile(
                json.loads(Path(os.fspath(file)).read_text(encoding="utf-8"))
            )

        fake_np = ModuleType("numpy")
        fake_np.asarray = lambda v: v  # type: ignore[attr-defined]
        fake_np.savez_compressed = savez_compressed  # type: ignore[attr-defined]
        fake_np.load = load  # type: ignore[attr-defined]

        from app.core.decloud_cache import (
            array_path,
            read_window_array,
            write_window_array,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DECLOUD_CACHE_DIR": tmp}):
                with patch.dict(sys.modules, {"numpy": fake_np}):
                    path = write_window_array(
                        "4745", "2026-09-02", "S2", stack=[0.1, 0.2]
                    )
                    self.assertIsNotNone(path)
                    assert path is not None
                    expected = array_path("4745", "2026-09-02", "S2")
                    self.assertEqual(path, expected)
                    self.assertTrue(path.is_file())
                    self.assertEqual(path.name, "2026-09-02_S2.npz")
                    self.assertTrue(savez_paths)
                    self.assertTrue(savez_paths[0].endswith(".npz"))
                    self.assertNotIn(".npz.tmp", Path(savez_paths[0]).name)
                    self.assertIn(".writing.npz", Path(savez_paths[0]).name)

                    names = [p.name for p in Path(tmp).rglob("*") if p.is_file()]
                    self.assertNotIn("2026-09-02_S2.npz.tmp", names)
                    self.assertNotIn("2026-09-02_S2.npz.tmp.npz", names)
                    self.assertFalse(
                        any(
                            n.endswith(".npz.tmp")
                            or n.endswith(".npz.tmp.npz")
                            or n.endswith(".writing.npz")
                            for n in names
                        )
                    )
                    self.assertFalse(any(".tmp.npz" in n for n in names))

                    got = read_window_array("4745", "2026-09-02", "S2")
                    self.assertIsNotNone(got)
                    self.assertEqual(got["stack"], [0.1, 0.2])

    def test_write_window_array_cleans_temp_on_failure(self) -> None:
        import sys
        import tempfile
        from types import ModuleType

        def savez_compressed(file: object, **_packed: object) -> None:
            Path(os.fspath(file)).write_text("partial", encoding="utf-8")
            raise OSError("simulated write failure")

        fake_np = ModuleType("numpy")
        fake_np.asarray = lambda v: v  # type: ignore[attr-defined]
        fake_np.savez_compressed = savez_compressed  # type: ignore[attr-defined]

        from app.core.decloud_cache import array_path, write_window_array

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DECLOUD_CACHE_DIR": tmp}):
                with patch.dict(sys.modules, {"numpy": fake_np}):
                    with self.assertRaises(OSError):
                        write_window_array("4745", "2026-09-02", "S2", stack=[1])
                    names = [p.name for p in Path(tmp).rglob("*") if p.is_file()]
                    self.assertFalse(
                        any(".tmp" in n or n.endswith(".writing.npz") for n in names)
                    )
                    self.assertFalse(
                        array_path("4745", "2026-09-02", "S2").is_file()
                    )

    def test_write_window_array_roundtrip_when_numpy_available(self) -> None:
        import tempfile

        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy missing")
        if not hasattr(np, "savez_compressed"):
            self.skipTest("numpy stub has no savez_compressed")

        from app.core.decloud_cache import read_window_array, write_window_array

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"DECLOUD_CACHE_DIR": tmp}):
                arr = np.zeros((2, 3, 3), dtype="float32")
                path = write_window_array("42", "2024-07-15", "S2", stack=arr)
                self.assertIsNotNone(path)
                self.assertTrue(str(path).endswith(".npz"))
                self.assertFalse(str(path).endswith(".npz.tmp.npz"))
                loaded = read_window_array("42", "2024-07-15", "S2")
                self.assertIsNotNone(loaded)
                self.assertIn("stack", loaded)


class UncrtaintsCheckpointTests(unittest.TestCase):
    """Loader tests with fake weights. CI ingest has no torch/numpy extras."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._added_modules = _stub_optional_modules()
        from app.core import uncrtaints as uncrtaints_mod

        cls.u = uncrtaints_mod

    def test_strip_netg_prefix_drops_wrapper_keys(self) -> None:
        stripped = self.u._strip_netg_prefix(
            {
                "netG.in_conv.weight": 1,
                "netG.out_block.0.weight": 2,
                "module.netG.in_block.0.weight": 3,
                "criterion.foo": 9,
            }
        )
        self.assertEqual(
            stripped,
            {
                "in_conv.weight": 1,
                "out_block.0.weight": 2,
                "in_block.0.weight": 3,
            },
        )

    def test_rename_in_out_blocks_digit_minus_one(self) -> None:
        renamed = self.u._rename_in_out_blocks(
            {"in_block1.conv.weight": 1, "out_block1.proj.bias": 2, "in_conv.weight": 3}
        )
        self.assertEqual(
            renamed,
            {
                "in_block.0.conv.weight": 1,
                "out_block.0.proj.bias": 2,
                "in_conv.weight": 3,
            },
        )

    def test_resolve_prefers_pth_tar_under_experiment_dir(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exp = root / "diagonal_1"
            exp.mkdir()
            (exp / "other.pth").write_bytes(b"pth")
            target = exp / "model.pth.tar"
            target.write_bytes(b"tar")
            with patch.dict(
                os.environ,
                {
                    "UNCRTAINTS_CHECKPOINT_DIR": str(root),
                    "UNCRTAINTS_CHECKPOINT_NAME": "diagonal_1",
                },
            ):
                self.assertEqual(self.u._resolve_checkpoint_file(), target)
            with patch.dict(
                os.environ,
                {
                    "UNCRTAINTS_CHECKPOINT_DIR": str(exp),
                    "UNCRTAINTS_CHECKPOINT_NAME": "diagonal_1",
                },
            ):
                self.assertEqual(self.u._resolve_checkpoint_file(), target)
            with patch.dict(
                os.environ,
                {"UNCRTAINTS_CHECKPOINT_DIR": str(target)},
            ):
                self.assertEqual(self.u._resolve_checkpoint_file(), target)

    def test_load_strips_netg_and_reads_conf(self) -> None:
        import json
        import sys
        import tempfile
        import types
        from pathlib import Path

        class FakeGenerator:
            last_kwargs: dict = {}
            last_state: dict = {}
            load_calls: list = []

            def __init__(self, **kwargs):
                type(self).last_kwargs = kwargs
                self.expected = {"in_conv.weight", "out_block.0.weight"}

            def load_state_dict(self, state, strict=False):
                type(self).load_calls.append(dict(state))
                type(self).last_state = dict(state)
                missing = [k for k in self.expected if k not in state]
                unexpected = [k for k in state if k not in self.expected]
                return missing, unexpected

            def to(self, device):
                return self

            def eval(self):
                return self

        FakeGenerator.load_calls = []

        class FakeTorch:
            @staticmethod
            def load(path, map_location=None):
                del path, map_location
                return {
                    "epoch": 12,
                    "state_dict": {
                        "netG.in_conv.weight": 1,
                        "netG.out_block.0.weight": 2,
                        "criterion.loss.weight": 9,
                    },
                }

        src_mod = types.ModuleType("src")
        bb_mod = types.ModuleType("src.backbones")
        u_mod = types.ModuleType("src.backbones.uncrtaints")
        u_mod.UNCRTAINTS = FakeGenerator
        extra = {
            "torch": FakeTorch,
            "src": src_mod,
            "src.backbones": bb_mod,
            "src.backbones.uncrtaints": u_mod,
        }
        with tempfile.TemporaryDirectory() as tmp:
            exp = Path(tmp) / "diagonal_1"
            exp.mkdir()
            (exp / "model.pth.tar").write_bytes(b"fake")
            (exp / "conf.json").write_text(
                json.dumps(
                    {
                        "encoder_widths": "[128]",
                        "decoder_widths": "[128,128,128,128,128]",
                        "out_conv": "[13]",
                        "mean_nonLinearity": True,
                        "var_nonLinearity": "softplus",
                        "agg_mode": "att_group",
                        "encoder_norm": "group",
                        "decoder_norm": "batch",
                        "n_head": 16,
                        "d_model": 256,
                        "d_k": 4,
                        "pad_value": 0,
                        "padding_mode": "reflect",
                        "positional_encoding": True,
                        "covmode": "diag",
                        "scale_by": 10.0,
                        "separate_out": False,
                        "use_v": False,
                        "block_type": "mbconv",
                        "pretrain": False,
                    }
                )
            )
            env = {
                "UNCRTAINTS_CHECKPOINT_DIR": str(exp),
                "UNCRTAINTS_CHECKPOINT_NAME": "diagonal_1",
                "UNCRTAINTS_HOME": "",
                "DECLOUD_USE_SAR": "1",
                "DECLOUD_INPUT_T": "3",
            }
            with patch.dict(sys.modules, extra), patch.dict(os.environ, env):
                infer = self.u.UncrtainTSInferencer(device="cpu")

        self.assertIsInstance(infer.model, FakeGenerator)
        self.assertEqual(FakeGenerator.last_kwargs["encoder_widths"], [128])
        self.assertEqual(FakeGenerator.last_kwargs["out_conv"], [26])
        self.assertEqual(FakeGenerator.last_kwargs["block_type"], "mbconv")
        self.assertFalse(FakeGenerator.last_kwargs["is_mono"])
        self.assertEqual(
            FakeGenerator.last_state,
            {"in_conv.weight": 1, "out_block.0.weight": 2},
        )
        self.assertTrue(
            all(not k.startswith("netG.") for k in FakeGenerator.last_state)
        )
        self.assertEqual(len(FakeGenerator.load_calls), 1)

    def test_in_block_rename_fallback_loads_cleanly(self) -> None:
        class FakeGenerator:
            def __init__(self):
                self.expected = {"in_block.0.conv.weight"}

            def load_state_dict(self, state, strict=False):
                missing = [k for k in self.expected if k not in state]
                unexpected = [k for k in state if k not in self.expected]
                return missing, unexpected

        warnings: list[tuple] = []

        def _warn(*args, **kwargs):
            warnings.append((args, kwargs))

        with patch.object(self.u.logger, "warning", _warn):
            missing, unexpected = self.u._load_state_into_generator(
                FakeGenerator(),
                {"netG.in_block1.conv.weight": 1},
                Path("model.pth.tar"),
            )
        self.assertEqual(missing, [])
        self.assertEqual(unexpected, [])
        self.assertEqual(warnings, [])


def _stub_optional_modules() -> list[str]:
    """CI ingest job does not install numpy/structlog; stub only if missing."""
    import sys
    import types

    added: list[str] = []
    if "numpy" not in sys.modules:
        numpy_mod = types.ModuleType("numpy")
        numpy_mod.ndarray = type("ndarray", (), {})  # type: ignore[attr-defined]
        numpy_mod.float32 = float  # type: ignore[attr-defined]
        sys.modules["numpy"] = numpy_mod
        added.append("numpy")
    if "structlog" not in sys.modules:
        structlog_mod = types.ModuleType("structlog")

        class _Log:
            def warning(self, *args, **kwargs):
                return None

            def info(self, *args, **kwargs):
                return None

        structlog_mod.get_logger = lambda: _Log()  # type: ignore[attr-defined]
        sys.modules["structlog"] = structlog_mod
        added.append("structlog")
    return added


if __name__ == "__main__":
    unittest.main()
