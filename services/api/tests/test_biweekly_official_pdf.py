"""Official six-page biweekly PDF: phase metrics, 参照 labeling, missing-science notes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agric_satellite_analysis_common.biweekly.models import ReportError

from app.reports.biweekly.fonts import resolve_yahei_fonts
from app.reports.biweekly.official_pdf import render_official_biweekly_pdf


def _facts(*, reference: bool = False, include_weather: bool = True) -> dict:
    phase = {
        "display_phase": "S2",
        "primary_metrics": ["ndre", "evi"],
        "supporting_metrics": ["ndvi", "ndmi"],
        "phenology_basis": (
            "PLAN_DERIVED_FROM_BTU_REFERENCE"
            if reference
            else "PLAN_DERIVED_FROM_SMART_PLANT_TASK"
        ),
        "plan_source": "reference" if reference else "native",
        "plan_label": "参照方案" if reference else "本田方案",
        "policy_version": "biweekly-wheat-display-v8-1",
        "reference": {
            "planting_instance_id": "15933",
            "name": "15933",
            "disclosure": "参照方案，须按参照阅读，不是本田已关联方案。",
        }
        if reference
        else None,
    }
    weather_daily = []
    if include_weather:
        weather_daily = [
            {
                "date": "2026-04-04",
                "precipitation_sum": 0.0,
                "et0_fao_mm": 3.1,
                "temperature_2m_min": 8.0,
                "temperature_2m_max": 18.0,
            },
            {
                "date": "2026-04-10",
                "precipitation_sum": 2.0,
                "et0_fao_mm": 3.4,
                "temperature_2m_min": 7.0,
                "temperature_2m_max": 20.0,
            },
        ]
    return {
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
        "phase": phase,
        "cutoff": "2026-04-18T00:00:00+08:00",
        "optical_days": ["2026-04-09", "2026-04-14"],
        "observations": [
            {
                "date": "2026-04-09",
                "sensor": "S2",
                "ndvi_avg": 0.42,
                "evi_avg": 0.31,
                "ndre_avg": 0.22,
                "ndmi_avg": 0.11,
            },
            {
                "date": "2026-04-14",
                "sensor": "S2",
                "ndvi_avg": 0.45,
                "evi_avg": 0.33,
                "ndre_avg": 0.24,
                "ndmi_avg": 0.12,
            },
        ],
        "prior_comparison": {
            "period_id": "11",
            "all_compared_higher": True,
            "can_claim_sustained_improvement": True,
            "metrics": {
                "ndre": {"current": 0.23, "prior": 0.19, "delta": 0.04},
                "evi": {"current": 0.32, "prior": 0.28, "delta": 0.04},
            },
        },
        "s1_count": 1,
        "in_period_radar": True,
        "weather_daily": weather_daily,
        "weather_period": {"precipitation_mm": 2.0, "et0_mm": 6.5} if include_weather else None,
        "weather_antecedent": {"precipitation_mm": 12.0, "et0_mm": 40.0} if include_weather else None,
        "spatial_variation": None,
        "surrounding_comparison": None,
        "forbidden_metrics_omitted": ["fvc", "lai"],
    }


def _pdf_text(path: Path) -> str:
    try:
        import subprocess

        proc = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout
    except FileNotFoundError:
        pass
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return path.read_bytes().decode("latin-1", errors="ignore")


@unittest.skipUnless(
    Path("/workspace/fonts/msyh.ttf").is_file() and Path("/workspace/fonts/msyhbd.ttf").is_file(),
    "Microsoft YaHei fonts are not on this machine",
)
class OfficialBiweeklyPdfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="official-pdf-")

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_yahei_fonts_from_workspace(self):
        regular, bold = resolve_yahei_fonts()
        self.assertTrue(regular.is_file())
        self.assertTrue(bold.is_file())
        self.assertEqual(regular.name, "msyh.ttf")
        self.assertEqual(bold.name, "msyhbd.ttf")

    def test_reference_plan_is_labeled_and_missing_maps_are_disclosed(self):
        dest = Path(self.tmp.name) / "ref.pdf"
        path = render_official_biweekly_pdf(_facts(reference=True), dest)
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 1000)
        self.assertTrue(path.read_bytes().startswith(b"%PDF"))
        text = _pdf_text(path)
        self.assertIn("参照", text)
        self.assertIn("归一化差值红边指数（NDRE）", text)
        self.assertIn("增强型植被指数（EVI）", text)
        self.assertNotIn("绿度", text)
        self.assertNotIn("叶片营养", text)
        self.assertIn("缺少", text)
        self.assertNotIn("算法", text)
        self.assertNotIn("FVC", text)
        self.assertNotIn("LAI", text)

    def test_native_plan_is_not_labeled_as_reference(self):
        dest = Path(self.tmp.name) / "native.pdf"
        path = render_official_biweekly_pdf(_facts(reference=False), dest)
        text = _pdf_text(path)
        self.assertIn("本田方案", text)
        self.assertNotIn("参照方案", text)

    def test_empty_facts_fail_as_generate_failed(self):
        with self.assertRaises(ReportError) as ctx:
            render_official_biweekly_pdf({}, Path(self.tmp.name) / "empty.pdf")
        self.assertEqual(ctx.exception.code, "GENERATE_FAILED")


class OfficialBiweeklyFontMissingTests(unittest.TestCase):
    def test_missing_fonts_raise_font_missing(self):
        empty = Path(tempfile.mkdtemp())
        with patch("app.reports.biweekly.fonts.font_search_dirs", return_value=[empty]):
            with self.assertRaises(ReportError) as ctx:
                resolve_yahei_fonts()
        self.assertEqual(ctx.exception.code, "FONT_MISSING")


if __name__ == "__main__":
    unittest.main()
