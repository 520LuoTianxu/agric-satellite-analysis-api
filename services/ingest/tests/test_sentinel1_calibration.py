"""Sentinel-1 calibration coordinate regression tests."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from datetime import date
from unittest.mock import patch

import numpy as np


def _load_sentinel1_module():
    """Load raster processing code while stubbing the unused COG writer if absent."""
    try:
        return importlib.import_module("app.tasks.sentinel1")
    except ModuleNotFoundError as exc:
        if exc.name != "rio_cogeo":
            raise

    package = types.ModuleType("rio_cogeo")
    package.__path__ = []
    cogeo = types.ModuleType("rio_cogeo.cogeo")
    cogeo.cog_translate = lambda *args, **kwargs: None
    profiles = types.ModuleType("rio_cogeo.profiles")
    profiles.cog_profiles = {}
    with patch.dict(
        sys.modules,
        {
            "rio_cogeo": package,
            "rio_cogeo.cogeo": cogeo,
            "rio_cogeo.profiles": profiles,
        },
    ):
        return importlib.import_module("app.tasks.sentinel1")


class Sentinel1CalibrationCoordinateTests(unittest.TestCase):
    def test_radiometric_metadata_records_platform_epoch_and_processing_version(self) -> None:
        sentinel1 = _load_sentinel1_module()
        metadata = sentinel1._s1_radiometric_calibration(
            {
                "id": "S1C_IW_GRDH_1SDV_20260203T160000_20260203T160025_000100_00AAAA",
                "platform": "sentinel-1c",
                "processing_version": "003.80",
                "acquisition_datetime": "2026-02-03T16:00:00Z",
                "vv_calibration_href": "vv.xml",
                "vh_calibration_href": "vh.xml",
            }
        )

        self.assertEqual(metadata["platform"], "S1C")
        self.assertEqual(metadata["processing_version"], "003.80")
        self.assertEqual(metadata["calibration_epoch"], "s1c-auxcal-post-2026-02-03")
        self.assertEqual(
            sentinel1._s1_processing_version(
                {"processing:software": {"name": "Sentinel-1 IPF", "version": "004.03"}}
            ),
            "Sentinel-1 IPF:004.03",
        )

    def test_zero_based_pixel_centers_include_lut_edge_pixels(self) -> None:
        sentinel1 = _load_sentinel1_module()
        lut = sentinel1.S1CalibrationLUT(
            lines=np.asarray([0.0, 1.0]),
            pixels=(np.asarray([0.0, 1.0, 2.0]),) * 2,
            sigma_nought=(
                np.asarray([2.0, 3.0, 4.0]),
                np.asarray([4.0, 5.0, 6.0]),
            ),
        )

        source_power = sentinel1._calibrate_source_window_sigma0(
            np.full((2, 3), 4.0, dtype=np.float32), lut, row_offset=0, col_offset=0
        )

        expected = np.asarray(
            [[4.0, 16.0 / 9.0, 1.0], [1.0, 16.0 / 25.0, 4.0 / 9.0]],
            dtype=np.float32,
        )
        np.testing.assert_allclose(source_power, expected, rtol=1e-6, atol=1e-6)
        self.assertTrue(np.isfinite(source_power[:, [0, -1]]).all())


class UpsertAgriS1AcquiredAtTests(unittest.TestCase):
    """_upsert_agri_s1 must not NameError on acquired_at; pass acquisition_datetime."""

    def _call(self, sentinel1, acquisition_datetime):
        import agric_satellite_analysis_common.lonlat_pixels as lonlat_pixels
        import agric_satellite_analysis_common.mq_results as mq_results
        from app.core import http_mode

        captured: dict = {}

        def fake_upload(*, land_id, date_str, sensor, product):
            captured["product"] = product
            return ("oss/key.json", "https://example.com/key.json")

        meta = {
            "land_id": "26972",
            "tile_id": "tile",
            "land_name": "land",
        }
        with (
            patch.object(mq_results, "upload_scene_product_json", side_effect=fake_upload),
            patch.object(mq_results, "scene_json_oss_key", return_value="oss/key.json"),
            patch.object(
                lonlat_pixels, "to_columnar_pixel_data", return_value={"pixels": []}
            ),
            patch.object(http_mode, "cache_scene_result_http"),
        ):
            url = sentinel1._upsert_agri_s1(
                None,
                meta,
                date(2024, 1, 7),
                "S1A_item_stac",
                "26972",
                [{"lon": 110.0, "lat": 35.0, "vv": 0.1, "vh": 0.05}],
                {"mean": 0.1, "min": 0.0, "max": 1.0, "quality_score": 1.0},
                {"mean": 0.05, "min": 0.0, "max": 0.5, "quality_score": 1.0},
                result_delivery="http",
                acquisition_datetime=acquisition_datetime,
            )
        self.assertEqual(url, "https://example.com/key.json")
        return captured["product"]

    def test_acquired_at_set_from_acquisition_datetime(self) -> None:
        sentinel1 = _load_sentinel1_module()
        product = self._call(sentinel1, "2024-01-07T02:55:08.329Z")
        self.assertEqual(product["acquired_at"], "2024-01-07T02:55:08.329+00:00")

    def test_acquired_at_none_when_missing(self) -> None:
        sentinel1 = _load_sentinel1_module()
        product = self._call(sentinel1, None)
        self.assertIsNone(product["acquired_at"])


if __name__ == "__main__":
    unittest.main()


class Sentinel1CalibrationPrefetchTests(unittest.TestCase):
    def test_prefetch_s1_calibration_luts_parallel(self) -> None:
        sentinel1 = _load_sentinel1_module()
        calls: list[str] = []

        def fake_load(href: str):
            calls.append(href)
            return object()

        with patch.object(sentinel1, "_load_s1_sigma0_lut", side_effect=fake_load):
            sentinel1._prefetch_s1_calibration_luts(
                {
                    "id": "scene-1",
                    "vv_calibration_href": "vv.xml",
                    "vh_calibration_href": "vh.xml",
                }
            )
        self.assertCountEqual(calls, ["vv.xml", "vh.xml"])

    def test_prefetch_skips_when_no_calibration(self) -> None:
        sentinel1 = _load_sentinel1_module()
        with patch.object(sentinel1, "_load_s1_sigma0_lut") as load:
            sentinel1._prefetch_s1_calibration_luts({"id": "scene-2"})
        load.assert_not_called()

    def test_load_sigma0_lut_signs_before_http_get(self) -> None:
        """MPC Azure calibration XML must be SAS-signed before httpx opens it."""
        sentinel1 = _load_sentinel1_module()
        unsigned = (
            "https://sentinel1euwest.blob.core.windows.net/s1-grd/"
            "annotation/calibration/calibration-iw-vv.xml"
        )
        signed = unsigned + "?sig=test-sas"
        sentinel1._load_s1_sigma0_lut_cached.cache_clear()

        class _Resp:
            headers = {"content-length": "64"}

            def raise_for_status(self):
                return None

            def iter_bytes(self):
                # Minimal well-formed LUT XML with two vectors.
                xml = (
                    b"<cal>"
                    b"<calibrationVector><line>0</line><pixel>0 1</pixel>"
                    b"<sigmaNought>1 1</sigmaNought></calibrationVector>"
                    b"<calibrationVector><line>1</line><pixel>0 1</pixel>"
                    b"<sigmaNought>1 1</sigmaNought></calibrationVector>"
                    b"</cal>"
                )
                yield xml

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Client:
            def __init__(self):
                self.urls: list[str] = []

            def stream(self, method, url):
                self.urls.append(url)
                return _Resp()

        client = _Client()
        with patch.object(
            sentinel1, "s1_http_href", side_effect=lambda href: signed if href == unsigned else href
        ) as resolve, patch.object(sentinel1, "_s1_http", return_value=client):
            lut = sentinel1._load_s1_sigma0_lut(unsigned)
        resolve.assert_called()
        self.assertEqual(client.urls, [signed])
        self.assertEqual(len(lut.lines), 2)

    def test_load_sigma0_lut_rewrites_aws_s3_before_http_get(self) -> None:
        sentinel1 = _load_sentinel1_module()
        s3_href = (
            "s3://sentinel-s1-l1c/GRD/2024/1/8/IW/DV/scene/"
            "annotation/calibration/calibration-iw-vv.xml"
        )
        https_href = (
            "https://sentinel-s1-l1c.s3.amazonaws.com/GRD/2024/1/8/IW/DV/scene/"
            "annotation/calibration/calibration-iw-vv.xml"
        )
        sentinel1._load_s1_sigma0_lut_cached.cache_clear()

        class _Resp:
            headers = {"content-length": "64"}

            def raise_for_status(self):
                return None

            def iter_bytes(self):
                yield (
                    b"<cal>"
                    b"<calibrationVector><line>0</line><pixel>0 1</pixel>"
                    b"<sigmaNought>1 1</sigmaNought></calibrationVector>"
                    b"<calibrationVector><line>1</line><pixel>0 1</pixel>"
                    b"<sigmaNought>1 1</sigmaNought></calibrationVector>"
                    b"</cal>"
                )

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Client:
            def __init__(self):
                self.urls: list[str] = []

            def stream(self, method, url):
                self.urls.append(url)
                return _Resp()

        client = _Client()
        with patch.object(sentinel1, "_s1_http", return_value=client):
            # Use real s1_http_href from module under test.
            lut = sentinel1._load_s1_sigma0_lut(s3_href)
        self.assertEqual(client.urls, [https_href])
        self.assertEqual(len(lut.lines), 2)
