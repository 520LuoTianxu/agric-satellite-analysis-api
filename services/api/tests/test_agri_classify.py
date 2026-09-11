"""Stdlib tests for NDDI, multi-indicator drought, S1 flood, and product pick."""

from __future__ import annotations

import unittest

from app.core.agri_classify import (
    classify_drought,
    classify_drought_scene,
    classify_drought_series,
    classify_flood,
    classify_flood_scene,
    classify_flood_series,
    optical_tooltip_fields,
    parse_s1_relative_orbit,
    pick_official_optical,
    pick_optical_for_ndvi,
    scene_cloud_fields,
)


class ClassifyDroughtPixelTests(unittest.TestCase):
    def test_healthy_canopy_is_normal_not_mild(self) -> None:
        self.assertEqual(classify_drought(0.75, 0.45), "normal")
        self.assertEqual(classify_drought(0.4, 0.25), "normal")
        self.assertEqual(classify_drought(0.6, 0.05), "severe")

    def test_nddi_bands(self) -> None:
        self.assertEqual(classify_drought(0.66, 0.34), "mild")
        self.assertEqual(classify_drought(0.72, 0.28), "moderate")
        self.assertEqual(classify_drought(0.80, 0.20), "severe")
        self.assertEqual(classify_drought(0.3, 0.5), "normal")

    def test_ndmi_fallback_is_tight(self) -> None:
        self.assertEqual(classify_drought(0.0, 0.0), "normal")
        self.assertEqual(classify_drought(None, -0.25), "severe")
        self.assertEqual(classify_drought(None, -0.05), "normal")
        self.assertIsNone(classify_drought(None, None))


class ClassifyDroughtSceneTests(unittest.TestCase):
    def test_out_of_season(self) -> None:
        cls = classify_drought_scene(
            {"date": "2026-03-10", "ndvi": 0.4, "ndmi": 0.0, "official": True},
            None,
        )
        self.assertEqual(cls, "out_of_season")

    def test_unofficial_is_unreliable(self) -> None:
        cls = classify_drought_scene(
            {"date": "2026-07-10", "ndvi": 0.4, "ndmi": 0.0, "official": False},
            None,
        )
        self.assertEqual(cls, "unreliable")

    def test_nddi_alone_is_not_drought(self) -> None:
        # High NDDI but NDMI still moist and no month baseline drop.
        cls = classify_drought_scene(
            {"date": "2026-07-10", "ndvi": 0.8, "ndmi": 0.2, "official": True},
            {"n": 1, "ndvi_median": 0.8, "ndmi_median": 0.2, "nddi_values": [0.6]},
        )
        self.assertEqual(cls, "normal")

    def test_nddi_and_ndmi_dry(self) -> None:
        cls = classify_drought_scene(
            {"date": "2026-07-10", "ndvi": 0.8, "ndmi": 0.05, "official": True},
            {
                "n": 4,
                "ndvi_median": 0.75,
                "ndmi_median": 0.25,
                "nddi_values": [0.2, 0.3, 0.4, 0.6],
            },
        )
        self.assertEqual(cls, "severe")

    def test_percentile_and_ndvi_drop(self) -> None:
        cls = classify_drought_scene(
            {"date": "2026-08-01", "ndvi": 0.58, "ndmi": 0.30, "official": True},
            {
                "n": 4,
                "ndvi_median": 0.70,
                "ndmi_median": 0.32,
                "nddi_values": [0.10, 0.12, 0.15, 0.25],
            },
        )
        self.assertEqual(cls, "moderate")

    def test_series_skips_fair_decloud_from_baseline(self) -> None:
        rows = [
            {
                "date": "2024-07-01",
                "ndvi": 0.75,
                "ndmi": 0.40,
                "official": True,
            },
            {
                "date": "2025-07-01",
                "ndvi": 0.74,
                "ndmi": 0.39,
                "official": True,
            },
            {
                "date": "2026-07-01",
                "ndvi": 0.73,
                "ndmi": 0.38,
                "official": True,
            },
            {
                "date": "2026-07-10",
                "ndvi": 0.20,
                "ndmi": 0.02,
                "official": False,
            },
        ]
        out = dict(classify_drought_series(rows))
        self.assertEqual(out["2026-07-10"], "unreliable")
        self.assertEqual(out["2026-07-01"], "normal")


