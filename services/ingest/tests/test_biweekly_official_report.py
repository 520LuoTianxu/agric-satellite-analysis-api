"""Download-machine official v8 biweekly PDF: bundled fonts, staged errors, API return."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agric_satellite_analysis_common.biweekly.models import ReportError


class BundledFontTests(unittest.TestCase):
    def test_resolves_bundled_fonts_without_env(self):
        from app.reports.biweekly.fonts import bundled_font_dir, resolve_yahei_fonts

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("BIWEEKLY_FONT")
        }
        with patch.dict(os.environ, env, clear=True):
            regular, bold = resolve_yahei_fonts()
        self.assertEqual(regular.parent, bundled_font_dir())
        self.assertEqual(regular.name, "msyh.ttf")
        self.assertEqual(bold.name, "msyhbd.ttf")
        self.assertTrue(regular.is_file())
        self.assertTrue(bold.is_file())

    def test_missing_override_dir_falls_back_to_bundled(self):
        from app.reports.biweekly.fonts import bundled_font_dir, resolve_yahei_fonts

        empty = tempfile.mkdtemp()
        with patch.dict(os.environ, {"BIWEEKLY_FONT_DIR": empty}):
            regular, _bold = resolve_yahei_fonts()
        self.assertEqual(regular.parent, bundled_font_dir())

    def test_missing_fonts_raise_font_missing(self):
        from app.reports.biweekly.fonts import resolve_yahei_fonts

        empty = Path(tempfile.mkdtemp())
        with patch("app.reports.biweekly.fonts.font_search_dirs", return_value=[empty]):
            with self.assertRaises(ReportError) as ctx:
                resolve_yahei_fonts()
        self.assertEqual(ctx.exception.code, "FONT_MISSING")


class OfficialPdfRenderTests(unittest.TestCase):
    def test_renders_six_page_pdf_with_bundled_fonts(self):
        from app.reports.biweekly.official_pdf import render_official_biweekly_pdf

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("BIWEEKLY_FONT")
        }
        facts = {
            "land_id": "6417",
            "land_name": "试验田6417",
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
        with patch.dict(os.environ, env, clear=True):
            path = render_official_biweekly_pdf(facts, dest)
        self.assertTrue(path.is_file())
        self.assertTrue(path.read_bytes().startswith(b"%PDF"))
        self.assertGreater(path.stat().st_size, 1000)


class GenerateTaskTests(unittest.TestCase):
    def test_all_failed_backfill_is_not_no_optics(self):
        from app.tasks.biweekly_report import generate_biweekly_report

        with (
            patch(
                "app.tasks.biweekly_report._wait_for_backfill",
                return_value={"job-a": "failed"},
            ),
            patch(
                "app.tasks.biweekly_report._complete", return_value={"ok": True}
            ) as complete,
            patch("app.core.job_http.update_job_record"),
            patch("app.core.job_http.fail_work_item_http"),
        ):
            result = generate_biweekly_report.run(
                job_id="report-1",
                land_id="19994",
                biweekly_job_id="bj-test",
                wait_for_job_ids=["job-a"],
                report_job_id="report-1",
            )
        self.assertEqual(result["error_code"], "BACKFILL_FAILED")
        kwargs = complete.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertEqual(kwargs["error_code"], "BACKFILL_FAILED")
        self.assertIn("补数", kwargs["note"])
        self.assertNotIn("没有可用光学观测，因此只给出证据受限", kwargs["note"])

    def test_scene_query_failure_is_not_evidence_limited(self):
        from app.tasks.biweekly_report import generate_biweekly_report

        with (
            patch("app.tasks.biweekly_report._wait_for_backfill", return_value={}),
            patch(
                "agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs",
                return_value={
                    "error_code": "SCENE_QUERY_FAILED",
                    "note": "读取已入库景失败，不能当作本期没有可用光学。",
                },
            ),
            patch(
                "app.tasks.biweekly_report._complete", return_value={"ok": True}
            ) as complete,
            patch("app.core.job_http.update_job_record"),
            patch("app.core.job_http.fail_work_item_http"),
        ):
            result = generate_biweekly_report.run(
                land_id="19994",
                biweekly_job_id="bj-test",
                report_job_id="report-1",
            )
        self.assertEqual(result["error_code"], "SCENE_QUERY_FAILED")
        self.assertEqual(complete.call_args.kwargs["status"], "failed")

    def test_true_no_optics_returns_evidence_limited_brief(self):
        from app.tasks.biweekly_report import generate_biweekly_report

        facts = {"brief": "本期没有可用光学观测。", "optical_days": []}
        oss = {
            "bucket": "agric-isolated-fixture",
            "key": "reports/default/19994/biweekly-bj-0123456789abcdef.json",
            "size_bytes": 32,
            "sha256": "ab" * 32,
            "content_type": "application/json",
            "kind": "evidence_limited_brief",
        }
        with (
            patch("app.tasks.biweekly_report._wait_for_backfill", return_value={}),
            patch(
                "agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs",
                return_value={
                    "error_code": None,
                    "status": "evidence_limited",
                    "facts": facts,
                },
            ),
            patch(
                "app.tasks.biweekly_report._complete",
                return_value={"ok": True, "job": {"status": "evidence_limited"}},
            ) as complete,
            patch(
                "app.tasks.biweekly_report._upload_official_object",
                return_value=oss,
            ),
            patch("app.core.job_http.update_job_record"),
            patch("app.core.job_http.complete_work_item_http"),
            patch("app.core.job_http.progress_work_item_http"),
        ):
            result = generate_biweekly_report.run(
                land_id="19994",
                biweekly_job_id="bj-0123456789abcdef",
                report_job_id="report-1",
            )
        self.assertEqual(result["status"], "evidence_limited")
        kwargs = complete.call_args.kwargs
        self.assertEqual(kwargs["status"], "evidence_limited")
        self.assertEqual(kwargs["kind"], "evidence_limited_brief")
        self.assertEqual(kwargs["oss"]["sha256"], oss["sha256"])
        self.assertIsNone(kwargs.get("content"))
        self.assertIn("没有可用光学", kwargs["note"])

    def test_ready_inputs_render_pdf_and_return_to_api(self):
        from app.tasks.biweekly_report import generate_biweekly_report

        facts = {
            "land_id": "6417",
            "period": {
                "start": "2026-04-04",
                "end_inclusive": "2026-04-17",
                "end_exclusive": "2026-04-18",
            },
            "phase": {
                "display_phase": "S2",
                "primary_metrics": ["ndre", "evi"],
                "supporting_metrics": ["ndvi", "ndmi"],
                "plan_source": "native",
            },
            "observations": [
                {
                    "date": "2026-04-09",
                    "sensor": "S2",
                    "ndvi_avg": 0.4,
                    "evi_avg": 0.3,
                    "ndre_avg": 0.2,
                    "ndmi_avg": 0.1,
                }
            ],
            "optical_days": ["2026-04-09"],
            "s1_count": 1,
            "in_period_radar": True,
        }

        def _render(payload, dest, **_kwargs):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(
                b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n"
            )
            return Path(dest)

        import sys
        from types import ModuleType

        gen_mod = ModuleType("app.reports.biweekly.generate")
        gen_mod.generate_official_biweekly_pdf = _render
        sys.modules["app.reports.biweekly.generate"] = gen_mod
        oss = {
            "bucket": "agric-isolated-fixture",
            "key": "reports/default/6417/biweekly-bj-0123456789abcdef.pdf",
            "size_bytes": 1200,
            "sha256": "cd" * 32,
            "content_type": "application/pdf",
            "kind": "official_visual_artifact",
        }
        with (
            patch(
                "app.tasks.biweekly_report._wait_for_backfill",
                return_value={"job-a": "completed"},
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.get_biweekly_report_inputs",
                return_value={"error_code": None, "status": "ready", "facts": facts},
            ),
            patch(
                "app.tasks.biweekly_report._upload_official_object",
                return_value=oss,
            ),
            patch(
                "app.tasks.biweekly_report._complete",
                return_value={"ok": True, "job": {"status": "ready"}},
            ) as complete,
            patch("app.core.job_http.update_job_record"),
            patch("app.core.job_http.complete_work_item_http"),
            patch("app.core.job_http.progress_work_item_http"),
        ):
            result = generate_biweekly_report.run(
                land_id="6417",
                biweekly_job_id="bj-0123456789abcdef",
                wait_for_job_ids=["job-a"],
                report_job_id="report-1",
            )
        self.assertEqual(result["status"], "ready")
        kwargs = complete.call_args.kwargs
        self.assertEqual(kwargs["status"], "ready")
        self.assertEqual(kwargs["kind"], "official_visual_artifact")
        self.assertEqual(kwargs["oss"]["key"], oss["key"])
        self.assertIsNone(kwargs.get("content"))
        self.assertIsNone(kwargs.get("error_code") or None)


if __name__ == "__main__":
    unittest.main()
