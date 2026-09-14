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
        self.assertIn("未配置", out["synthesis"] or out["summary"] or "")
        self.assertIn("core_conclusion", out)
        self.assertIn("timeline_bullets", out)
        self.assertIn("factors_strong", out)
        self.assertIn("actions_now", out)
        self.assertIn("evidence_gaps", out)
        self.assertIsInstance(out["timeline_bullets"], list)
        self.assertIsInstance(out["factors_mid"], list)
        self.assertIsInstance(out["evidence_gaps"], list)

    def test_success_parses_json(self) -> None:
        payload = {
            "core_conclusion": "冠层绿度中期较高，九月回落并与干旱共现。",
            "synthesis": "官方可用景充足。峰值出现在7月。干旱提示偏高，洪涝未检出。收获信号低置信度，疑似进入成熟后期或收获准备阶段，需田间确认。峰值日期较上年提前。还缺土壤与气象资料。",
            "timeline_bullets": ["6月苗期绿度上升", "7月峰值", "9月绿度回落"],
            "monthly_notes": ["6月上升", "7月峰值", "8月维持", "9月回落"],
            "conclusions": ["冠层绿度前高后落", "干旱提示存在", "洪涝未检出"],
            "factors_strong": ["官方干旱景与九月绿度回落共现"],
            "factors_mid": ["峰值日期较上年提前"],
            "factors_weak": ["品种与播种日期未提供"],
            "actions_now": "田间确认成熟与脱水情况。",
            "actions_week": "关注墒情，不据此立即收割。",
            "actions_next_season": "补充播种与气象资料。",
            "evidence_gaps": ["实测播种日期"],
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
        self.assertIn("冠层绿度", out["core_conclusion"] or "")
        self.assertLessEqual(len(out["core_conclusion"] or ""), 90)
        self.assertIn("田间确认", out["synthesis"] or "")
        self.assertEqual(len(out["timeline_bullets"]), 3)
        self.assertEqual(out["factors_strong"][0], "官方干旱景与九月绿度回落共现")
        self.assertEqual(out["evidence_gaps"], ["实测播种日期"])
        self.assertIn("田间确认", out["actions_now"] or "")
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
        self.assertIn("失败", out["synthesis"] or out["summary"] or "")
        self.assertIn("factors_strong", out)
        self.assertIn("evidence_gaps", out)
        self.assertIsInstance(out["factors_mid"], list)
        self.assertIsInstance(out["evidence_gaps"], list)

    def test_normalize_lists_from_strings(self) -> None:
        out = bailian._normalize_ai(
            {
                "core_conclusion": "x" * 120,
                "timeline_bullets": "单条",
                "factors_mid": "原因A",
                "evidence_gaps": ["a", ""],
                "actions_now": ["建议1", "建议2"],
            }
        )
        self.assertEqual(out["timeline_bullets"], ["单条"])
        self.assertEqual(out["factors_mid"], ["原因A"])
        self.assertEqual(out["evidence_gaps"], ["a"])
        self.assertIn("建议1", out["actions_now"] or "")
        self.assertLessEqual(len(out["core_conclusion"] or ""), 90)

    def test_legacy_keys_mapped(self) -> None:
        out = bailian._normalize_ai(
            {
                "one_liner": "旧一句话",
                "summary": "旧摘要段落用于综合解读。",
                "causes_ranked": ["旧原因"],
                "follow_up": ["旧取证"],
            }
        )
        self.assertEqual(out["core_conclusion"], "旧一句话")
        self.assertIn("旧摘要", out["synthesis"] or "")
        self.assertEqual(out["factors_mid"], ["旧原因"])
        self.assertEqual(out["evidence_gaps"], ["旧取证"])

    def test_system_prompt_forbids_banned_and_english_keys(self) -> None:
        prompt = bailian.SYSTEM_PROMPT
        self.assertIn("英文字段名", prompt)
        self.assertIn("flood_scene_count", prompt)
        self.assertIn("生物量积累达标", prompt)
        self.assertIn("立即收割", prompt)
        self.assertIn("生育进程提前一个月", prompt)
        self.assertIn("疑似进入成熟后期或收获准备阶段", prompt)
        self.assertIn("core_conclusion", prompt)
        self.assertIn("synthesis", prompt)
        self.assertIn("无人机", prompt)
        self.assertIn("拔节", prompt)
        self.assertIn("actions_next_season", prompt)


    def test_sanitize_replaces_remote_ops_next_season(self) -> None:
        bad = (
            "本季遥感数据受云量影响较大，下一季可考虑增加多源卫星或无人机补测频次。"
            + '\n'
            + "结合本地积温优化播种。"
        )
        facts = {
            "drought": {
                "drought_scene_count": 5,
                "counts": {"severe": 3},
                "days": [{"date": "2026-09-01", "class": "severe"}],
            },
            "flood": {"counts": {"watch": 2}, "flood_scene_count": 0},
        }
        out = bailian._sanitize_next_season(bad, facts)
        self.assertNotIn("无人机", out or "")
        self.assertNotIn("云量", out or "")
        self.assertNotIn("多源", out or "")
        self.assertTrue(("灌溉" in (out or "")) or ("墒情" in (out or "")))

if __name__ == "__main__":
    unittest.main()
