"""Stdlib tests for Sentinel-1 Planetary Computer STAC/GDAL helpers."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class S1StacTests(unittest.TestCase):
    def test_default_stac_is_element84_aws(self) -> None:
        from app.core.s1_stac import s1_stac_api_url, s1_uses_planetary_computer

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S1_STAC_API_URL", None)
            self.assertFalse(s1_uses_planetary_computer())
            self.assertIn("earth-search.aws.element84.com", s1_stac_api_url())

    def test_open_path_rewrites_sentinel_s1_l1c_s3_to_https_vsicurl(self) -> None:
        from app.core.s1_stac import s1_http_href, s1_open_path

        with patch.dict(
            os.environ,
            {"S1_STAC_API_URL": "https://earth-search.aws.element84.com/v1"},
            clear=False,
        ):
            self.assertEqual(
                s1_http_href("s3://sentinel-s1-l1c/GRD/foo.tiff"),
                "https://sentinel-s1-l1c.s3.amazonaws.com/GRD/foo.tiff",
            )
            self.assertEqual(
                s1_open_path("s3://sentinel-s1-l1c/GRD/foo.tiff"),
                "/vsicurl/https://sentinel-s1-l1c.s3.amazonaws.com/GRD/foo.tiff",
            )
            self.assertEqual(
                s1_open_path("https://example.com/vv.tif"),
                "/vsicurl/https://example.com/vv.tif",
            )
            # 已是 VSI 前缀时先剥前缀再解析，最终仍包一层 vsicurl。
            self.assertEqual(
                s1_open_path("/vsicurl/https://example.com/vv.tif?sig=abc"),
                "/vsicurl/https://example.com/vv.tif?sig=abc",
            )

    def test_gdal_env_is_https_friendly_not_requester_pays(self) -> None:
        from app.core.s1_stac import s1_gdal_env

        with patch.dict(os.environ, {}, clear=False):
            for key in (
                "S1_GDAL_CURL_CHUNK_SIZE",
                "S1_GDAL_CURL_CACHE_SIZE",
                "S1_VSI_CACHE_SIZE",
                "S1_GDAL_HTTP_VERSION",
                "S1_GDAL_HTTP_MULTIRANGE",
            ):
                os.environ.pop(key, None)
            env = s1_gdal_env()
        self.assertEqual(env["GDAL_DISABLE_READDIR_ON_OPEN"], "EMPTY_DIR")
        self.assertEqual(env["CPL_VSIL_CURL_USE_HEAD"], "NO")
        self.assertEqual(env["GDAL_HTTP_MERGE_CONSECUTIVE_RANGES"], "YES")
        self.assertEqual(env["GDAL_HTTP_MULTIPLEX"], "YES")
        self.assertEqual(env["GDAL_HTTP_VERSION"], "2")
        self.assertEqual(env["CPL_VSIL_CURL_CHUNK_SIZE"], "262144")
        self.assertEqual(env["GDAL_HTTP_MAX_RETRY"], "1")
        self.assertNotIn("AWS_REQUEST_PAYER", env)
        # Shared windowed Env uses anonymous AWS Open Data Range reads.
        self.assertEqual(env["AWS_NO_SIGN_REQUEST"], "YES")
        self.assertEqual(env["GDAL_INGESTED_BYTES_AT_OPEN"], "32768")

    def test_gdal_env_honors_chunk_override(self) -> None:
        from app.core.s1_stac import s1_gdal_env

        with patch.dict(
            os.environ,
            {"S1_GDAL_CURL_CHUNK_SIZE": "524288", "S1_GDAL_HTTP_VERSION": "1.1"},
            clear=False,
        ):
            env = s1_gdal_env()
        self.assertEqual(env["CPL_VSIL_CURL_CHUNK_SIZE"], "524288")
        self.assertEqual(env["GDAL_HTTP_VERSION"], "1.1")

    def test_stac_asset_href_prefers_https_alternate(self) -> None:
        from app.core.s1_stac import stac_asset_href

        asset = SimpleNamespace(
            href="s3://sentinel-s1-l1c/vv.tiff",
            extra_fields={
                "alternate": {"https": {"href": "https://cdn.example/vv.tiff"}}
            },
        )
        self.assertEqual(stac_asset_href(asset), "https://cdn.example/vv.tiff")
        self.assertEqual(
            stac_asset_href({"href": "s3://bucket/a.tif"}),
            "s3://bucket/a.tif",
        )
        self.assertIsNone(stac_asset_href(None))

    def test_access_hint_mentions_element84_aws_https(self) -> None:
        from app.core.s1_stac import s1_access_hint

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S1_STAC_API_URL", None)
            hint = s1_access_hint()
        self.assertIn("Element84", hint)
        self.assertIn("sentinel-s1-l1c", hint)

    def test_sign_s1_href_calls_pc_when_mpc(self) -> None:
        from unittest.mock import MagicMock

        from app.core import s1_stac

        fake_pc = MagicMock()
        fake_pc.sign.return_value = "https://signed/x"
        with patch.dict(
            os.environ,
            {"S1_STAC_API_URL": "https://planetarycomputer.microsoft.com/api/stac/v1"},
            clear=False,
        ):
            # CI ingest tests do not install planetary-computer; inject a stub module.
            with patch.dict("sys.modules", {"planetary_computer": fake_pc}):
                out = s1_stac.sign_s1_href("https://raw/x")
        self.assertEqual(out, "https://signed/x")
        fake_pc.sign.assert_called_once_with("https://raw/x")

    def test_staging_stac_still_uses_planetary_computer_signing(self) -> None:
        from app.core.s1_stac import s1_uses_planetary_computer, sign_s1_href

        with patch.dict(
            os.environ,
            {
                "S1_STAC_API_URL": (
                    "https://planetarycomputer-staging.microsoft.com/api/stac/v1"
                )
            },
            clear=False,
        ):
            self.assertTrue(s1_uses_planetary_computer())
            from unittest.mock import MagicMock

            from app.core import s1_stac

            fake_pc = MagicMock()
            fake_pc.sign.return_value = (
                "https://sentinel1euwest.blob.core.windows.net/s1-grd/x.xml?sig=1"
            )
            with patch.dict("sys.modules", {"planetary_computer": fake_pc}):
                out = sign_s1_href(
                    "https://sentinel1euwest.blob.core.windows.net/s1-grd/x.xml"
                )
            self.assertIn("sig=1", out)
            fake_pc.sign.assert_called_once_with(
                "https://sentinel1euwest.blob.core.windows.net/s1-grd/x.xml"
            )

    def test_sign_strips_vsicurl_before_pc_sign(self) -> None:
        from unittest.mock import MagicMock

        from app.core import s1_stac
        from app.core.s1_stac import s1_open_path, sign_s1_href

        fake_pc = MagicMock()
        fake_pc.sign.return_value = (
            "https://sentinel1euwest.blob.core.windows.net/s1-grd/vv.tif?sig=abc"
        )
        with patch.dict(
            os.environ,
            {"S1_STAC_API_URL": "https://planetarycomputer.microsoft.com/api/stac/v1"},
            clear=False,
        ):
            with patch.dict("sys.modules", {"planetary_computer": fake_pc}):
                signed = sign_s1_href(
                    "/vsicurl/https://sentinel1euwest.blob.core.windows.net/s1-grd/vv.tif"
                )
                opened = s1_open_path(
                    "/vsicurl/https://sentinel1euwest.blob.core.windows.net/s1-grd/vv.tif"
                )
        self.assertEqual(
            signed,
            "https://sentinel1euwest.blob.core.windows.net/s1-grd/vv.tif?sig=abc",
        )
        self.assertEqual(
            opened,
            "/vsicurl/https://sentinel1euwest.blob.core.windows.net/s1-grd/vv.tif?sig=abc",
        )
        fake_pc.sign.assert_called_with(
            "https://sentinel1euwest.blob.core.windows.net/s1-grd/vv.tif"
        )
