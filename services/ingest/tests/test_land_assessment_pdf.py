"""Smoke render land-assessment PDF with fixture + mocked AI."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader

FIX = Path(__file__).resolve().parent / "fixtures" / "land_assessment"


class LandAssessmentPdfSmoke(unittest.TestCase):
    def test_smoke_from_dir_with_mock_ai(self) -> None:
        if not (FIX / "field.json").exists():
            self.skipTest("fixture missing")

        from app.reports.land_assessment.ai_analysis import empty_ai_payload
        from app.reports.land_assessment.service import generate_assessment_pdf

        ai = empty_ai_payload(error="missing_api_key", note="AI 分析失败")
        ai["llm_configured"] = False
        # Fill a bit so PDF AI sections are non-empty markers
        ai["overall"]["evaluation"] = "AI 分析失败（测试占位）"
        ai["yield_potential"] = {"level": "中", "rationale": "测试无产量模型"}

        with patch(
            "app.reports.land_assessment.service.generate_land_assessment_narrative",
            return_value=ai,
        ):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "assess.pdf"
                result = generate_assessment_pdf(data_dir=FIX, out_path=out)
                self.assertTrue(Path(result["out_path"]).exists())
                self.assertGreater(Path(result["out_path"]).stat().st_size, 2000)
                self.assertIn("score", result)
                self.assertIn("ai", result)
                self.assertEqual(result["ai"]["yield_potential"]["level"], "中")
                # scoring still produced a numeric score
                self.assertIsInstance(result["score"], (int, float))
                n_pages = len(PdfReader(result["out_path"]).pages)
                self.assertLessEqual(n_pages, 10)


if __name__ == "__main__":
    unittest.main()
