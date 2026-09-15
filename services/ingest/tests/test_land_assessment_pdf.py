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


    def test_ai_reference_score_on_cover_with_disclaimer(self) -> None:
        from app.reports.land_assessment.ai_analysis import AI_REFERENCE_DISCLAIMER
        from app.reports.land_assessment.pdf_render import render_pdf

        with tempfile.TemporaryDirectory() as folder:
            path = render_pdf(
                out_path=Path(folder) / "ai_ref.pdf",
                field={"name": "参考分测试", "area_ha": 1.0},
                scorecard={
                    "overall": {
                        "score": 76.5,
                        "light": "绿",
                        "grade": "较好",
                        "one_liner": "程序综合分不变",
                    },
                    "dimensions": [],
                    "confidence": {"plain": "测"},
                },
                rs={},
                risk={},
                soil={},
                weather_summary={},
                ai={
                    "overall": {
                        "evaluation": "综合解读正常",
                        "strengths": ["长势"],
                        "main_risks": [],
                        "core_advice": ["常规"],
                    },
                    "ai_reference_score": 68.0,
                    "ai_reference_grade": "一般",
                    "ai_reference_light": "黄",
                    "ai_reference_rationale": "问卷排水偏弱，与遥感略有冲突。",
                    "ai_reference_disclaimer": AI_REFERENCE_DISCLAIMER,
                    "yield_potential": {"level": "中", "rationale": "无模型"},
                },
            )
            text = "".join(p.extract_text() or "" for p in PdfReader(path).pages[:3])
            self.assertTrue("76.5" in text or "76" in text)
            self.assertIn("程序计算", text)
            self.assertIn("AI 参考分", text)
            self.assertIn("不可作为准入", text)
            self.assertIn("问卷排水偏弱", text)

    def test_ai_fail_marks_reference_absent(self) -> None:
        from app.reports.land_assessment.ai_analysis import empty_ai_payload
        from app.reports.land_assessment.pdf_render import render_pdf

        ai = empty_ai_payload(error="missing_api_key", note="AI 分析失败")
        with tempfile.TemporaryDirectory() as folder:
            path = render_pdf(
                out_path=Path(folder) / "ai_fail.pdf",
                field={"name": "失败占位"},
                scorecard={
                    "overall": {"score": 70, "light": "绿", "grade": "较好"},
                    "dimensions": [],
                },
                rs={},
                risk={},
                soil={},
                weather_summary={},
                ai=ai,
            )
            text = "".join(p.extract_text() or "" for p in PdfReader(path).pages[:2])
            self.assertIn("70", text)
            self.assertIn("AI 参考分：缺失", text)
            self.assertIn("程序综合分不受影响", text)


if __name__ == "__main__":
    unittest.main()