class ClassifyFloodTests(unittest.TestCase):
    def test_snapshot_low_vv_with_vh_is_watch_not_unconfirmed_flood(self) -> None:
        self.assertEqual(classify_flood(-18.0, -23.0), "watch")
        self.assertEqual(classify_flood(-16.0, -21.0), "watch")

    def test_vv_vh_diff_alone_is_not_flood(self) -> None:
        self.assertEqual(classify_flood(-12.0, -20.0), "dry")
        cls = classify_flood_scene(-12.0, -20.0, -12.0, -6.0)
        self.assertEqual(cls, "dry")

    def test_series_requires_orbit_drop(self) -> None:
        # Same relative orbit (parse from S1 id). Baseline ~ -12 dB.
        def sid(abs_orbit: int) -> str:
            return (
                f"S1A_IW_GRDH_1SDV_20240701T101000_20240701T101025_"
                f"{abs_orbit:06d}_AAAAAA"
            )

        scenes = [
            {"date": "2024-06-01", "vv": -12.0, "vh": -18.0, "scene_id": sid(100)},
            {"date": "2024-06-13", "vv": -11.5, "vh": -18.2, "scene_id": sid(275)},
            {"date": "2024-06-25", "vv": -12.2, "vh": -18.5, "scene_id": sid(450)},
            {"date": "2024-07-07", "vv": -18.0, "vh": -23.0, "scene_id": sid(625)},
        ]
        out = dict(classify_flood_series(scenes))
        self.assertEqual(out["2024-06-01"], "dry")
        self.assertIn(out["2024-07-07"], ("flood_moderate", "flood_severe"))

    def test_watch_near_threshold(self) -> None:
        cls = classify_flood_scene(-15.5, -20.5, -12.0, -8.0)
        self.assertEqual(cls, "watch")


class OrbitParseTests(unittest.TestCase):
    def test_parse_s1a_relative_orbit(self) -> None:
        sid = "S1A_IW_GRDH_1SDV_20240715T221550_20240715T221615_054810_06A8F2"
        rel = parse_s1_relative_orbit(sid)
        self.assertIsInstance(rel, int)
        self.assertGreaterEqual(rel, 1)
        self.assertLessEqual(rel, 175)

    def test_prefers_explicit_relative_orbit(self) -> None:
        self.assertEqual(parse_s1_relative_orbit("nope", 42), 42)


