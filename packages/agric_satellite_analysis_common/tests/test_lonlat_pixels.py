"""Unit tests for lonlat object-list ↔ columnar encoding."""

from __future__ import annotations

import json
import math
import unittest

from agric_satellite_analysis_common.lonlat_pixels import (
    LONLAT_FORMAT_COLUMNS_V1,
    LONLAT_FORMAT_V1,
    columns_to_pixels,
    extract_pixels,
    extract_pixels_from_product,
    is_lonlat_format,
    is_lonlat_pixel_data,
    pixel_count,
    pixels_to_columns,
    replace_pixels_with_columns,
    roundtrip_ok,
    to_columnar_pixel_data,
)


class LonlatPixelsEncodeDecodeTests(unittest.TestCase):
    def test_roundtrip_preserves_count_order_and_values(self) -> None:
        pixels = [
            {
                "lon": 122.560932,
                "lat": 43.090741,
                "clear": 1,
                "NDVI": 0.212494,
                "NDMI": -0.064599,
                "NDRE": 0.125193,
                "EVI": 0.194153,
                "CIre": 0.286218,
                "MNDWI": -0.393624,
            },
            {
                "lon": 122.561032,
                "lat": 43.090841,
                "clear": 0,
                "NDVI": 0.301,
                "NDMI": 0.01,
                "NDRE": 0.2,
                "EVI": 0.25,
                "CIre": 0.3,
                "MNDWI": -0.4,
            },
            # Sparse: clear omitted, one index missing → null in column, dropped on expand.
            {
                "lon": 122.561132,
                "lat": 43.090941,
                "NDVI": 0.5,
                "EVI": 0.4,
            },
        ]
        self.assertTrue(roundtrip_ok(pixels))
        columns = pixels_to_columns(pixels)
        self.assertEqual(len(columns["lon"]), 3)
        self.assertEqual(columns["clear"][2], None)
        self.assertEqual(columns["NDMI"][2], None)
        back = columns_to_pixels(columns)
        self.assertEqual(len(back), 3)
        self.assertEqual(back[0]["NDVI"], 0.212494)
        self.assertNotIn("clear", back[2])
        self.assertNotIn("NDMI", back[2])

    def test_empty_pixels(self) -> None:
        columns = pixels_to_columns([])
        self.assertEqual(columns, {"lon": [], "lat": []})
        self.assertEqual(columns_to_pixels(columns), [])

    def test_s1_columns(self) -> None:
        pixels = [
            {"lon": 1.0, "lat": 2.0, "VV_db": -12.5, "VH_db": -18.1},
            {"lon": 1.1, "lat": 2.1, "VV_db": -11.0},
        ]
        self.assertTrue(roundtrip_ok(pixels))
        cols = pixels_to_columns(pixels)
        self.assertEqual(cols["VH_db"][1], None)

    def test_to_columnar_pixel_data_extra_meta(self) -> None:
        pixels = [{"lon": 1.0, "lat": 2.0, "NDVI": 0.3}]
        payload = to_columnar_pixel_data(
            pixels,
            extra={
                "source": "stac_direct",
                "algorithm_version": "v5",
                "format": "should_be_ignored",
                "pixels": [{"lon": 0}],
            },
        )
        self.assertEqual(payload["format"], LONLAT_FORMAT_COLUMNS_V1)
        self.assertEqual(payload["source"], "stac_direct")
        self.assertEqual(payload["algorithm_version"], "v5")
        self.assertNotIn("pixels", payload)
        self.assertEqual(pixel_count(payload), 1)

    def test_extract_pixels_legacy_and_columnar(self) -> None:
        legacy = {
            "format": LONLAT_FORMAT_V1,
            "pixels": [{"lon": 1.0, "lat": 2.0, "NDVI": 0.1}],
        }
        columnar = to_columnar_pixel_data(legacy["pixels"])
        self.assertEqual(extract_pixels(legacy), legacy["pixels"])
        self.assertEqual(extract_pixels(columnar), legacy["pixels"])
        self.assertTrue(is_lonlat_pixel_data(legacy))
        self.assertTrue(is_lonlat_pixel_data(columnar))
        self.assertTrue(is_lonlat_format(LONLAT_FORMAT_V1))
        self.assertTrue(is_lonlat_format(LONLAT_FORMAT_COLUMNS_V1))

    def test_extract_pixels_from_product_nested_and_root(self) -> None:
        pixels = [{"lon": 1.0, "lat": 2.0, "NDVI": 0.2}]
        nested = {
            "land_id": "7750",
            "pixel_data": to_columnar_pixel_data(pixels, extra={"source": "x"}),
        }
        self.assertEqual(extract_pixels_from_product(nested), pixels)
        flat_legacy = {"pixels": pixels, "pixel_count": 1}
        self.assertEqual(extract_pixels_from_product(flat_legacy), pixels)

    def test_replace_pixels_with_columns_inplace(self) -> None:
        pixel_data = {
            "format": LONLAT_FORMAT_V1,
            "source": "stac_direct",
            "pixels": [{"lon": 1.0, "lat": 2.0, "NDVI": 0.4, "clear": 1}],
        }
        replace_pixels_with_columns(pixel_data)
        self.assertEqual(pixel_data["format"], LONLAT_FORMAT_COLUMNS_V1)
        self.assertNotIn("pixels", pixel_data)
        self.assertEqual(pixel_data["source"], "stac_direct")
        self.assertEqual(extract_pixels(pixel_data)[0]["NDVI"], 0.4)

    def test_json_preserves_float_precision(self) -> None:
        """json.dumps/loads must not drop precision beyond ordinary float JSON."""
        pixels = [
            {
                "lon": 122.560932,
                "lat": 43.090741,
                "NDVI": 0.212494,
                "NDMI": -0.064599,
            }
        ]
        payload = to_columnar_pixel_data(pixels)
        encoded = json.dumps(payload, separators=(",", ":"))
        decoded = json.loads(encoded)
        back = extract_pixels(decoded)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0]["lon"], 122.560932)
        self.assertEqual(back[0]["NDVI"], 0.212494)
        self.assertTrue(math.isfinite(back[0]["NDMI"]))

    def test_column_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            columns_to_pixels({"lon": [1.0, 2.0], "lat": [3.0]})


