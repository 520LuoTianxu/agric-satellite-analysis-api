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
                reader = PdfReader(result["out_path"])
                n_pages = len(reader.pages)
                # 完整版保留全部章节，不再通过删减内容压到十页以内。
                self.assertGreater(n_pages, 10)
                front = "".join((pg.extract_text() or "") for pg in reader.pages[:4])
                self.assertIn("目录", front)
                self.assertIn("二、地块基础画像", front)
                self.assertTrue(
                    (Path(result["charts_dir"]) / "score_radar.png").exists()
                    or "score_radar.png" in (result.get("charts") or [])
                )
                all_text = "".join((pg.extract_text() or "") for pg in reader.pages)
                self.assertIn("乡合农服", all_text)
                self.assertNotIn("openfarm", all_text.lower())
                self.assertEqual(reader.metadata.author, "乡合农服")
                self.assertIn("异常点", all_text)
                self.assertIn("三、综合评分解释", all_text)
                self.assertIn("八、种植管理建议", all_text)
                self.assertIn("九、产量潜力", all_text)
                self.assertIn("十、经营分析", all_text)
                self.assertIn("潜力等级（相对）：中", all_text)
                self.assertEqual(len(reader.outline), 10)
                # 目录页码来自实际分页结果，跳转目的地与正文的章节一致。
                for entry in reader.outline:
                    page_index = reader.get_destination_page_number(entry)
                    self.assertIn(entry.title, reader.pages[page_index].extract_text())
                # Evidence columns / event ids when fixture has events
                evs = (result.get("analysis") or {}).get("risk_events_evidence") or []
                if evs:
                    self.assertIn("E1", all_text)

    def test_questionnaire_appendix_preserves_answers_and_red_lines(self) -> None:
        from app.reports.land_assessment.pdf_render import render_pdf

        survey = {
            "source": "现场采集",
            "fetched_at": "2026-09-14",
            "item_answers": {
                "drainage": "blocked",
                "custom_note": "雨后排水缓慢，待核实",
                "cost": 0,
            },
            "red_line_answers": {"权属是否清晰": False},
            "dimensions": [
                {
                    "name": "水利",
                    "items": [
                        {
                            "id": "drainage",
                            "name": "排水条件",
                            "option_key": "blocked",
                            "option_label": "排水出口受阻",
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = render_pdf(
                out_path=Path(folder) / "survey.pdf",
                field={"name": "问卷测试"},
                scorecard={
                    "overall": {"score": 70, "light": "绿", "grade": "较好"},
                    "dimensions": [],
                },
                rs={},
                risk={},
                soil={},
                weather_summary={},
                site_admission=survey,
            )
            reader = PdfReader(path)
            text = "".join(p.extract_text() for p in reader.pages)
            self.assertIn("附录、现场问卷明细", text)
            self.assertIn("排水出口受阻", text)
            self.assertIn("雨后排水缓慢，待核实", text)
            self.assertIn("权属是否清晰", text)
            self.assertIn("false", text)
            self.assertIn("\n0\n", text)
            self.assertEqual(reader.outline[-1].title, "附录、现场问卷明细")

    def test_long_analysis_and_all_events_remain_complete(self) -> None:
        from app.reports.land_assessment.pdf_render import render_pdf

        # 长段落与超八条事件不能被页数限制或表格分页截断。
        long_note = (
            "保持完整分析，结合土壤和现场条件说明选地依据。" * 90 + "分析结束标记"
        )
        with tempfile.TemporaryDirectory() as folder:
            path = render_pdf(
                out_path=Path(folder) / "long.pdf",
                field={"name": "完整性测试"},
                scorecard={
                    "overall": {"score": 70, "light": "绿", "grade": "较好"},
                    "dimensions": [],
                },
                rs={},
                soil={},
                weather_summary={},
                risk={
                    "events": [
                        {"id": f"E{i}", "performance": "原始观测记录"}
                        for i in range(1, 11)
                    ]
                },
                ai={"yield_potential": {"level": "中", "rationale": long_note}},
            )
            # 比较正文时去掉跨页页眉、页脚和续表重复表头。
            text = "".join(
                "\n".join(p.extract_text().splitlines()[2:])
                for p in PdfReader(path).pages
            )
            text = (
                text.replace("\n", "")
                .replace("选地分析报告", "")
                .replace("依据说明", "")
            )
            self.assertIn(long_note, text)
            self.assertIn("E10", text)
            self.assertIn("十、经营分析", text)


if __name__ == "__main__":
    unittest.main()
