"""official_stack_from_product: usable pixels even when the official 30% gate fails."""

from __future__ import annotations

import json
import unittest

from agric_satellite_analysis_common.agri_classify import (
    CLOUD_MAX_PCT,
    is_official_optical_product,
)
from agric_satellite_analysis_common.autumn_emergence import (
    MIN_CLEAR_FRACTION,
    official_stack_from_product,
)


def _pixels(n: int, *, ndvi: float = 0.18, clear: int = 1) -> list[dict]:
    return [
        {
            "lon": 115.0 + i * 1e-4,
            "lat": 34.82,
            "clear": clear,
            "NDVI": ndvi,
        }
        for i in range(n)
    ]


def _raw_product(
    day: str = "2026-11-02",
    *,
    cloud_cover: float = 41.7,
    cloud_cover_over_30: bool = True,
    pixels: list[dict] | None = None,
    extra_pixel_data: dict | None = None,
    **overrides,
) -> dict:
    pixel_data = {
        "format": "lonlat_v1",
        "source": "stac_direct",
        "pixels": pixels if pixels is not None else _pixels(8),
    }
    if extra_pixel_data:
        pixel_data.update(extra_pixel_data)
    product = {
        "land_id": "33052",
        "date": day,
        "sensor": "S2",
        "scene_id": f"S2-{day}",
        "source": "stac_direct",
        "cloud_cover": cloud_cover,
        "cloud_cover_over_30": cloud_cover_over_30,
        "parcel_cloud_cover_pct": cloud_cover,
        "parcel_cloud_source": "scl",
        "pixel_data": pixel_data,
    }
    product.update(overrides)
    return product


class OfficialStackFromProductTests(unittest.TestCase):
    def test_official_classifier_still_rejects_scene_over_30(self) -> None:
        self.assertEqual(CLOUD_MAX_PCT, 30.0)
        product = _raw_product()
        self.assertFalse(
            is_official_optical_product(
                source=product["source"],
                scene_id=product["scene_id"],
                parcel_cloud_cover_pct=product["parcel_cloud_cover_pct"],
                cloud_cover=product["cloud_cover"],
                cloud_cover_over_30=product["cloud_cover_over_30"],
                parcel_cloud_source=product["parcel_cloud_source"],
            )
        )

    def test_raw_s2_over_30_cloud_with_clear_pixels_is_stacked(self) -> None:
        # 2026-11-02 / 2026-11-14 style: official flag is false, pixels are usable.
        for day, cloud in (("2026-11-02", 41.7), ("2026-11-14", 56.0)):
            with self.subTest(day=day):
                stack, total = official_stack_from_product(
                    _raw_product(day, cloud_cover=cloud, pixels=_pixels(8))
                )
                self.assertIsNotNone(stack)
                self.assertEqual(total, 8)
                self.assertEqual(int(stack.ndvi.size), 8)
                self.assertEqual(stack.day.isoformat(), day)

    def test_clear_official_raw_s2_still_stacks(self) -> None:
        stack, total = official_stack_from_product(
            _raw_product(
                "2026-11-24",
                cloud_cover=3.76,
                cloud_cover_over_30=False,
                pixels=_pixels(8),
            )
        )
        self.assertIsNotNone(stack)
        self.assertEqual(total, 8)
        self.assertEqual(int(stack.ndvi.size), 8)

    def test_missing_date_is_skipped(self) -> None:
        product = _raw_product(cloud_cover=3.76, cloud_cover_over_30=False)
        product["date"] = None
        self.assertEqual(official_stack_from_product(product), (None, 0))

    def test_non_autumn_date_is_skipped(self) -> None:
        product = _raw_product("2026-06-01", cloud_cover=3.76, cloud_cover_over_30=False)
        self.assertEqual(official_stack_from_product(product), (None, 0))

    def test_missing_pixel_ndvi_stack_is_skipped(self) -> None:
        product = _raw_product(pixels=[])
        stack, total = official_stack_from_product(product)
        self.assertIsNone(stack)
        self.assertEqual(total, 0)

    def test_json_string_pixel_data_is_accepted(self) -> None:
        product = _raw_product()
        product["pixel_data"] = json.dumps(product["pixel_data"])
        stack, total = official_stack_from_product(product)
        self.assertIsNotNone(stack)
        self.assertEqual(total, 8)

    def test_clear_fraction_below_minimum_is_skipped(self) -> None:
        # 3/8 = 0.375 < MIN_CLEAR_FRACTION 0.50
        pixels = _pixels(3, clear=1) + _pixels(5, clear=0)
        stack, total = official_stack_from_product(_raw_product(pixels=pixels))
        self.assertIsNone(stack)
        self.assertEqual(total, 8)
        self.assertLess(3 / 8, MIN_CLEAR_FRACTION)

    def test_clear_fraction_at_minimum_is_kept(self) -> None:
        pixels = _pixels(4, clear=1) + _pixels(4, clear=0)
        stack, total = official_stack_from_product(_raw_product(pixels=pixels))
        self.assertIsNotNone(stack)
        self.assertEqual(total, 8)
        self.assertEqual(int(stack.ndvi.size), 4)

    def test_fair_and_bad_decloud_are_rejected(self) -> None:
        for quality in ("fair", "bad", None, ""):
            with self.subTest(quality=quality):
                product = _raw_product(
                    cloud_cover=56.0,
                    extra_pixel_data={
                        "source": "uncrtaints_decloud",
                        "decloud_quality": quality,
                    },
                )
                product["source"] = "uncrtaints_decloud"
                product["decloud_quality"] = quality
                product["scene_id"] = "S2-2026-11-02_decloud"
                stack, total = official_stack_from_product(product)
                self.assertIsNone(stack)
                self.assertEqual(total, 0)
                self.assertFalse(
                    is_official_optical_product(
                        source=product["source"],
                        scene_id=product["scene_id"],
                        decloud_quality=quality,
                    )
                )

    def test_good_decloud_still_stacks(self) -> None:
        product = _raw_product(
            cloud_cover=56.0,
            extra_pixel_data={
                "source": "uncrtaints_decloud",
                "decloud_quality": "good",
            },
        )
        product["source"] = "uncrtaints_decloud"
        product["decloud_quality"] = "good"
        product["scene_id"] = "S2-2026-11-02_decloud"
        stack, total = official_stack_from_product(product)
        self.assertIsNotNone(stack)
        self.assertEqual(total, 8)
        self.assertTrue(
            is_official_optical_product(
                source=product["source"],
                scene_id=product["scene_id"],
                decloud_quality="good",
            )
        )


if __name__ == "__main__":
    unittest.main()
