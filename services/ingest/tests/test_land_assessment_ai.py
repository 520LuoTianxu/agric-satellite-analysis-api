"""Unit tests for land-assessment AI JSON parse / fallback."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.reports.land_assessment import ai_analysis


class LandAssessmentAiTests(unittest.TestCase):
    def test_missing_key_returns_ai_fail_shell(self) -> None:
        with patch.dict(os.environ, {"BAILIAN_API_KEY": ""}, clear=False):
            os.environ.pop("BAILIAN_API_KEY", None)
            out = ai_analysis.generate_land_assessment_narrative({"field": {}})
        self.assertFalse(out["llm_configured"])
        self.assertEqual(out["error"], "missing_api_key")
        self.assertIn("AI 分析失败", out["overall"]["evaluation"])
        self.assertEqual(out["version"], 1)
        self.assertIn("score_explain", out)
        self.assertIn("rs_growth", out)
        self.assertIn("yield_potential", out)
        self.assertFalse(out["business"]["available"])
        self.assertEqual(out["business"]["note"], "数据不足")

    def test_normalize_structured_json(self) -> None:
        payload = {
            "version": 1,
            "overall": {
                "evaluation": "综合条件中等偏好，适合玉米。",
                "strengths": ["旺季绿度尚可"],
                "main_risks": ["偏碱"],
                "core_advice": ["选耐碱品种，因 pH 偏高"],
            },
            "portrait": {
                "regional_ag_traits": "华北平原夏玉米区",
                "crop_fit": "较适宜玉米",
                "limits": ["偏碱"],
            },
            "score_explain": {
                "high_dims": ["长势 85"],
                "low_dims": ["土壤 65"],
                "biggest_drivers": ["峰值 NDVI"],
                "how_to_improve": ["改良土壤酸碱"],
            },
            "rs_growth": {
                "phenology_normality": "升-峰-落基本正常",
                "anomalies": ["某年峰值偏低"],
                "ranked_causes": [
                    {"rank": 1, "cause": "可能未种植", "evidence": "峰值 NDVI<0.25"}
                ],
            },
            "spatial": {
                "watch_zones": ["低洼处"],
                "why": ["水分偏高信号"],
                "temporal_caveat": "单景不足定论",
            },
            "soil": {
                "indicators_to_farm": [
                    {
                        "indicator": "pH 7.8",
                        "farm_impact": "养分有效性下降",
                        "management": "选耐碱品种/测土",
                    }
                ]
            },
            "climate": {
                "risk_present": ["旺季干旱风险"],
                "disaster_occurred": [],
                "notes": "无成灾记录",
            },
            "management": {
                "variety_direction": "耐碱中晚熟",
                "planting_focus": ["保苗"],
                "water_fertility_watch": ["抽雄灌浆墒情"],
                "scouting": ["低洼排水"],
            },
            "yield_potential": {"level": "中", "rationale": "长势中等", "亩产": 800},
            "business": {"available": False, "note": "数据不足", "收益": 10000},
            "evidence_gaps": ["实测产量"],
            "custom_extra": {"ok": True},
        }
        out = ai_analysis.normalize_ai(payload)
        self.assertEqual(out["overall"]["strengths"], ["旺季绿度尚可"])
        self.assertEqual(out["rs_growth"]["ranked_causes"][0]["cause"], "可能未种植")
        self.assertEqual(out["yield_potential"]["level"], "中")
        self.assertNotIn("亩产", out["yield_potential"])
        self.assertFalse(out["business"]["available"])
        self.assertNotIn("收益", out["business"])
        self.assertEqual(out["custom_extra"], {"ok": True})
        self.assertIsNone(out["error"])

    def test_yield_level_strips_numbers(self) -> None:
        out = ai_analysis.normalize_ai(
            {"yield_potential": {"level": "高产约650公斤", "rationale": "x"}}
        )
        self.assertEqual(out["yield_potential"]["level"], "高")

    def test_http_error_soft_fails(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        transport = httpx.MockTransport(handler)
        client = httpx.Client(transport=transport)
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "k"}, clear=False):
            out = ai_analysis.generate_land_assessment_narrative(
                {"field": {}}, client=client
            )
        client.close()
        self.assertTrue(out["llm_configured"])
        self.assertIsNotNone(out["error"])
        self.assertIn("AI 分析失败", out["overall"]["evaluation"])

    def test_success_parses_json(self) -> None:
        payload = {
            "version": 1,
            "overall": {
                "evaluation": "适合玉米，综合中等偏好。",
                "strengths": ["长势"],
                "main_risks": ["偏碱"],
                "core_advice": ["耐碱品种因 pH 偏高"],
            },
            "portrait": {
                "regional_ag_traits": "华北",
                "crop_fit": "较适宜",
                "limits": [],
            },
            "score_explain": {
                "high_dims": ["vigor"],
                "low_dims": ["soil"],
                "biggest_drivers": ["NDVI"],
                "how_to_improve": ["测土"],
            },
            "rs_growth": {
                "phenology_normality": "正常",
                "anomalies": [],
                "ranked_causes": [],
            },
            "spatial": {
                "watch_zones": [],
                "why": [],
                "temporal_caveat": "单景不足",
            },
            "soil": {"indicators_to_farm": []},
            "climate": {
                "risk_present": [],
                "disaster_occurred": [],
                "notes": "",
            },
            "management": {
                "variety_direction": "常规",
                "planting_focus": [],
                "water_fertility_watch": [],
                "scouting": [],
            },
            "yield_potential": {"level": "中", "rationale": "无产量模型"},
            "business": {"available": False, "note": "数据不足"},
            "evidence_gaps": [],
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
                "BAILIAN_MODEL": "qwen3.7-flash",
            },
            clear=False,
        ):
            out = ai_analysis.generate_land_assessment_narrative(
                {"field": {"name": "x"}}, client=client
            )
        client.close()
        self.assertTrue(out["llm_configured"])
        self.assertIn("适合玉米", out["overall"]["evaluation"])
        self.assertEqual(out["yield_potential"]["level"], "中")
        self.assertIsNone(out["error"])

    def test_facts_for_llm_compact(self) -> None:
        facts = ai_analysis.facts_for_llm(
            field={"name": "A", "area_ha": 1.0, "crop_label": "玉米"},
            scorecard={
                "overall": {"score": 70, "grade": "较好", "light": "绿"},
                "dimensions": [
                    {
                        "key": "crop",
                        "name": "作物",
                        "score": 70,
                        "light": "绿",
                        "plain": "ok",
                        "weight": "25%",
                    }
                ],
            },
            rs={"peak_ndvi_mean": 0.7},
            risk={"period": "2024", "events": []},
            soil={"avg_ph": 7.2},
            weather_summary={},
            analysis={},
            flood_evidence=None,
        )
        self.assertEqual(facts["field"]["area_mu"], 15.0)
        self.assertIsNone(facts["yield_model"])
        self.assertIsNone(facts["business_model"])


if __name__ == "__main__":
    unittest.main()
