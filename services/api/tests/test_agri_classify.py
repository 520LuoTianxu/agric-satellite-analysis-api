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


class SceneCloudFieldsTests(unittest.TestCase):
    def test_stac_over_30_skips_parcel_metrics(self) -> None:
        cloud, over, parcel = scene_cloud_fields(42.0, 0.9)
        self.assertEqual(cloud, 42.0)
        self.assertTrue(over)
        self.assertIsNone(parcel)

    def test_parcel_from_quality_when_stac_clear(self) -> None:
        cloud, over, parcel = scene_cloud_fields(10.0, 0.8)
        self.assertEqual(cloud, 10.0)
        self.assertFalse(over)
        self.assertAlmostEqual(parcel or 0.0, 20.0, places=4)

    def test_parcel_over_30_flags_without_dropping_stac(self) -> None:
        cloud, over, parcel = scene_cloud_fields(12.0, 0.5)
        self.assertEqual(cloud, 12.0)
        self.assertTrue(over)
        self.assertAlmostEqual(parcel or 0.0, 50.0, places=4)


if __name__ == "__main__":
    unittest.main()