class ProductPickTests(unittest.TestCase):
    def test_prefers_clear_raw_over_good_decloud(self) -> None:
        raw = {
            "source": "stac_direct",
            "scene_id": "stac_bridge_2026-07-01_S2",
            "cloud_cover": 8.0,
            "cloud_cover_over_30": False,
            "parcel_cloud_cover_pct": 8.0,
            "parcel_cloud_source": "scl",
            "decloud_quality": None,
        }
        decloud = {
            "source": "uncrtaints_decloud",
            "scene_id": "stac_bridge_2026-07-01_S2_decloud",
            "cloud_cover": 55.0,
            "cloud_cover_over_30": False,
            "parcel_cloud_cover_pct": 0.0,
            "decloud_quality": "good",
        }
        picked = pick_official_optical([decloud, raw])
        self.assertEqual(picked["scene_id"], raw["scene_id"])

    def test_cloudy_date_uses_good_decloud(self) -> None:
        raw = {
            "source": "stac_direct",
            "scene_id": "stac_bridge_2026-07-01_S2",
            "cloud_cover": 62.0,
            "cloud_cover_over_30": True,
            "parcel_cloud_cover_pct": 70.0,
            "parcel_cloud_source": "scl",
        }
        decloud = {
            "source": "uncrtaints_decloud",
            "scene_id": "stac_bridge_2026-07-01_S2_decloud",
            "cloud_cover": 62.0,
            "cloud_cover_over_30": False,
            "parcel_cloud_cover_pct": 0.0,
            "decloud_quality": "good",
        }
        picked = pick_official_optical([raw, decloud])
        self.assertEqual(picked["scene_id"], decloud["scene_id"])

    def test_fair_decloud_never_official(self) -> None:
        raw = {
            "source": "stac_direct",
            "scene_id": "stac_bridge_2026-07-01_S2",
            "cloud_cover": 62.0,
            "cloud_cover_over_30": True,
        }
        fair = {
            "source": "uncrtaints_decloud",
            "scene_id": "stac_bridge_2026-07-01_S2_decloud",
            "decloud_quality": "fair",
            "cloud_cover_over_30": True,
        }
        self.assertIsNone(pick_official_optical([raw, fair]))
        ndvi = pick_optical_for_ndvi([raw, fair])
        self.assertEqual(ndvi["scene_id"], raw["scene_id"])

    def test_tooltip_fields_match_picked_product(self) -> None:
        scene = {
            "source": "uncrtaints_decloud",
            "scene_id": "stac_bridge_2026-09-10_S2_decloud",
            "cloud_cover": 41.0,
            "parcel_cloud_cover_pct": 0.0,
            "parcel_cloud_source": "scl",
            "decloud_quality": "good",
            "decloud_reasons": [],
            "cloud_cover_over_30": False,
        }
        tip = optical_tooltip_fields(scene)
        self.assertEqual(tip["cloud_cover"], 0.0)
        self.assertEqual(tip["decloud_quality"], "good")
        self.assertTrue(tip["is_decloud"])
        self.assertTrue(tip["is_official"])
        self.assertEqual(tip["scene_id"], scene["scene_id"])

    def test_legacy_window_fill_tooltip_falls_back_to_stac(self) -> None:
        scene = {
            "source": "stac_direct",
            "scene_id": "stac_bridge_2026-07-19_S2",
            "cloud_cover": 27.4,
            "parcel_cloud_cover_pct": 82.45,
            "cloud_cover_over_30": True,
        }
        tip = optical_tooltip_fields(scene)
        self.assertAlmostEqual(tip["cloud_cover"], 27.4)
        self.assertTrue(tip["is_official"])

    def test_legacy_fill_prefers_clear_raw_over_decloud(self) -> None:
        raw = {
            "source": "stac_direct",
            "scene_id": "stac_bridge_2026-07-19_S2",
            "date": "2026-07-19",
            "cloud_cover": 27.4,
            "parcel_cloud_cover_pct": 82.45,
            "cloud_cover_over_30": True,
            "ndvi_avg": 0.82,
        }
        decloud = {
            "source": "uncrtaints_decloud",
            "scene_id": "stac_bridge_2026-07-19_S2_decloud",
            "date": "2026-07-19",
            "cloud_cover": 27.4,
            "parcel_cloud_cover_pct": 0.0,
            "decloud_quality": "good",
            "ndvi_avg": 0.55,
        }
        picked = pick_official_optical([raw, decloud])
        self.assertEqual(picked["scene_id"], raw["scene_id"])

    def test_closer_to_truth_picks_decloud_near_neighbors(self) -> None:
        raw = {
            "source": "stac_direct",
            "scene_id": "stac_bridge_2026-07-15_S2",
            "date": "2026-07-15",
            "cloud_cover": 22.0,
            "parcel_cloud_cover_pct": 35.0,
            "parcel_cloud_source": "scl",
            "ndvi_avg": 0.21,
            "ndmi_avg": 0.05,
        }
        decloud = {
            "source": "uncrtaints_decloud",
            "scene_id": "stac_bridge_2026-07-15_S2_decloud",
            "date": "2026-07-15",
            "cloud_cover": 22.0,
            "parcel_cloud_cover_pct": 0.0,
            "decloud_quality": "good",
            "ndvi_avg": 0.71,
            "ndmi_avg": 0.28,
        }
        neighbors = [
            {
                "source": "stac_direct",
                "scene_id": "stac_bridge_2026-07-01_S2",
                "date": "2026-07-01",
                "cloud_cover": 8.0,
                "parcel_cloud_cover_pct": 5.0,
                "parcel_cloud_source": "scl",
                "ndvi_avg": 0.70,
                "ndmi_avg": 0.30,
            },
            {
                "source": "stac_direct",
                "scene_id": "stac_bridge_2026-07-20_S2",
                "date": "2026-07-20",
                "cloud_cover": 6.0,
                "parcel_cloud_cover_pct": 4.0,
                "parcel_cloud_source": "scl",
                "ndvi_avg": 0.72,
                "ndmi_avg": 0.29,
            },
        ]
        picked = pick_official_optical([raw, decloud], neighbors=neighbors)
        self.assertEqual(picked["scene_id"], decloud["scene_id"])

    def test_closer_to_truth_tie_breaks_to_raw(self) -> None:
        raw = {
            "source": "stac_direct",
            "scene_id": "stac_bridge_2026-07-15_S2",
            "date": "2026-07-15",
            "cloud_cover": 18.0,
            "parcel_cloud_cover_pct": 28.0,
            "parcel_cloud_source": "scl",
            "ndvi_avg": 0.70,
            "ndmi_avg": 0.30,
        }
        decloud = {
            "source": "uncrtaints_decloud",
            "scene_id": "stac_bridge_2026-07-15_S2_decloud",
            "date": "2026-07-15",
            "cloud_cover": 18.0,
            "parcel_cloud_cover_pct": 0.0,
            "decloud_quality": "good",
            "ndvi_avg": 0.70,
            "ndmi_avg": 0.30,
        }
        neighbors = [
            {
                "source": "stac_direct",
                "scene_id": "stac_bridge_2026-07-01_S2",
                "date": "2026-07-01",
                "cloud_cover": 5.0,
                "parcel_cloud_cover_pct": 4.0,
                "parcel_cloud_source": "scl",
                "ndvi_avg": 0.70,
                "ndmi_avg": 0.30,
            }
        ]
        picked = pick_official_optical([raw, decloud], neighbors=neighbors)
        self.assertEqual(picked["scene_id"], raw["scene_id"])

    def test_ndvi_pick_prefers_physiological_raw_over_good_decloud(self) -> None:
        """Cloudy raw that matches neighbors beats a 'good' reconstruct that does not."""
        raw = {
            "source": "stac_direct",
            "scene_id": "stac_bridge_2026-07-15_S2",
            "date": "2026-07-15",
            "cloud_cover": 62.0,
            "parcel_cloud_cover_pct": 70.0,
            "parcel_cloud_source": "scl",
            "cloud_cover_over_30": True,
            "ndvi_avg": 0.68,
            "ndmi_avg": 0.28,
        }
        decloud = {
            "source": "uncrtaints_decloud",
            "scene_id": "stac_bridge_2026-07-15_S2_decloud",
            "date": "2026-07-15",
            "cloud_cover": 62.0,
            "parcel_cloud_cover_pct": 0.0,
            "decloud_quality": "good",
            "ndvi_avg": 0.18,
            "ndmi_avg": 0.04,
        }
        neighbors = [
            {
                "source": "stac_direct",
                "scene_id": "stac_bridge_2026-07-01_S2",
                "date": "2026-07-01",
                "cloud_cover": 8.0,
                "parcel_cloud_cover_pct": 5.0,
                "parcel_cloud_source": "scl",
                "ndvi_avg": 0.70,
                "ndmi_avg": 0.30,
            },
            {
                "source": "stac_direct",
                "scene_id": "stac_bridge_2026-07-20_S2",
                "date": "2026-07-20",
                "cloud_cover": 6.0,
                "parcel_cloud_cover_pct": 4.0,
                "parcel_cloud_source": "scl",
                "ndvi_avg": 0.72,
                "ndmi_avg": 0.29,
            },
        ]
        drought = pick_official_optical([raw, decloud], neighbors=neighbors)
        self.assertEqual(drought["scene_id"], decloud["scene_id"])
        ndvi = pick_optical_for_ndvi([raw, decloud], neighbors=neighbors)
        self.assertEqual(ndvi["scene_id"], raw["scene_id"])

    def test_ndvi_pick_uses_good_decloud_when_raw_is_cloud_dip(self) -> None:
        raw = {
            "source": "stac_direct",
            "scene_id": "stac_bridge_2026-07-15_S2",
            "date": "2026-07-15",
            "cloud_cover": 62.0,
            "parcel_cloud_cover_pct": 70.0,
            "parcel_cloud_source": "scl",
            "cloud_cover_over_30": True,
            "ndvi_avg": 0.16,
            "ndmi_avg": 0.02,
        }
        decloud = {
            "source": "uncrtaints_decloud",
            "scene_id": "stac_bridge_2026-07-15_S2_decloud",
            "date": "2026-07-15",
            "cloud_cover": 62.0,
            "parcel_cloud_cover_pct": 0.0,
            "decloud_quality": "good",
            "ndvi_avg": 0.71,
            "ndmi_avg": 0.28,
        }
        neighbors = [
            {
                "source": "stac_direct",
                "scene_id": "stac_bridge_2026-07-01_S2",
                "date": "2026-07-01",
                "cloud_cover": 8.0,
                "parcel_cloud_cover_pct": 5.0,
                "parcel_cloud_source": "scl",
                "ndvi_avg": 0.70,
                "ndmi_avg": 0.30,
            }
        ]
        ndvi = pick_optical_for_ndvi([raw, decloud], neighbors=neighbors)
        self.assertEqual(ndvi["scene_id"], decloud["scene_id"])

    def test_tooltip_fair_decloud_is_unreliable(self) -> None:
        tip = optical_tooltip_fields(
            {
                "source": "uncrtaints_decloud",
                "scene_id": "stac_bridge_2026-07-15_S2_decloud",
                "decloud_quality": "fair",
                "decloud_reasons": ["ndvi_far_below_neighbors"],
                "cloud_cover_over_30": True,
                "parcel_cloud_cover_pct": 100.0,
            }
        )
        self.assertTrue(tip["is_decloud"])
        self.assertFalse(tip["is_official"])
        self.assertTrue(tip["may_be_unreliable"])
        self.assertEqual(tip["decloud_quality"], "fair")


