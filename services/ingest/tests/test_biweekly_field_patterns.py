"""Persistent weak areas, uniformity and same-field historical archive.

样例是离线构造的像元，不是生产地块 32696 的结论。
"""

from __future__ import annotations

import unittest

import numpy as np

from app.reports.biweekly.field_patterns import (
    HISTORY_ABSENT,
    assess_field_patterns,
)


def _product(day: str, values: np.ndarray, *, lon0: float = 115.5) -> dict:
    rows, cols = values.shape
    lons, lats, column = [], [], []
    for r in range(rows):
        for c in range(cols):
            lons.append(lon0 + c * 0.000112)
            lats.append(36.5 - r * 0.00009)
            column.append(float(values[r, c]))
    return {
        "date": day,
        "sensor": "S2",
        "pixel_data": {"format": "lonlat_columns_v1", "columns": {"lon": lons, "lat": lats, "NDRE": column}},
    }


def _field(low: bool) -> np.ndarray:
    values = np.linspace(0.42, 0.74, 12 * 12).reshape(12, 12)
    if low:
        values[4:8, 4:8] = 0.08
    return values


def _facts(**extra) -> dict:
    facts = {
        "land_id": "LAYOUT-FIXTURE",
        "crop_name": "冬小麦",
        "crop_type": "冬小麦",
        "sowing_date": "2025-10-06",
        "period": {"start": "2026-05-06", "end_exclusive": "2026-05-20"},
        "report_metrics": {"primary": ["ndre"], "supporting": ["ndvi"]},
        "comparison": {
            "latest_date": "2026-05-18",
            "baseline_date": "2026-05-08",
            "metrics": {"ndre": {"latest": 0.55, "baseline": 0.62, "delta": -0.07, "statistic": "median"}},
        },
        "metric_series": [],
    }
    facts.update(extra)
    return facts


class PersistentWeakTests(unittest.TestCase):
    def test_three_dates_separate_persistent_from_occasional(self):
        products = [
            _product("2026-05-06", _field(True)),
            _product("2026-05-12", _field(True)),
            _product("2026-05-18", _field(False)),
        ]
        # 第三期把斑块抬高，只在前两期偏低的像元应记为偶发，而不是持续。
        products[2] = _product("2026-05-18", _field(True))
        occasional = [
            _product("2026-05-06", _field(False)),
            _product("2026-05-12", _field(False)),
            _product("2026-05-18", _field(True)),
        ]
        held = assess_field_patterns(_facts(), products)["persistent"][0]
        once = assess_field_patterns(_facts(), occasional)["persistent"][0]
        self.assertEqual(held["status"], "ok")
        self.assertGreater(held["n_persistent_clustered"], 0)
        self.assertEqual(once["n_persistent"], 0)
        self.assertGreater(once["n_occasional"], 0)
        self.assertIn("建议去地里看看确认一下", held["line"])
        self.assertNotIn("不判定", held["line"])
        self.assertNotIn("缺肥", held["line"])
        self.assertNotIn("病虫害", held["line"])

    def test_two_dates_are_not_called_persistent(self):
        products = [_product("2026-05-12", _field(True)), _product("2026-05-18", _field(True))]
        item = assess_field_patterns(_facts(), products)["persistent"][0]
        self.assertEqual(item["status"], "insufficient")
        self.assertIn("少于3期", item["line"])
        self.assertNotIn("东北部", item["line"])

    def test_unregistered_dates_are_insufficient(self):
        shifted = _product("2026-05-18", _field(True), lon0=116.2)
        products = [
            _product("2026-05-06", _field(True)),
            _product("2026-05-12", _field(True)),
            shifted,
        ]
        item = assess_field_patterns(_facts(), products)["persistent"][0]
        self.assertEqual(item["status"], "insufficient")
        self.assertIn("对不上同一块地方", item["line"])

    def test_small_grid_does_not_invent_a_region(self):
        tiny = np.linspace(0.2, 0.8, 16).reshape(4, 4)
        products = [_product(day, tiny) for day in ("2026-05-06", "2026-05-12", "2026-05-18")]
        item = assess_field_patterns(_facts(), products)["persistent"][0]
        self.assertEqual(item["status"], "insufficient")
        self.assertNotIn("成片", item["line"])


