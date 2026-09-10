"""Stdlib tests for NDDI-primary drought classes and cloud-metric skip."""

from __future__ import annotations

import unittest

from app.core.agri_classify import (
    classify_drought,
    scene_cloud_fields,
)


class ClassifyDroughtTests(unittest.TestCase):
    def test_healthy_canopy_is_normal_not_mild(self) -> None:
        # Well-watered crop: NDVI 0.75, NDMI 0.45 → NDDI ~0.25
        self.assertEqual(classify_drought(0.75, 0.45), "normal")
        # Old mild OR (nddi >= 0.1) with modest NDDI and decent NDMI
        self.assertEqual(classify_drought(0.4, 0.25), "normal")
        # High NDDI remains severe
        self.assertEqual(classify_drought(0.6, 0.05), "severe")

    def test_nddi_bands(self) -> None:
        # Avoid binary-float edges (0.70/0.30 is 0.3999... not 0.4).
        self.assertEqual(classify_drought(0.66, 0.34), "mild")  # NDDI 0.32
        self.assertEqual(classify_drought(0.72, 0.28), "moderate")  # NDDI 0.44
        self.assertEqual(classify_drought(0.80, 0.20), "severe")  # NDDI 0.60
        self.assertEqual(classify_drought(0.3, 0.5), "normal")

    def test_ndmi_fallback_is_tight(self) -> None:
        # Denom ~0 so NDDI is None; ndmi=0 must not become mild.
        self.assertEqual(classify_drought(0.0, 0.0), "normal")
        self.assertEqual(classify_drought(None, -0.25), "severe")
        self.assertEqual(classify_drought(None, -0.05), "normal")
        self.assertIsNone(classify_drought(None, None))


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
