"""Stdlib tests for decloud flag, trigger, quality scoring, and official gate."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.core.decloud import (
    DECLOUD_SOURCE,
    DecloudQualityInputs,
    decloud_backend,
    decloud_cloud_min_pct,
    decloud_enabled,
    decloud_oss_sensor,
    decloud_scene_id,
    score_decloud,
    should_trigger_decloud,
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

    def test_backend_dummy(self) -> None:
        with patch.dict(os.environ, {"DECLOUD_BACKEND": "dummy"}):
            self.assertEqual(decloud_backend(), "dummy")

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


if __name__ == "__main__":
    unittest.main()
