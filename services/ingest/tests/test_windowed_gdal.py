"""Tests for shared windowed-COG GDAL env, JP2 skip, and TIMING helper."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class WindowedGdalTests(unittest.TestCase):
    def test_windowed_env_has_bench_winner_knobs(self) -> None:
        from app.core.windowed_gdal import windowed_cog_gdal_env

        with patch.dict(os.environ, {}, clear=False):
            for key in (
                "WINDOWED_GDAL_CURL_CHUNK_SIZE",
                "WINDOWED_GDAL_CURL_CACHE_SIZE",
                "WINDOWED_VSI_CACHE_SIZE",
                "WINDOWED_GDAL_HTTP_VERSION",
                "WINDOWED_GDAL_INGESTED_BYTES_AT_OPEN",
                "S1_GDAL_CURL_CHUNK_SIZE",
                "GDAL_HTTP_VERSION",
                "GDAL_INGESTED_BYTES_AT_OPEN",
            ):
                os.environ.pop(key, None)
            env = windowed_cog_gdal_env()
        self.assertEqual(env["GDAL_DISABLE_READDIR_ON_OPEN"], "EMPTY_DIR")
        self.assertEqual(env["GDAL_HTTP_MULTIPLEX"], "YES")
        self.assertEqual(env["GDAL_HTTP_MERGE_CONSECUTIVE_RANGES"], "YES")
        self.assertEqual(env["GDAL_HTTP_VERSION"], "2")
        self.assertEqual(env["GDAL_INGESTED_BYTES_AT_OPEN"], "32768")
        self.assertEqual(env["CPL_VSIL_CURL_CHUNK_SIZE"], "262144")
        self.assertEqual(env["CPL_VSIL_CURL_USE_HEAD"], "NO")
        self.assertEqual(env["AWS_NO_SIGN_REQUEST"], "YES")
        self.assertEqual(env["GDAL_HTTP_TIMEOUT"], "60")
        self.assertIn(".tif", env["CPL_VSIL_CURL_ALLOWED_EXTENSIONS"])

    def test_s1_prefix_honors_s1_overrides(self) -> None:
        from app.core.windowed_gdal import windowed_cog_gdal_env

        with patch.dict(
            os.environ,
            {
                "S1_GDAL_CURL_CHUNK_SIZE": "524288",
                "S1_GDAL_HTTP_VERSION": "1.1",
                "WINDOWED_GDAL_CURL_CHUNK_SIZE": "131072",
            },
            clear=False,
        ):
            env = windowed_cog_gdal_env(prefix="S1")
        self.assertEqual(env["CPL_VSIL_CURL_CHUNK_SIZE"], "524288")
        self.assertEqual(env["GDAL_HTTP_VERSION"], "1.1")

    def test_jp2_href_detection(self) -> None:
        from app.core.windowed_gdal import asset_is_non_cog_jp2, is_non_cog_jp2_href

        self.assertTrue(
            is_non_cog_jp2_href(
                "https://sentinel-cogs.s3.amazonaws.com/x/B04.jp2"
            )
        )
        self.assertTrue(
            is_non_cog_jp2_href(
                "/vsicurl/https://example.com/path/B04.JP2?x=1"
            )
        )
        self.assertFalse(
            is_non_cog_jp2_href(
                "https://sentinel-cogs.s3.amazonaws.com/x/B04.tif"
            )
        )
        self.assertTrue(
            asset_is_non_cog_jp2(
                SimpleNamespace(
                    href="https://x/a.tif",
                    media_type="image/jp2",
                )
            )
        )
        self.assertFalse(
            asset_is_non_cog_jp2(
                SimpleNamespace(href="https://x/a.tif", media_type="image/tiff")
            )
        )

    def test_log_timing_emits_prefix(self) -> None:
        from app.core.windowed_gdal import log_timing

        with self.assertLogs("openfarm.ingest.windowed_gdal", level="INFO") as cm:
            # Force stdlib path by breaking structlog import temporarily.
            with patch.dict("sys.modules", {"structlog": None}):
                # structlog import will fail → fallback logger
                log_timing(
                    "stac_search",
                    sensor="S2",
                    land_id="17329",
                    wall_ms=31200,
                    item_count=498,
                )
        joined = "\n".join(cm.output)
        self.assertIn("TIMING stage=stac_search", joined)
        self.assertIn("land_id=17329", joined)
        self.assertIn("wall_ms=31200", joined)

    def test_s1_gdal_env_delegates_to_shared(self) -> None:
        from app.core.s1_stac import s1_gdal_env

        with patch.dict(os.environ, {}, clear=False):
            for key in (
                "S1_GDAL_CURL_CHUNK_SIZE",
                "S1_GDAL_HTTP_VERSION",
                "WINDOWED_GDAL_INGESTED_BYTES_AT_OPEN",
            ):
                os.environ.pop(key, None)
            env = s1_gdal_env()
        self.assertEqual(env["GDAL_INGESTED_BYTES_AT_OPEN"], "32768")
        self.assertEqual(env["GDAL_HTTP_MULTIPLEX"], "YES")
        self.assertEqual(env["AWS_NO_SIGN_REQUEST"], "YES")


if __name__ == "__main__":
    unittest.main()