class SceneCloudFieldsTests(unittest.TestCase):
    def test_parcel_metric_used_for_over_30_not_stac_alone(self) -> None:
        cloud, over, parcel = scene_cloud_fields(42.0, 5.0)
        self.assertEqual(cloud, 42.0)
        self.assertFalse(over)
        self.assertEqual(parcel, 5.0)

    def test_parcel_over_30_flags_without_dropping_stac(self) -> None:
        cloud, over, parcel = scene_cloud_fields(12.0, 50.0)
        self.assertEqual(cloud, 12.0)
        self.assertTrue(over)
        self.assertEqual(parcel, 50.0)

    def test_missing_parcel_uses_stac_for_over_30(self) -> None:
        cloud, over, parcel = scene_cloud_fields(42.0, None)
        self.assertEqual(cloud, 42.0)
        self.assertTrue(over)
        self.assertIsNone(parcel)

    def test_window_fill_quality_cannot_become_parcel_cloud(self) -> None:
        # Old bug: parcel = (1 - quality_score) * 100.
        # quality 0.1755 over a padded window -> fake 82.45.
        # Second arg is now parcel %, so 0.1755 means 0.1755% cloud, not 82%.
        cloud, over, parcel = scene_cloud_fields(27.4, 0.1755)
        self.assertEqual(cloud, 27.4)
        self.assertFalse(over)
        self.assertLess(parcel or 0.0, 1.0)
        self.assertNotAlmostEqual(parcel or 0.0, 82.45, places=1)

    def test_counts_ignore_window_size(self) -> None:
        from app.core.agri_classify import parcel_cloud_from_counts

        # 50 in-polygon pixels, 0 cloudy; 280-cell padded window is irrelevant.
        self.assertEqual(parcel_cloud_from_counts(0, 50), 0.0)
        self.assertAlmostEqual(parcel_cloud_from_counts(10, 50) or 0.0, 20.0)
        self.assertIsNone(parcel_cloud_from_counts(0, 0))

    def test_lonlat_clear_pixels_are_low_cloud(self) -> None:
        from app.core.agri_classify import parcel_cloud_from_lonlat_pixels

        pixels = [{"clear": 1, "NDVI": 0.82} for _ in range(20)]
        self.assertEqual(parcel_cloud_from_lonlat_pixels(pixels), 0.0)
        pixels[0]["clear"] = 0
        self.assertAlmostEqual(parcel_cloud_from_lonlat_pixels(pixels) or 0.0, 5.0)

    def test_jiahebei_style_legacy_fill_is_not_trusted(self) -> None:
        from app.core.agri_classify import (
            effective_cloud_pct,
            parcel_cloud_is_legacy_window_fill,
        )

        self.assertTrue(parcel_cloud_is_legacy_window_fill(82.45, 27.4))
        self.assertAlmostEqual(effective_cloud_pct(82.45, 27.4) or 0.0, 27.4)
        # Trusted SCL writes are never treated as the fill artifact.
        self.assertFalse(
            parcel_cloud_is_legacy_window_fill(82.45, 27.4, parcel_cloud_source="scl")
        )
        self.assertAlmostEqual(
            effective_cloud_pct(82.45, 27.4, parcel_cloud_source="scl") or 0.0,
            82.45,
        )

    def test_clear_frac_overrides_high_legacy_parcel(self) -> None:
        from app.core.agri_classify import effective_cloud_pct

        self.assertAlmostEqual(
            effective_cloud_pct(82.45, 27.4, clear_frac=1.0) or 0.0,
            27.4,
        )


if __name__ == "__main__":
    unittest.main()
