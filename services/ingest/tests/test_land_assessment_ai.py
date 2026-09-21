"""Unit tests for land-assessment AI JSON parse / fallback / parallel / anomaly schema."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.reports.land_assessment import ai_analysis
from app.reports.land_assessment.charts import estimate_emergence
from app.reports.land_assessment.soil_labels import (
    soil_drainage_zh,
    soil_texture_zh,
    translate_soil_jargon,
)


class SoilZhMappingTests(unittest.TestCase):
    def test_texture_and_drainage(self) -> None:
        self.assertEqual(soil_texture_zh("clay loam"), "黏壤土")
        self.assertEqual(soil_texture_zh("Clay Loam"), "黏壤土")
        self.assertEqual(soil_drainage_zh("well drained"), "排水良好")
        self.assertEqual(soil_drainage_zh("Well drained"), "排水良好")
        self.assertEqual(soil_texture_zh("粉壤土"), "粉壤土")

    def test_translate_free_text(self) -> None:
        s = translate_soil_jargon("Clay loam + Well drained; Rootzone AWC 164 mm")
        self.assertIn("黏壤土", s)
        self.assertIn("排水良好", s)
        self.assertIn("根系层有效持水量(mm)", s)
        self.assertNotIn("clay loam", s.lower())
        self.assertNotIn("well drained", s.lower())


class EmergenceEstimateTests(unittest.TestCase):
    def test_estimates_from_ndvi_rise(self) -> None:
        # 起升观测与实际出苗不同；保留相邻低值和高值给出的不确定区间。
        by_date = {
            "2025-05-20": {"NDVI": 0.18},
            "2025-06-01": {"NDVI": 0.17},
            "2025-06-10": {"NDVI": 0.19},
            "2025-06-18": {"NDVI": 0.28},
            "2025-06-25": {"NDVI": 0.35},
            "2025-07-05": {"NDVI": 0.48},
            "2025-08-01": {"NDVI": 0.82},
        }
        em = estimate_emergence(by_date, 2025)
        self.assertEqual(em["method"], "observed_greenup")
        self.assertIsNotNone(em["date"])
        self.assertTrue(str(em["date"]).startswith("2025-06"))
        self.assertIn("实际出苗日需现场记录确认", em["note_zh"])
        self.assertEqual(em["interval"], ["2025-06-18", "2025-06-25"])

    def test_insufficient_data(self) -> None:
        em = estimate_emergence({"2025-08-01": {"NDVI": 0.8}}, 2025)
        self.assertEqual(em["method"], "insufficient")
        self.assertIsNone(em["date"])
        self.assertIn("依据不足", em["note_zh"])


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
        self.assertIsNone(out.get("ai_reference_score"))

    def test_normalize_anomaly_card_schema(self) -> None:
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
                "anomalies": [
                    {
                        "event_id": "E2",
                        "problem": "拔节前后绿度明显偏低",
                        "likely_cause": "天气",
                        "basis": "均NDVI≈0.265，对照P30=0.322；6月降水偏少",
                        "confidence": "中",
                    }
                ],
                "ranked_causes": [
                    {"rank": 1, "cause": "可能干旱", "evidence": "降水偏少"}
                ],
            },
            "spatial": {
                "watch_zones": [{"zone": "低洼处", "note": "偏湿信号"}],
                "why": ["水分偏高信号"],
                "temporal_caveat": "单景不足定论",
            },
            "soil": {
                "indicators_to_farm": [
                    {
                        "indicator": "clay loam + well drained",
                        "farm_impact": "质地中等",
                        "management": "保墒",
                    },
                    {
                        "indicator": "Rootzone AWC 164",
                        "farm_impact": "持水中等",
                        "management": "及时灌溉",
                    },
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
        cards = out["rs_growth"]["anomalies"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["event_id"], "E2")
        self.assertEqual(cards[0]["problem"], "拔节前后绿度明显偏低")
        self.assertEqual(cards[0]["likely_cause"], "天气")
        self.assertEqual(cards[0]["confidence"], "中")
        self.assertIn("低洼处", out["spatial"]["watch_zones"][0])
        soil_inds = [r["indicator"] for r in out["soil"]["indicators_to_farm"]]
        self.assertTrue(any("黏壤土" in x for x in soil_inds))
        self.assertTrue(any("根系层有效持水量" in x for x in soil_inds))
        self.assertFalse(any("clay loam" in x.lower() for x in soil_inds))
        self.assertEqual(out["yield_potential"]["level"], "中")
        self.assertNotIn("亩产", out["yield_potential"])
        self.assertFalse(out["business"]["available"])
        self.assertNotIn("收益", out["business"])
        self.assertEqual(out["custom_extra"], {"ok": True})
        self.assertIsNone(out["error"])

    def test_spatial_empty_zones_with_why_is_no_hotspot(self) -> None:
        out = ai_analysis.normalize_ai(
            {
                "overall": {"evaluation": "ok", "strengths": ["a"]},
                "spatial": {
                    "watch_zones": [],
                    "why": ["全地块同步，未见斑块"],
                    "temporal_caveat": "单景不足",
                },
            }
        )
        self.assertTrue(out["spatial"]["no_hotspot"])
        self.assertEqual(out["spatial"]["watch_zones"], [])
        self.assertTrue(out["spatial"]["why"])

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
                {"field": {}}, client=client, parallel=False
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
                "ai_reference_score": 72.0,
                "ai_reference_grade": "较好",
                "ai_reference_light": "绿",
                "ai_reference_rationale": "程序分与长势一致。",
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
                {"field": {"name": "x"}}, client=client, parallel=False
            )
        client.close()
        self.assertTrue(out["llm_configured"])
        self.assertIn("适合玉米", out["overall"]["evaluation"])
        self.assertEqual(out["yield_potential"]["level"], "中")
        self.assertIsNone(out["error"])
        self.assertEqual(out["ai_reference_score"], 72.0)
        self.assertEqual(
            out["ai_reference_disclaimer"], ai_analysis.AI_REFERENCE_DISCLAIMER
        )

    def test_parallel_section_soft_fail(self) -> None:
        """One section HTTP-fails; others still assemble."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            body = json.loads(request.content.decode("utf-8"))
            user = body["messages"][1]["content"]
            if "分节=spatial" in user:
                return httpx.Response(500, text="spatial boom")
            # Return minimal matching section
            if "分节=overall" in user:
                payload = {
                    "overall": {
                        "evaluation": "并行 overall 成功",
                        "strengths": ["ok"],
                        "main_risks": [],
                        "core_advice": ["因程序分数中等，保持常规管理"],
                    }
                }
            elif "分节=soil" in user:
                payload = {
                    "soil": {
                        "indicators_to_farm": [
                            {
                                "indicator": "黏壤土",
                                "farm_impact": "适中",
                                "management": "保墒",
                            }
                        ]
                    }
                }
            else:
                # generic empty-ish success for other sections
                key = "portrait"
                for name, _, _ in ai_analysis.SECTION_SPECS:
                    if f"分节={name}" in user:
                        key = name
                        break
                if key == "rs_growth":
                    payload = {
                        "rs_growth": {
                            "phenology_normality": "正常",
                            "anomalies": [],
                            "ranked_causes": [],
                        }
                    }
                elif key == "yield_potential":
                    payload = {
                        "yield_potential": {"level": "中", "rationale": "无模型"}
                    }
                elif key == "business":
                    payload = {
                        "business": {"available": False, "note": "数据不足"},
                        "evidence_gaps": [],
                    }
                elif key == "climate":
                    payload = {
                        "climate": {
                            "risk_present": [],
                            "disaster_occurred": [],
                            "notes": "",
                        }
                    }
                elif key == "management":
                    payload = {
                        "management": {
                            "variety_direction": "常规",
                            "planting_focus": [],
                            "water_fertility_watch": [],
                            "scouting": [],
                        }
                    }
                elif key == "score_explain":
                    payload = {
                        "score_explain": {
                            "high_dims": [],
                            "low_dims": [],
                            "biggest_drivers": [],
                            "how_to_improve": [],
                        }
                    }
                elif key == "portrait":
                    payload = {
                        "portrait": {
                            "regional_ag_traits": "华北",
                            "crop_fit": "较适宜",
                            "limits": [],
                        }
                    }
                else:
                    payload = {key: {}}
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(payload, ensure_ascii=False)
                            }
                        }
                    ]
                },
            )

        # Parallel path creates its own clients; patch httpx.Client
        transport = httpx.MockTransport(handler)

        class _C(httpx.Client):
            def __init__(self, *a, **k):
                k.setdefault("transport", transport)
                super().__init__(*a, **k)

        with patch.dict(os.environ, {"BAILIAN_API_KEY": "k"}, clear=False):
            with patch.object(httpx, "Client", _C):
                out = ai_analysis.generate_land_assessment_narrative(
                    {"field": {}, "scorecard": {}, "rs": {}, "risk": {}, "soil": {}},
                    parallel=True,
                    max_workers=4,
                    timeout=30.0,
                )
        self.assertTrue(out["llm_configured"])
        self.assertIn("并行 overall 成功", out["overall"]["evaluation"])
        self.assertTrue(out["soil"]["indicators_to_farm"])
        self.assertTrue(out.get("section_errors"))
        self.assertTrue(any("spatial" in e for e in out["section_errors"]))
        self.assertGreaterEqual(calls["n"], 5)

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
            soil={
                "avg_ph": 7.2,
                "dominant_texture": "clay loam",
                "drainage_class": "well drained",
                "rootzone_awc_mm": 164,
            },
            weather_summary={},
            analysis={},
            flood_evidence=None,
        )
        self.assertEqual(facts["field"]["area_mu"], 15.0)
        self.assertIsNone(facts["yield_model"])
        self.assertIsNone(facts["business_model"])
        self.assertEqual(facts["soil"]["dominant_texture"], "黏壤土")
        self.assertEqual(facts["soil"]["drainage_class"], "排水良好")
        self.assertIn("根系层有效持水量", facts["soil"]["rootzone_awc_label"])


    def test_normalize_ai_reference_fields(self) -> None:
        out = ai_analysis.normalize_ai(
            {
                "overall": {
                    "evaluation": "综合条件中等偏好。",
                    "strengths": ["长势"],
                    "main_risks": [],
                    "core_advice": ["常规管理"],
                    "ai_reference_score": 73.4,
                    "ai_reference_grade": "较好",
                    "ai_reference_light": "绿",
                    "ai_reference_rationale": "程序分项与问卷基本一致，渍涝证据偏弱。",
                }
            }
        )
        self.assertEqual(out["ai_reference_score"], 73.4)
        self.assertEqual(out["ai_reference_grade"], "较好")
        self.assertEqual(out["ai_reference_light"], "绿")
        self.assertIn("问卷", out["ai_reference_rationale"])
        self.assertEqual(
            out["ai_reference_disclaimer"], ai_analysis.AI_REFERENCE_DISCLAIMER
        )
        # Must not leak into narrative overall keys
        self.assertNotIn("ai_reference_score", out["overall"])

    def test_ai_reference_clamped_and_fail_omits(self) -> None:
        high = ai_analysis.normalize_ai(
            {"overall": {"evaluation": "ok", "ai_reference_score": 140}}
        )
        self.assertEqual(high["ai_reference_score"], 100.0)
        miss = ai_analysis.empty_ai_payload(error="missing_api_key", note="AI 分析失败")
        self.assertIsNone(miss["ai_reference_score"])
        self.assertIsNone(miss["ai_reference_disclaimer"])
        failed = ai_analysis.failed_llm_sections("boom")
        self.assertIsNone(failed["ai_reference_score"])

    def test_program_overall_untouched_by_ai_reference(self) -> None:
        """AI reference must not rewrite a program scorecard overall."""
        from app.reports.land_assessment.scorecard_view import scorecard_public_view

        program = {
            "overall": {
                "score": 76.5,
                "grade": "较好",
                "light": "绿",
                "one_liner": "程序一句话",
            },
            "dimensions": [
                {"key": k, "score": 70.0, "light": "绿", "weight": "10%"}
                for k in (
                    "crop",
                    "soil",
                    "vigor",
                    "weather",
                    "wet_safety",
                    "drought_safety",
                )
            ],
        }
        ai = ai_analysis.normalize_ai(
            {
                "overall": {
                    "evaluation": "ok",
                    "ai_reference_score": 61,
                    "ai_reference_rationale": "问卷偏弱",
                }
            }
        )
        view = scorecard_public_view(program, ai=ai)
        self.assertEqual(view["overall"]["score"], 76.5)
        self.assertEqual(view["ai_reference"]["score"], 61.0)
        self.assertIn("不可作为准入", view["ai_reference"]["disclaimer"])