class UniformityTests(unittest.TestCase):
    def _series(self, spreads, counts):
        rows = []
        for index, (spread, count) in enumerate(zip(spreads, counts)):
            mid = 0.5
            rows.append(
                {
                    "date": f"2026-05-{6 + index:02d}",
                    "in_period": True,
                    "metrics": {
                        "ndre": {
                            "status": "ok",
                            "p10": mid - spread / 2,
                            "p90": mid + spread / 2,
                            "n_valid": count,
                            "median": mid,
                        }
                    },
                }
            )
        return rows

    def test_narrower_wider_and_similar(self):
        narrow = assess_field_patterns(_facts(metric_series=self._series([0.20, 0.08], [80, 80])))
        wide = assess_field_patterns(_facts(metric_series=self._series([0.08, 0.22], [80, 80])))
        same = assess_field_patterns(_facts(metric_series=self._series([0.12, 0.13], [80, 80])))
        self.assertEqual(narrow["uniformity"][0]["direction"], "narrower")
        self.assertIn("趋同", narrow["uniformity"][0]["line"])
        self.assertEqual(wide["uniformity"][0]["direction"], "wider")
        self.assertIn("分化", wide["uniformity"][0]["line"])
        self.assertEqual(same["uniformity"][0]["direction"], "similar")
        self.assertIn("各处差不多", same["uniformity"][0]["line"])
        for item in (narrow, wide, same):
            self.assertNotIn("百分位", item["uniformity"][0]["line"])
            self.assertNotIn("像元", item["uniformity"][0]["line"])

    def test_low_sample_does_not_claim_a_direction(self):
        item = assess_field_patterns(_facts(metric_series=self._series([0.20, 0.05], [12, 80])))
        self.assertEqual(item["uniformity"][0]["status"], "low_sample")
        self.assertNotIn("direction", item["uniformity"][0])
        self.assertIn("样本少，不判断", item["uniformity"][0]["line"])


class HistoricalArchiveTests(unittest.TestCase):
    def test_missing_archive_is_the_fixed_sentence(self):
        item = assess_field_patterns(_facts())
        self.assertEqual(item["history"]["status"], "absent")
        self.assertEqual(item["history_line"], HISTORY_ABSENT)

    def test_previous_period_is_not_a_prior_year(self):
        facts = _facts(
            historical_same_period=[
                {
                    "land_id": "LAYOUT-FIXTURE",
                    "crop_type": "冬小麦",
                    "sowing_date": "2025-10-06",
                    "observations": [
                        {
                            "date": "2026-04-20",
                            "metrics": {"ndre": {"median": 0.70, "n_valid": 400, "statistic": "median"}},
                        }
                    ],
                }
            ]
        )
        item = assess_field_patterns(facts)["history"]
        self.assertEqual(item["status"], "absent")
        self.assertIn("暂不评价", item["line"])
        self.assertNotIn("0.70", item["line"])

    def test_other_land_and_other_crop_are_not_compared(self):
        def archive(land, crop):
            return _facts(
                historical_same_period=[
                    {
                        "land_id": land,
                        "crop_type": crop,
                        "sowing_date": "2024-10-06",
                        "observations": [
                            {
                                "date": "2024-05-18",
                                "metrics": {"ndre": {"median": 0.62, "n_valid": 400, "statistic": "median"}},
                            }
                        ],
                    }
                ]
            )

        other_land = assess_field_patterns(archive("OTHER", "冬小麦"))["history"]
        other_crop = assess_field_patterns(archive("LAYOUT-FIXTURE", "玉米"))["history"]
        self.assertEqual(other_land["status"], "absent")
        self.assertEqual(other_crop["status"], "limited")
        self.assertIn("暂不评价", other_crop["line"])
        self.assertNotIn("0.62", other_crop["line"])

    def test_unknown_sowing_lists_values_without_ranking(self):
        facts = _facts(sowing_date=None)
        facts["historical_same_period"] = [
            {
                "land_id": "LAYOUT-FIXTURE",
                "crop_type": "冬小麦",
                "observations": [
                    {
                        "date": "2024-05-18",
                        "metrics": {"ndre": {"median": 0.62, "n_valid": 400, "statistic": "median"}},
                    }
                ],
            }
        ]
        item = assess_field_patterns(facts)["history"]
        self.assertEqual(item["status"], "limited")
        self.assertIn("0.62", item["line"])
        self.assertIn("播期未知", item["line"])
        self.assertIn("暂不评价", item["line"])
        self.assertNotIn("相差", item["line"])

    def test_comparable_archive_reports_the_difference_and_not_yield(self):
        facts = _facts()
        facts["historical_same_period"] = [
            {
                "land_id": "LAYOUT-FIXTURE",
                "crop_type": "小麦",
                "sowing_date": "2024-10-10",
                "observations": [
                    {
                        "date": "2024-05-16",
                        "metrics": {"ndre": {"median": 0.62, "n_valid": 380, "statistic": "median"}},
                    }
                ],
            }
        ]
        item = assess_field_patterns(facts)["history"]
        self.assertEqual(item["status"], "compared")
        self.assertIn("相差-0.07", item["line"])
        self.assertIn("不是产量", item["line"])
        self.assertNotIn("吨", item["line"])

    def test_low_archive_sample_is_not_scored(self):
        facts = _facts()
        facts["historical_same_period"] = [
            {
                "land_id": "LAYOUT-FIXTURE",
                "crop_type": "冬小麦",
                "sowing_date": "2024-10-06",
                "observations": [
                    {
                        "date": "2024-05-18",
                        "metrics": {"ndre": {"median": 0.62, "n_valid": 8, "statistic": "median"}},
                    }
                ],
            }
        ]
        item = assess_field_patterns(facts)["history"]
        self.assertEqual(item["reason"], "sample")
        self.assertIn("样本少", item["line"])
        self.assertNotIn("相差", item["line"])


if __name__ == "__main__":
    unittest.main()
