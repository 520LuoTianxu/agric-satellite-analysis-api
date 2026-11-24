"""Water-risk text must follow the evidence that is already on the facts.

离线数字只验证分支，不是生产地块 32696 的旱涝结论。
"""

from __future__ import annotations

import unittest

from app.reports.biweekly.water_risk import assess_water_risk


def _facts(**extra) -> dict:
    facts = {
        "land_id": "LAYOUT-FIXTURE",
        "period": {"start": "2026-05-06", "end_exclusive": "2026-05-20", "end_inclusive": "2026-05-19"},
        "comparison": {
            "latest_date": "2026-05-18",
            "baseline_date": "2026-05-08",
            "metrics": {
                "ndvi": {"latest": 0.72, "baseline": 0.70, "delta": 0.02, "statistic": "median"},
                "ndmi": {"latest": 0.46, "baseline": 0.40, "delta": 0.06, "statistic": "median"},
            },
        },
        "s1_count": 0,
        "data_quality": {"radar_count": 0},
    }
    facts.update(extra)
    return facts


def _joined(result: dict) -> str:
    return "".join(result["lines"])


class WaterRiskTests(unittest.TestCase):
    def test_missing_ndmi_and_weather_is_insufficient_and_not_no_risk(self):
        result = assess_water_risk(
            {
                "comparison": None,
                "s1_count": 0,
                "data_quality": {},
                "weather_period": {"days": 0, "precipitation_mm": None, "et0_mm": None},
            }
        )
        text = _joined(result)
        self.assertEqual(result["status"], "insufficient")
        self.assertIn("证据不足", text)
        self.assertIn("不判定干旱或洪涝", text)
        self.assertIn("不能写成无风险", text)
        self.assertNotIn("未见干旱", text)
        self.assertNotIn("无旱", text)
        self.assertIsNone(result["balance_mm"])

    def test_incomplete_weather_does_not_invent_a_balance(self):
        result = assess_water_risk(
            _facts(
                weather_period={
                    "days": 14,
                    "precipitation_mm": 8.6,
                    "precipitation_days": 12,
                    "et0_mm": 40.0,
                    "et0_days": 11,
                    "missing_precipitation": True,
                    "missing_et0": True,
                }
            )
        )
        text = _joined(result)
        self.assertEqual(result["status"], "described")
        self.assertIsNone(result["balance_mm"])
        self.assertFalse(result["conflict"])
        self.assertIn("不计算水分收支", text)
        self.assertIn("有效降雨", text)
        self.assertNotIn("雨水", text)
        self.assertNotIn("账", text)
        self.assertIn("0.46", text)
        self.assertIn("不判定干旱", text)
        self.assertNotIn("8.6-40", text)

    def test_opposite_ndmi_and_water_balance_stay_unmerged(self):
        result = assess_water_risk(
            _facts(
                weather_period={
                    "days": 14,
                    "precipitation_mm": 6.0,
                    "precipitation_days": 14,
                    "et0_mm": 42.0,
                    "et0_days": 14,
                    "missing_precipitation": False,
                    "missing_et0": False,
                }
            )
        )
        text = _joined(result)
        self.assertTrue(result["conflict"])
        self.assertEqual(result["balance_mm"], -36.0)
        self.assertIn("方向不一致", text)
        self.assertIn("+0.06", text)
        self.assertIn("-36.0", text)
        self.assertIn("不判定干旱", text)
        self.assertNotIn("确诊", text)

    def test_radar_without_backscatter_cannot_clear_or_confirm_water(self):
        result = assess_water_risk(_facts(s1_count=2, data_quality={"radar_count": 2}))
        text = _joined(result)
        self.assertIn("不能确认或排除积水", text)
        self.assertNotIn("无积水", text)
        self.assertEqual(result["radar_vv_count"], 0)

    def test_flood_rule_is_quoted_not_treated_as_a_loss(self):
        rows = [{"date": f"2026-05-{day:02d}", "vv": -10.0, "vh": -16.0, "scene_id": f"S1A_{day}"} for day in (6, 8, 10)]
        rows.append({"date": "2026-05-18", "vv": -21.0, "vh": -24.0, "scene_id": "S1A_18"})
        result = assess_water_risk(
            _facts(radar_observations=rows, s1_count=4, data_quality={"radar_count": 4})
        )
        text = _joined(result)
        self.assertIn("积水候选", text)
        self.assertIn("不是灾损认定", text)
        self.assertIn("不判定干旱或洪涝", text)
        self.assertNotIn("受灾面积", text)

    def test_dry_rule_is_not_reported_as_no_risk(self):
        rows = [
            {"date": f"2026-05-{day:02d}", "vv": -10.0, "vh": -16.0, "scene_id": f"S1A_{day}"}
            for day in (6, 8, 10, 18)
        ]
        result = assess_water_risk(
            _facts(radar_observations=rows, s1_count=4, data_quality={"radar_count": 4})
        )
        text = _joined(result)
        self.assertIn("不等于没有积水风险", text)
        self.assertIn("不能写成无风险", text)
        self.assertNotIn("没有洪涝", text)
        self.assertNotIn("未见积水", text)

    def test_mndwi_fraction_is_not_called_area(self):
        columns = {
            "lon": [115.5] * 40,
            "lat": [36.5] * 40,
            "MNDWI": [0.2] * 10 + [-0.3] * 30,
            "clear": [1] * 40,
        }
        product = {"date": "2026-05-18", "pixel_data": {"columns": columns}}
        text = _joined(assess_water_risk(_facts(), [product]))
        self.assertIn("水体指数", text)
        self.assertNotIn("MNDWI", text)
        self.assertIn("不是积水面积", text)
        self.assertNotIn("亩", text)


if __name__ == "__main__":
    unittest.main()
