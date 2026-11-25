"""Model font marks must be ReportLab-safe: add a missing #, drop illegal colors."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.reports.biweekly.narrative_ai import _present, section_lines
from app.reports.biweekly.official_pdf import (
    _append_paragraph,
    _styles,
    _register_fonts,
    render_official_biweekly_pdf,
)


MISSING_HASH = (
    "绿度从0.17升到0.18，变化很小，苗的绿色"
    '<font face="YaHei-Bold" color="#1F4D38" backColor="D9F2E3">略有增加</font>'
    "，还谈不上旺。"
)


def _facts_with_conclusion(sentence: str) -> dict:
    return {
        "land_id": "13229",
        "land_name": "试验田13229",
        "policy_version": "biweekly-wheat-display-v8-1",
        "period": {
            "period_id": "2",
            "start": "2026-11-11",
            "end_inclusive": "2026-11-24",
            "end_exclusive": "2026-11-25",
        },
        "phase": {
            "display_phase": "S2",
            "primary_metrics": ["ndvi", "evi"],
            "supporting_metrics": ["ndmi"],
            "plan_source": "native",
            "plan_label": "本田方案",
        },
        "optical_days": ["2026-11-12"],
        "observations": [
            {
                "date": "2026-11-12",
                "sensor": "S2",
                "ndvi_avg": 0.18,
                "evi_avg": 0.15,
                "ndmi_avg": 0.11,
            }
        ],
        "s1_count": 0,
        "ai_narrative": {
            "llm_configured": True,
            "conclusion": [sentence],
            "field_notes": [],
        },
    }


class FontMarkupSanitizeTests(unittest.TestCase):
    def test_missing_hash_on_backcolor_is_repaired(self) -> None:
        shown = section_lines({"conclusion": [MISSING_HASH]}, "conclusion")[0]
        self.assertIn("略有增加", shown)
        self.assertIn('color="#1F4D38"', shown)
        self.assertIn('backColor="#D9F2E3"', shown)
        self.assertNotIn('backColor="D9F2E3"', shown)

    def test_illegal_backcolor_is_dropped_and_color_kept(self) -> None:
        raw = (
            '苗情<font face="YaHei-Bold" color="#1F4D38" backColor="ZZZZZZ">'
            "略有增加</font>。"
        )
        shown = _present(raw)
        self.assertIn("略有增加", shown)
        self.assertIn('color="#1F4D38"', shown)
        self.assertNotIn("ZZZZZZ", shown)
        self.assertNotIn("backColor", shown)

    def test_unknown_color_is_not_passed_through(self) -> None:
        shown = _present('<font color="#0000FF">蓝</font>字')
        self.assertNotIn("0000FF", shown)
        self.assertIn("蓝", shown)


class ParagraphFallbackTests(unittest.TestCase):
    def test_illegal_color_at_paragraph_still_makes_a_pdf(self) -> None:
        from reportlab.platypus import SimpleDocTemplate

        _register_fonts()
        styles = _styles()
        dest = Path(tempfile.mkdtemp()) / "fallback.pdf"
        story: list = []
        raw = (
            "绿度从0.17升到0.18，苗的绿色"
            '<font face="YaHei-Bold" color="#1F4D38" backColor="D9F2E3">略有增加</font>。'
        )
        _append_paragraph(story, raw, styles["body"])
        self.assertTrue(story)
        doc = SimpleDocTemplate(str(dest), pagesize=(400, 400))
        doc.build(story)
        self.assertTrue(dest.read_bytes().startswith(b"%PDF"))
        self.assertGreater(dest.stat().st_size, 200)

    def test_official_pdf_accepts_missing_hash_from_model(self) -> None:
        dest = Path(tempfile.mkdtemp()) / "official.pdf"
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("BIWEEKLY_FONT")
        }
        with patch.dict(os.environ, env, clear=True):
            path = render_official_biweekly_pdf(_facts_with_conclusion(MISSING_HASH), dest)
        self.assertTrue(path.is_file())
        self.assertTrue(path.read_bytes().startswith(b"%PDF"))
        self.assertGreater(path.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
