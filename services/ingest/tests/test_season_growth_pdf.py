"""PDF render smoke test (no LLM, minimal facts)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.reports.season_growth.pdf_render import render_season_growth_pdf


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


if __name__ == "__main__":
    unittest.main()