if __name__ == "__main__":
    unittest.main()


class LonlatMigrateHelpersTests(unittest.TestCase):
    def test_needs_and_migrate_nested_v1(self) -> None:
        from agric_satellite_analysis_common.lonlat_pixels import (
            needs_columnar_migration,
            migrate_product_to_columnar,
            extract_pixels,
            LONLAT_FORMAT_COLUMNS_V1,
        )

        pixels = [
            {"lon": 122.560932, "lat": 43.090741, "clear": 1, "NDVI": 0.212494},
            {"lon": 122.561032, "lat": 43.090841, "NDVI": 0.301},
        ]
        product = {
            "land_id": "7750",
            "date": "2026-10-19",
            "sensor": "S2",
            "pixel_data": {
                "format": LONLAT_FORMAT_V1,
                "source": "stac_direct",
                "pixels": pixels,
            },
        }
        self.assertTrue(needs_columnar_migration(product))
        self.assertTrue(migrate_product_to_columnar(product))
        self.assertEqual(product["pixel_data"]["format"], LONLAT_FORMAT_COLUMNS_V1)
        self.assertNotIn("pixels", product["pixel_data"])
        self.assertEqual(product["pixel_data"]["source"], "stac_direct")
        self.assertEqual(extract_pixels(product["pixel_data"]), pixels)
        self.assertFalse(needs_columnar_migration(product))
        self.assertFalse(migrate_product_to_columnar(product))

    def test_migrate_root_pixels_offline_product(self) -> None:
        from agric_satellite_analysis_common.lonlat_pixels import (
            needs_columnar_migration,
            migrate_product_to_columnar,
            extract_pixels_from_product,
            LONLAT_FORMAT_COLUMNS_V1,
        )

        pixels = [{"lon": 1.0, "lat": 2.0, "NDVI": 0.5, "clear": 1}]
        product = {
            "land_id": "29392",
            "pixel_count": 1,
            "pixels": pixels,
        }
        self.assertTrue(needs_columnar_migration(product))
        self.assertTrue(migrate_product_to_columnar(product))
        self.assertNotIn("pixels", product)
        self.assertEqual(product["pixel_data"]["format"], LONLAT_FORMAT_COLUMNS_V1)
        self.assertEqual(extract_pixels_from_product(product), pixels)

    def test_migrate_pixel_data_preserves_float_json(self) -> None:
        from agric_satellite_analysis_common.lonlat_pixels import (
            migrate_pixel_data_to_columnar,
            extract_pixels,
        )

        pixel_data = {
            "format": LONLAT_FORMAT_V1,
            "algorithm_version": "stac-optical-lonlat-v5",
            "pixels": [
                {
                    "lon": 122.560932,
                    "lat": 43.090741,
                    "NDVI": 0.212494,
                    "NDMI": -0.064599,
                }
            ],
        }
        self.assertTrue(migrate_pixel_data_to_columnar(pixel_data))
        encoded = json.dumps(pixel_data, separators=(",", ":"))
        decoded = json.loads(encoded)
        back = extract_pixels(decoded)
        self.assertEqual(back[0]["lon"], 122.560932)
        self.assertEqual(back[0]["NDVI"], 0.212494)
        self.assertEqual(decoded["algorithm_version"], "stac-optical-lonlat-v5")

    def test_sql_helpers_alias(self) -> None:
        from agric_satellite_analysis_common.lonlat_pixels import (
            sql_lonlat_format_in,
            sql_lonlat_nonempty,
            sql_lonlat_pixel_count,
            SQL_LONLAT_FORMAT_IN,
        )

        aliased = sql_lonlat_format_in("scene.pixel_data")
        self.assertIn("scene.pixel_data", aliased)
        self.assertIn("lonlat_columns_v1", aliased)
        self.assertEqual(SQL_LONLAT_FORMAT_IN, sql_lonlat_format_in())
        self.assertIn("columns", sql_lonlat_nonempty("scene.pixel_data"))
        self.assertIn("jsonb_array_length", sql_lonlat_pixel_count("p.pixel_data"))
