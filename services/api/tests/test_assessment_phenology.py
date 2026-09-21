"""旧评估报告也必须使用观测窗口，不能与新地块分析相矛盾。"""

import unittest
from datetime import date, timedelta

from app.reports.land_assessment.scoring import compute_assessment
from app.reports.land_assessment.charts import (
    pick_phenology_stages,
    pick_phenology_year,
)


class AssessmentPhenologyTests(unittest.TestCase):
    def rows(self, start):
        return [
            {
                "date": (start + timedelta(days=i * 10)).isoformat(),
                "layer_type": "NDVI",
                "mean": value,
                "quality_score": 0.9,
            }
            for i, value in enumerate(
                [0.18, 0.2, 0.23, 0.4, 0.52, 0.7, 0.78, 0.72, 0.56, 0.38, 0.2, 0.18]
            )
        ]

    def test_report_detects_spring_window_and_charts_use_real_dates(self):
        result = compute_assessment(
            self.rows(date(2025, 3, 1)), {}, {}, field_meta={"crop_type": "corn"}
        )
        window = result["risk"]["seasons"][0]
        self.assertLess(window["start"], "2025-06-01")
        self.assertLess(window["end"], "2025-07-01")
        self.assertEqual(result["scorecard"]["method"]["season_source"], "observed")
        year = pick_phenology_year(result["by_date"])
        stages = pick_phenology_stages(result["by_date"], year)
        self.assertEqual(stages["seedling"]["date"], window["start"])
        self.assertEqual(stages["peak"]["date"], window["peak"])
        self.assertEqual(stages["seedling"]["label"], "绿度起升")

    def test_cross_year_is_single_window(self):
        result = compute_assessment(self.rows(date(2024, 11, 1)), {}, {})
        self.assertEqual(len(result["risk"]["seasons"]), 1)
        self.assertEqual(result["risk"]["seasons"][0]["start"][:4], "2024")
        self.assertEqual(result["risk"]["seasons"][0]["end"][:4], "2025")

    def test_missing_quality_and_flat_curve_do_not_invent_seasons(self):
        for rows in [
            [],
            [{**r, "quality_score": 0.1} for r in self.rows(date(2025, 3, 1))],
            [{**r, "mean": 0.6} for r in self.rows(date(2025, 3, 1))],
        ]:
            result = compute_assessment(rows, {}, {})
            self.assertEqual(result["risk"]["seasons"], [])
            self.assertEqual(result["scorecard"]["method"]["season_source"], "unknown")
            self.assertIsNone(pick_phenology_year(result["by_date"]))


if __name__ == "__main__":
    unittest.main()
