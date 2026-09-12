"""Unit tests for Bailian client (httpx mock)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

import httpx

from app.reports.season_growth import bailian


class BailianClientTests(unittest.TestCase):
    def test_missing_key_returns_placeholder(self) -> None:
        with patch.dict(os.environ, {"BAILIAN_API_KEY": ""}, clear=False):
            os.environ.pop("BAILIAN_API_KEY", None)
            out = bailian.generate_season_narrative({"ndvi": {"mean": 0.5}})
        self.assertFalse(out["llm_configured"])
        self.assertEqual(out["error"], "missing_api_key")
        self.assertIn("未配置", out["summary"] or "")

    def test_success_parses_json(self) -> None:
        payload = {
            "one_liner": "长势整体正常",
            "summary": "窗口内 NDVI 均值 0.55，峰值出现在 8 月。",
            "evidence_bullets": ["NDVI 均值 0.55（来自 facts）"],
            "interpretation": "生育期绿度曲线完整。",
            "recommendations": "保持现有管理。",
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/chat/completions", str(request.url))
            body = {
                "choices": [
                    {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
                ]
            }
            return httpx.Response(200, json=body)

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        with patch.dict(
            os.environ,
            {
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_BASE_URL": "https://example.test/v1",
                "BAILIAN_MODEL": "qwen3.7flash",
            },
            clear=False,
        ):
            out = bailian.generate_season_narrative(
                {"ndvi": {"mean": 0.55}}, client=client
            )
        client.close()
        self.assertTrue(out["llm_configured"])
        self.assertEqual(out["one_liner"], "长势整体正常")
        self.assertIn("0.55", out["summary"] or "")
        self.assertEqual(len(out["evidence_bullets"]), 1)
        self.assertIsNone(out["error"])

    def test_http_error_soft_fails(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "k"}, clear=False):
            out = bailian.generate_season_narrative({"x": 1}, client=client)
        client.close()
        self.assertTrue(out["llm_configured"])
        self.assertIsNotNone(out["error"])
        self.assertIn("失败", out["summary"] or "")


if __name__ == "__main__":
    unittest.main()
