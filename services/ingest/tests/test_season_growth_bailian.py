"""Unit tests for Bailian client (httpx mock)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

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
        self.assertIn("evidence_bullets", out)
        self.assertIn("core_conclusion", out)
        self.assertIn("moisture_analysis", out)
        self.assertIn("causes_ranked", out)
        self.assertIn("follow_up", out)
        self.assertIn("timeline_notes", out)
        self.assertIsInstance(out["evidence_bullets"], list)
        self.assertIsInstance(out["causes_ranked"], list)
        self.assertIsInstance(out["follow_up"], list)

    def test_success_parses_json(self) -> None:
        payload = {
            "one_liner": "长势整体正常",
            "summary": "窗口内 NDVI 均值 0.55，峰值出现在 8 月。",
            "evidence_bullets": ["NDVI 均值 0.55（来自 facts）"],
            "core_conclusion": "整体正常，局部轻度水分胁迫。",
            "moisture_analysis": "干旱计数以轻度为主；无洪涝景。",
            "interpretation": "生育期绿度曲线完整。",
            "causes_ranked": ["季节性水分波动"],
            "recommendations": "保持现有管理。",
            "follow_up": ["补充田间墒情实测"],
            "timeline_notes": "8 月 NDVI 峰值与水分指标一致。",
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
                "BAILIAN_MODEL": "qwen3.8-flash",
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
        self.assertEqual(out["core_conclusion"], "整体正常，局部轻度水分胁迫。")
        self.assertIn("干旱", out["moisture_analysis"] or "")
        self.assertEqual(out["causes_ranked"], ["季节性水分波动"])
        self.assertEqual(out["follow_up"], ["补充田间墒情实测"])
        self.assertIn("8 月", out["timeline_notes"] or "")
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
        self.assertIn("causes_ranked", out)
        self.assertIn("follow_up", out)
        self.assertIsInstance(out["causes_ranked"], list)
        self.assertIsInstance(out["follow_up"], list)

    def test_normalize_lists_from_strings(self) -> None:
        out = bailian._normalize_ai(
            {
                "one_liner": "x",
                "evidence_bullets": "单条",
                "causes_ranked": "原因A",
                "follow_up": ["a", ""],
                "recommendations": ["建议1", "建议2"],
            }
        )
        self.assertEqual(out["evidence_bullets"], ["单条"])
        self.assertEqual(out["causes_ranked"], ["原因A"])
        self.assertEqual(out["follow_up"], ["a"])
        self.assertIn("建议1", out["recommendations"] or "")


if __name__ == "__main__":
    unittest.main()
