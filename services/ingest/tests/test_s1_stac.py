"""Stdlib tests for Sentinel-1 requester-pays STAC/GDAL helpers."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class S1StacTests(unittest.TestCase):
    def test_open_path_maps_s3_to_vsis3(self) -> None:
        from app.core.s1_stac import s1_open_path

        self.assertEqual(
            s1_open_path("s3://sentinel-s1-l1c/GRD/foo.tiff"),
            "/vsis3/sentinel-s1-l1c/GRD/foo.tiff",
        )
        self.assertEqual(
            s1_open_path("https://example.com/vv.tif"),
            "https://example.com/vv.tif",
        )

    def test_gdal_env_is_requester_pays_not_unsigned(self) -> None:
        from app.core.s1_stac import s1_gdal_env

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S1_AWS_ACCESS_KEY_ID", None)
            os.environ.pop("S1_AWS_SECRET_ACCESS_KEY", None)
            os.environ.pop("AWS_ACCESS_KEY_ID", None)
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            env = s1_gdal_env()
        self.assertEqual(env["AWS_NO_SIGN_REQUEST"], "NO")
        self.assertEqual(env["AWS_REQUEST_PAYER"], "requester")
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)

    def test_gdal_env_prefers_s1_keys(self) -> None:
        from app.core.s1_stac import s1_gdal_env, s1_has_aws_credentials

        with patch.dict(
            os.environ,
            {
                "S1_AWS_ACCESS_KEY_ID": "s1key",
                "S1_AWS_SECRET_ACCESS_KEY": "s1secret",
                "AWS_ACCESS_KEY_ID": "generic",
                "AWS_SECRET_ACCESS_KEY": "generic-secret",
            },
            clear=False,
        ):
            env = s1_gdal_env()
            self.assertTrue(s1_has_aws_credentials())
        self.assertEqual(env["AWS_ACCESS_KEY_ID"], "s1key")
        self.assertEqual(env["AWS_SECRET_ACCESS_KEY"], "s1secret")

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

    def test_missing_credentials_hint_mentions_requester_pays(self) -> None:
        from app.core.s1_stac import s1_missing_credentials_hint

        hint = s1_missing_credentials_hint()
        self.assertIn("requester-pays", hint)
        self.assertIn("S1_AWS_ACCESS_KEY_ID", hint)
