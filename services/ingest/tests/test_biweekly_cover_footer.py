"""Regression tests for the official biweekly cover footer (contacts + short gray disclaimer)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import ImageFont

from app.reports.biweekly.cover import (
    COVER_FOOTER_CONTACT_LIU,
    COVER_FOOTER_CONTACT_LUO,
    COVER_FOOTER_CONTACTS,
    COVER_FOOTER_DISCLAIMER,
    COVER_FOOTER_ORG,
    COVER_FOOTER_PARAGRAPHS,
    COVER_H,
    COVER_W,
    FOOTER_DISCLAIMER_FILL,
    FOOTER_FONT_SIZE,
    FOOTER_HEADING_FILL,
    FOOTER_PREFERRED_MIN_TOP,
    FOOTER_RULE,
    assert_footer_wording_intact,
    compose_official_cover,
    cover_footer_geometry,
    cover_footer_rows,
    cover_meta_rows,
)
from app.reports.biweekly.fonts import resolve_yahei_fonts

_EXPECTED = (
    "数字农业中心｜报告咨询",
    "刘伯恩：liuboen@chongho.net",
    "罗添煦：luotianxu@chongho.net",
    "部分内容由 AI 辅助生成，仅供参考，地块实际情况以实地核查为准。",
)


def _fonts():
    regular, bold = resolve_yahei_fonts()
    heading = ImageFont.truetype(str(bold), size=30)
    contact = ImageFont.truetype(str(regular), size=FOOTER_FONT_SIZE)
    return heading, contact, contact


def _sample_facts() -> dict:
    return {
        "land_id": "19994",
        "land_name": "北侯1号地块",
        "report_date": "2026-11-23",
        "period": {
            "period_id": "2",
            "start": "2025-10-16",
            "end_inclusive": "2025-10-29",
            "end_exclusive": "2025-10-30",
        },
        "phase": {"display_phase": "S1"},
    }


class OfficialWordingTests(unittest.TestCase):
    def test_paragraphs_match_reviewed_wording_exactly(self) -> None:
        self.assertEqual(COVER_FOOTER_PARAGRAPHS, _EXPECTED)
        self.assertEqual(COVER_FOOTER_ORG, _EXPECTED[0])
        self.assertEqual(COVER_FOOTER_CONTACTS, _EXPECTED[1:3])
        self.assertEqual(COVER_FOOTER_DISCLAIMER, _EXPECTED[3])
        self.assertEqual(COVER_FOOTER_CONTACT_LIU, "刘伯恩：liuboen@chongho.net")
        self.assertEqual(COVER_FOOTER_CONTACT_LUO, "罗添煦：luotianxu@chongho.net")
        self.assertIn("：", COVER_FOOTER_CONTACT_LIU)
        self.assertNotIn(":", COVER_FOOTER_CONTACT_LIU)
        self.assertNotIn(":", COVER_FOOTER_CONTACT_LUO)
        self.assertIn(" AI ", COVER_FOOTER_DISCLAIMER)
        self.assertNotIn("如对报告内容或相关任务有疑问", COVER_FOOTER_DISCLAIMER)
        self.assertNotIn("人工智能（AI）", COVER_FOOTER_DISCLAIMER)
        self.assertNotIn("AI业务中心", COVER_FOOTER_ORG)
        self.assertNotIn("陈洁", "".join(COVER_FOOTER_PARAGRAPHS))

    def test_wrap_and_order_are_contacts_then_disclaimer(self) -> None:
        _heading, contact, disclaimer = _fonts()
        for width in (400, 800, 1434):
            rows = cover_footer_rows(contact, disclaimer, width)
            assert_footer_wording_intact(rows)
            kinds = [kind for kind, _text in rows]
            self.assertEqual(kinds[:3], ["heading", "contact", "contact"])
            self.assertIn("blank", kinds)
            self.assertGreater(kinds.index("disclaimer"), kinds.index("blank"))
            disc = "".join(text for kind, text in rows if kind == "disclaimer")
            self.assertEqual(disc, COVER_FOOTER_DISCLAIMER)


class CoverCompositionTests(unittest.TestCase):
    def test_footer_is_centered_in_the_lower_band(self) -> None:
        heading, contact, disclaimer = _fonts()
        geo = cover_footer_geometry(
            heading_font=heading, contact_font=contact, disclaimer_font=disclaimer
        )
        self.assertGreaterEqual(geo["top"], 0)
        self.assertLessEqual(geo["bottom"], COVER_H)
        self.assertGreaterEqual(geo["top"], FOOTER_PREFERRED_MIN_TOP - 80)
        assert_footer_wording_intact(geo["rows"])

    def test_compose_uses_green_title_and_gray_disclaimer_no_blue(self) -> None:
        dest = Path(tempfile.mkdtemp()) / "cover.png"
        path = compose_official_cover(_sample_facts(), dest, land_title="北侯1号地块")
        from PIL import Image

        image = Image.open(path)
        self.assertEqual(image.size, (COVER_W, COVER_H))
        heading, contact, disclaimer = _fonts()
        geo = cover_footer_geometry(
            heading_font=heading, contact_font=contact, disclaimer_font=disclaimer
        )
        # Re-draw geometry after compose and sample pixels from painted cover.
        from app.reports.biweekly.cover import FOOTER_RULE_GAP, FOOTER_RULE_HEIGHT, centered_text_x

        # Heading is dark green near the vertical midline.
        hx = centered_text_x(heading, COVER_FOOTER_ORG, COVER_W)
        hy = geo["rule_y"] + FOOTER_RULE_HEIGHT + FOOTER_RULE_GAP + 16
        head_px = image.getpixel((hx + 20, hy))
        self.assertLess(head_px[2], head_px[1], msg=f"heading should be green, not blue: {head_px}")
        self.assertGreater(head_px[1], head_px[0])
        # Disclaimer is medium gray (r≈g≈b), not blue.
        dx = centered_text_x(disclaimer, COVER_FOOTER_DISCLAIMER, COVER_W)
        # last row is disclaimer
        y = geo["rule_y"] + FOOTER_RULE_HEIGHT + FOOTER_RULE_GAP
        from app.reports.biweekly.cover import _row_advance

        for kind, _text in geo["rows"]:
            if kind == "disclaimer":
                dy = y + 14
                break
            y += _row_advance(kind)
        else:
            self.fail("disclaimer row missing")
        # scan for a mid-gray glyph
        found = None
        for xx in range(int(dx), int(dx) + 200, 2):
            for yy in range(int(dy) - 4, int(dy) + 8):
                px = image.getpixel((xx, yy))
                r, g, b = px[:3]
                if max(r, g, b) - min(r, g, b) <= 18 and 70 <= (r + g + b) / 3 <= 140:
                    found = px
                    break
            if found:
                break
        self.assertIsNotNone(found, "expected medium-gray disclaimer ink")
        r, g, b = found[:3]
        self.assertLessEqual(abs(r - g), 18)
        self.assertLessEqual(abs(g - b), 18)
        self.assertLess(b, 160)
        # Fills themselves are not blue.
        self.assertEqual(FOOTER_DISCLAIMER_FILL[0], FOOTER_DISCLAIMER_FILL[1])
        self.assertEqual(FOOTER_DISCLAIMER_FILL[1], FOOTER_DISCLAIMER_FILL[2])
        self.assertGreater(FOOTER_HEADING_FILL[1], FOOTER_HEADING_FILL[2])
        self.assertGreater(FOOTER_RULE[1], FOOTER_RULE[2])
        # Footer sits below meta and is roughly centred.
        meta_bottom = 960 + 74 * len(cover_meta_rows(_sample_facts()))
        self.assertGreater(geo["top"], meta_bottom)
        compose_official_cover(_sample_facts(), dest, land_title="北侯1号地块")
        # line centers from a fresh draw
        from app.reports.biweekly.cover import draw_cover_footer
        from PIL import ImageDraw, Image as PILImage

        canvas = PILImage.new("RGB", (COVER_W, COVER_H), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        painted = draw_cover_footer(
            draw, heading_font=heading, contact_font=contact, disclaimer_font=disclaimer
        )
        for cx in painted["line_centers"]:
            self.assertAlmostEqual(cx, COVER_W / 2, delta=8)

    def test_body_pdf_pages_do_not_repeat_the_footer(self) -> None:
        from app.reports.biweekly.official_pdf import render_official_biweekly_pdf

        facts = {
            "land_id": "19994",
            "land_name": "北侯1号地块",
            "report_date": "2026-11-23",
            "policy_version": "biweekly-wheat-display-v8-1",
            "period": {
                "period_id": "12",
                "start": "2026-04-04",
                "end_inclusive": "2026-04-17",
                "end_exclusive": "2026-04-18",
                "s1_products": 1,
            },
            "phase": {
                "display_phase": "S2",
                "primary_metrics": ["ndre", "evi"],
                "supporting_metrics": ["ndvi", "ndmi"],
                "plan_source": "native",
                "plan_label": "本田方案",
            },
            "cutoff": "2026-04-18T00:00:00+08:00",
            "optical_days": ["2026-04-09"],
            "observations": [
                {
                    "date": "2026-04-09",
                    "sensor": "S2",
                    "ndvi_avg": 0.42,
                    "evi_avg": 0.31,
                    "ndre_avg": 0.22,
                    "ndmi_avg": 0.11,
                }
            ],
            "s1_count": 1,
            "in_period_radar": True,
            "forbidden_metrics_omitted": ["fvc", "lai"],
        }
        dest = Path(tempfile.mkdtemp()) / "out.pdf"
        render_official_biweekly_pdf(facts, dest)
        raw = subprocess.check_output(["pdftotext", "-layout", str(dest), "-"], text=True)
        pages = raw.split("\f")
        body = "\f".join(pages[1:])
        self.assertNotIn("数字农业中心", body)
        self.assertNotIn("liuboen@chongho.net", body)
        self.assertNotIn("luotianxu@chongho.net", body)
        self.assertNotIn("部分内容由 AI 辅助生成", body)

    def test_typographic_fallback_is_centered_and_uses_same_wording(self) -> None:
        from app.reports.biweekly.official_pdf import render_official_biweekly_pdf

        facts = _sample_facts()
        facts.update(
            {
                "optical_days": ["2025-10-20"],
                "observations": [
                    {
                        "date": "2025-10-20",
                        "sensor": "S2",
                        "ndvi_avg": 0.3,
                        "evi_avg": 0.2,
                        "ndre_avg": 0.15,
                        "ndmi_avg": 0.1,
                    }
                ],
                "phase": {
                    "display_phase": "S1",
                    "primary_metrics": ["ndvi"],
                    "supporting_metrics": [],
                    "plan_source": "native",
                },
                "s1_count": 0,
                "in_period_radar": False,
            }
        )
        dest = Path(tempfile.mkdtemp()) / "fallback.pdf"
        with patch("app.reports.biweekly.official_pdf.compose_official_cover", side_effect=FileNotFoundError("no art")):
            render_official_biweekly_pdf(facts, dest)
        raw = subprocess.check_output(["pdftotext", "-layout", str(dest), "-"], text=True)
        cover = raw.split("\f")[0].replace("\n", "")
        for paragraph in _EXPECTED:
            self.assertIn(paragraph, cover)
        body = "\f".join(raw.split("\f")[1:])
        self.assertNotIn("liuboen@chongho.net", body)


if __name__ == "__main__":
    unittest.main()
