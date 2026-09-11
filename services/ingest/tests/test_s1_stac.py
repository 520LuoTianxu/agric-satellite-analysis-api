"""Stdlib tests for Sentinel-1 Planetary Computer STAC/GDAL helpers."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class S1StacTests(unittest.TestCase):
    def test_default_stac_is_planetary_computer(self) -> None:
        from app.core.s1_stac import s1_stac_api_url, s1_uses_planetary_computer

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S1_STAC_API_URL", None)
            self.assertTrue(s1_uses_planetary_computer())
            self.assertIn("planetarycomputer.microsoft.com", s1_stac_api_url())

    def test_open_path_maps_s3_to_vsis3_without_signing_non_mpc(self) -> None:
        from app.core.s1_stac import s1_open_path

        with patch.dict(
            os.environ,
            {"S1_STAC_API_URL": "https://earth-search.aws.element84.com/v1"},
            clear=False,
        ):
            self.assertEqual(
                s1_open_path("s3://sentinel-s1-l1c/GRD/foo.tiff"),
                "/vsis3/sentinel-s1-l1c/GRD/foo.tiff",
            )
            self.assertEqual(
                s1_open_path("https://example.com/vv.tif"),
                "https://example.com/vv.tif",
            )

    def test_gdal_env_is_https_friendly_not_requester_pays(self) -> None:
        from app.core.s1_stac import s1_gdal_env

        env = s1_gdal_env()
        self.assertEqual(env["GDAL_DISABLE_READDIR_ON_OPEN"], "EMPTY_DIR")
        self.assertNotIn("AWS_REQUEST_PAYER", env)
        self.assertNotIn("AWS_NO_SIGN_REQUEST", env)

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

    def test_access_hint_mentions_planetary_computer(self) -> None:
        from app.core.s1_stac import s1_access_hint

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S1_STAC_API_URL", None)
            hint = s1_access_hint()
        self.assertIn("Planetary Computer", hint)
        self.assertIn("planetary-computer", hint)

    def test_sign_s1_href_calls_pc_when_mpc(self) -> None:
        from app.core import s1_stac

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S1_STAC_API_URL", None)
            with patch("planetary_computer.sign", return_value="https://signed/x") as m:
                out = s1_stac.sign_s1_href("https://raw/x")
        self.assertEqual(out, "https://signed/x")
        m.assert_called_once_with("https://raw/x")