class QuestionnaireAnalysisTests(unittest.TestCase):
    def facts(self, survey=None):
        return ai_analysis.facts_for_llm(
            field={},
            scorecard={},
            rs={},
            risk={},
            soil={},
            weather_summary={},
            site_admission=survey,
        )

    def test_complete_questionnaire_reaches_all_parallel_requests(self):
        survey = {
            "source": "现场问卷",
            "fetched_at": "2026-09-14",
            "item_answers": {"drainage": "blocked", "note": "排水出口受阻"},
            "red_line_answers": {"ownership": False},
            "dimensions": [
                {
                    "name": "水利",
                    "items": [
                        {
                            "id": "drainage",
                            "option_key": "blocked",
                            "option_label": "排水出口受阻",
                        }
                    ],
                }
            ],
        }
        facts = self.facts(survey)
        seen = []

        def respond(*, system, user, **kwargs):
            received = json.loads(user.split("程序事实 JSON：\n", 1)[1])
            received_survey = received["site_admission"]
            self.assertEqual(received_survey["item_answers"], survey["item_answers"])
            self.assertFalse(received_survey["红线排查"]["ownership"])
            self.assertEqual(
                received_survey["维度得分"][0]["items"][0]["option_label"],
                "排水出口受阻",
            )
            self.assertEqual(received_survey["source"], "现场问卷")
            self.assertEqual(received_survey["fetched_at"], "2026-09-14")
            key = user.split("分节=", 1)[1].split("。", 1)[0]
            seen.append(key)
            if key == "overall":
                return {
                    key: {
                        "evaluation": "现场问卷反映排水出口受阻，需与土壤排水等级交叉核实。"
                    }
                }, None
            if key == "business":
                return {
                    key: {"available": False},
                    "evidence_gaps": ["现场排水情况待核验"],
                }, None
            return {key: {}}, None

        with (
            patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}),
            patch.object(ai_analysis, "_bailian_chat", side_effect=respond),
        ):
            out = ai_analysis.generate_land_assessment_narrative(facts, parallel=True)
        self.assertEqual(set(seen), {spec[0] for spec in ai_analysis.SECTION_SPECS})
        self.assertIn("现场问卷", out["overall"]["evaluation"])
        self.assertEqual(out["evidence_gaps"], ["现场排水情况待核验"])

    def test_absent_questionnaire_stays_optional_for_every_section(self):
        facts = self.facts()
        for key, _, _ in ai_analysis.SECTION_SPECS:
            self.assertIsNone(ai_analysis._section_facts(facts, key)["site_admission"])


if __name__ == "__main__":
    unittest.main()
