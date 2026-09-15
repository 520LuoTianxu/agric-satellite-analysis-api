"""Unit tests for cdfinance analyzeSoilV2 client helpers."""

from __future__ import annotations

import unittest

from app.core.cdfinance_soil import (
    geojson_to_coords_string,
    normalize_bearer,
    normalize_vendor_payload,
    parse_auth_query,
)


class CdfinanceSoilHelpersTests(unittest.TestCase):
    def test_geojson_to_coords_drops_closing_point(self):
        gj = {
            "type": "Polygon",
            "coordinates": [
                [
                    [116.3, 38.1],
                    [116.31, 38.1],
                    [116.31, 38.11],
                    [116.3, 38.11],
                    [116.3, 38.1],
                ]
            ],
        }
        s = geojson_to_coords_string(gj)
        self.assertEqual(s, "116.3,38.1|116.31,38.1|116.31,38.11|116.3,38.11")

    def test_normalize_bearer(self):
        self.assertEqual(normalize_bearer("Bearer abc"), "abc")
        self.assertEqual(normalize_bearer("abc"), "abc")
        with self.assertRaises(ValueError):
            normalize_bearer("")

    def test_parse_auth_query(self):
        q = parse_auth_query("timestamp=1&nonce=x&sv=sv01&sign=abc%3D")
        self.assertEqual(q["timestamp"], "1")
        self.assertEqual(q["sign"], "abc=")
        self.assertEqual(parse_auth_query(None), {})

    def test_normalize_vendor_payload(self):
        payload = {
            "logId": 17419,
            "indicators": [
                {
                    "name": "TN",
                    "value": 0.55,
                    "unit": "g/kg",
                    "grade": "差",
                    "name_cn": "全氮",
                },
                {
                    "name": "AP",
                    "value": 2.47,
                    "unit": "mg/kg",
                    "grade": "差",
                    "name_cn": "有效磷",
                },
                {
                    "name": "AK",
                    "value": 112.49,
                    "unit": "mg/kg",
                    "grade": "良好",
                    "name_cn": "速效钾",
                },
            ],
            "sqi": {"rating": "四等", "total_score": 48.61},
            "texture": {"usda_cn": "粘壤土"},
        }
        n = normalize_vendor_payload(payload)
        self.assertEqual(n["tn_g_kg"], 0.55)
        self.assertEqual(n["ap_mg_kg"], 2.47)
        self.assertEqual(n["ak_mg_kg"], 112.49)
        self.assertEqual(n["n"]["label"], "氮")
        self.assertEqual(n["texture_usda_cn"], "粘壤土")
        self.assertEqual(n["vendor_log_id"], 17419)


if __name__ == "__main__":
    unittest.main()


class CdfinanceSoilHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_hr_base_id_override_sent_in_headers(self):
        from unittest.mock import patch

        from app.core import cdfinance_soil as mod

        captured: dict = {}

        class FakeResp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"indicators": [], "sqi": {}, "texture": {}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def post(self, url, headers=None, content=None):
                captured["headers"] = dict(headers or {})
                return FakeResp()

            async def aclose(self):
                return None

        with (
            patch.object(mod, "httpx") as httpx_mod,
            patch.object(mod.settings, "cdfinance_hr_base_id", "37"),
            patch.object(
                mod.settings,
                "cdfinance_soil_base_url",
                "https://example.test/agric-api",
            ),
            patch.object(mod.settings, "cdfinance_app_key", "app-key"),
            patch.object(mod.settings, "cdfinance_origin", "https://origin"),
            patch.object(mod.settings, "cdfinance_referer", "https://referer"),
            patch.object(mod.settings, "cdfinance_channel_net", "H5"),
            patch.object(mod.settings, "cdfinance_soil_timeout_seconds", 5),
        ):
            httpx_mod.AsyncClient = FakeClient
            await mod.analyze_soil_v2(
                bearer_token="tok",
                body={"coords": "1,2"},
                hr_base_id="10",
            )
        self.assertEqual(captured["headers"].get("hr-base-id"), "10")
