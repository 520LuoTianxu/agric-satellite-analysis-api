"""PDF render smoke test (no LLM, minimal + richer facts)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.reports.season_growth.pdf_render import render_season_growth_pdf


def _rich_facts() -> dict:
    return {
        "field": {
            "field_name": "测试地块",
            "land_id": "LAND001",
            "field_id": "00000000-0000-0000-0000-000000000001",
        },
        "window": {
            "start_date": "2026-06-01",
            "end_date": "2026-09-30",
            "label": "2026夏玉米",
            "crops": ["summer_corn"],
        },
        "data_source": "test",
        "scenes": {
            "s2_count": 7,
            "s1_count": 4,
            "s2_official_count": 7,
            "s2_clear_count": 7,
        },
        "ndvi": {
            "mean": 0.55,
            "peak": {"date": "2026-08-05", "value": 0.7},
            "latest": {"date": "2026-09-05", "value": 0.4},
            "series": [
                {"date": "2026-06-05", "value": 0.32},
                {"date": "2026-08-05", "value": 0.7},
            ],
        },
        "ndmi": {"mean": 0.1, "series": [{"date": "2026-06-05", "value": 0.18}]},
        "drought": {
            "drought_scene_count": 1,
            "counts": {"normal": 6, "mild": 1},
            "days": [{"date": "2026-08-20", "class": "mild"}],
            "scene_classes": [
                {"date": "2026-06-05", "class": "normal"},
                {"date": "2026-08-20", "class": "mild"},
            ],
        },
        "flood": {
            "status": "ok",
            "vv_median": -12.0,
            "flood_scene_count": 0,
            "counts": {"dry": 4},
            "scenes": [
                {
                    "date": "2026-07-01",
                    "vv": -12.0,
                    "vh": -18.0,
                    "relative_orbit": 10,
                    "class": "dry",
                }
            ],
            "note": None,
        },
        "harvest": {
            "status": "uncertain",
            "harvest_date": None,
            "confidence": "low",
        },
        "prior_year": None,
        "methodology": {
            "drought": "S2 干旱规则摘要",
            "flood": "S1 洪涝规则摘要",
            "sensors": "S2/S1",
        },
        "timeline": [
            {
                "month": "2026-06",
                "s2_count": 2,
                "s1_count": 0,
                "drought_days": 0,
                "flood_count": 0,
                "watch_count": 0,
            },
            {
                "month": "2026-07",
                "s2_count": 2,
                "s1_count": 3,
                "drought_days": 0,
                "flood_count": 0,
                "watch_count": 0,
            },
        ],
        "s2_appendix": [
            {
                "date": "2026-06-05",
                "cloud_pct": 5.0,
                "quality": "good",
                "drought_class": "normal",
                "drought_class_cn": "正常",
                "ndvi": 0.32,
                "ndmi": 0.18,
                "evi": 0.28,
                "mndwi": -0.1,
            },
            {
                "date": "2026-08-20",
                "cloud_pct": 8.0,
                "quality": "good",
                "drought_class": "mild",
                "drought_class_cn": "轻度",
                "ndvi": 0.55,
                "ndmi": -0.05,
                "evi": 0.45,
                "mndwi": -0.05,
            },
        ],
        "s1_appendix": [
            {
                "date": "2026-07-01",
                "relative_orbit": 10,
                "vv": -12.0,
                "vh": -18.0,
                "flood_class": "dry",
                "flood_class_cn": "正常",
            }
        ],
    }


def _rich_ai() -> dict:
    return {
        "one_liner": "遥感事实已生成（AI 摘要未启用）",
        "summary": "大模型未配置，仅含程序事实。",
        "evidence_bullets": ["S2 景数 7"],
        "core_conclusion": "窗口内长势总体可观测。",
        "moisture_analysis": "干旱轻度 1 景；无洪涝。",
        "interpretation": "大模型未配置。",
        "causes_ranked": ["数据覆盖有限"],
        "recommendations": None,
        "follow_up": ["补充田间调查"],
        "timeline_notes": "7 月 S1 覆盖较好。",
        "llm_configured": False,
    }


class SeasonGrowthPdfTests(unittest.TestCase):
    def test_render_minimal_pdf_bytes(self) -> None:
        facts = {
            "field": {
                "field_name": "测试地块",
                "land_id": "LAND001",
                "field_id": "00000000-0000-0000-0000-000000000001",
            },
            "window": {
                "start_date": "2026-06-01",
                "end_date": "2026-09-30",
                "label": "2026夏玉米",
                "crops": ["summer_corn"],
            },
            "data_source": "test",
            "scenes": {
                "s2_count": 7,
                "s1_count": 0,
                "s2_official_count": 7,
                "s2_clear_count": 7,
            },
            "ndvi": {
                "mean": 0.55,
                "peak": {"date": "2026-08-05", "value": 0.7},
                "latest": {"date": "2026-09-05", "value": 0.4},
                "series": [
                    {"date": "2026-06-05", "value": 0.32},
                    {"date": "2026-08-05", "value": 0.7},
                ],
            },
            "ndmi": {"mean": 0.1, "series": []},
            "drought": {"drought_scene_count": 0, "counts": {"normal": 7}},
            "flood": {"status": "no_s1_data", "vv_median": None, "note": "无 S1"},
            "harvest": {
                "status": "uncertain",
                "harvest_date": None,
                "confidence": "low",
            },
            "prior_year": None,
        }
        ai = {
            "one_liner": "遥感事实已生成（AI 摘要未启用）",
            "summary": "大模型未配置，仅含程序事实。",
            "evidence_bullets": [],
            "interpretation": "大模型未配置。",
            "recommendations": None,
            "llm_configured": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "season.pdf"
            path = render_season_growth_pdf(
                facts=facts,
                ai=ai,
                chart_path=None,
                materials_meta=[],
                out_path=out,
            )
            self.assertTrue(path.exists())
            data = path.read_bytes()
            self.assertGreater(len(data), 500)
            self.assertTrue(data.startswith(b"%PDF"))

    def test_render_richer_pdf_with_appendix(self) -> None:
        facts = _rich_facts()
        ai = _rich_ai()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "season_rich.pdf"
            path = render_season_growth_pdf(
                facts=facts,
                ai=ai,
                chart_paths=None,
                materials_meta=[],
                out_path=out,
            )
            self.assertTrue(path.exists())
            data = path.read_bytes()
            self.assertGreater(len(data), 3000)
            self.assertTrue(data.startswith(b"%PDF"))
            # Section titles are embedded as CN text; at least multi-page PDF
            self.assertIn(b"/Type /Page", data)


if __name__ == "__main__":
    unittest.main()
