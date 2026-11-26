"""Sowing date and accumulated temperature passed to the biweekly conclusion."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.reports.biweekly.narrative_ai import (
    SYSTEM_PROMPT,
    conclusion_gaps,
    narrative_input,
)
from app.reports.biweekly.sowing_gdd import (
    antecedent_days_for,
    gdd_base_c,
    gdd_between,
    gdd_for_model,
    sowing_for_model,
)


def _facts() -> dict:
    return {
        "crop_type": "小麦",
        "period": {"start": "2026-11-01", "end_inclusive": "2026-11-14"},
        "phase": {"observation_stages": [{"stage_code": "出苗期"}]},
        "comparison": {"latest_date": "2026-11-11", "baseline_date": "2026-10-28"},
        "report_metrics": {"primary": ["ndvi"], "supporting": []},
        "weather_daily": [
            {
                "date": "2026-10-08",
                "min_c": 2.0,
                "max_c": 8.0,
                "precipitation_mm": 0.0,
            },
            {
                "date": "2026-10-09",
                "min_c": -2.0,
                "max_c": 4.0,
                "precipitation_mm": 0.0,
            },
            {
                "date": "2026-10-10",
                "min_c": None,
                "max_c": 9.0,
                "precipitation_mm": 1.0,
            },
            {
                "date": "2026-11-11",
                "min_c": 1.0,
                "max_c": 5.0,
                "precipitation_mm": 0.0,
            },
        ],
        "key_node_records": {
            "nodes": [
                {
                    "task_code": "PRECISION_SOWING",
                    "task_name": "精量播种",
                    "submitted_on": "2026-10-10",
                    "answers": [
                        {"label": "播种日期", "value": "2026-10-08"},
                        {"label": "品种", "value": "济麦22"},
                    ],
                }
            ]
        },
    }


class SowingGddTests(unittest.TestCase):
    def test_sowing_date_is_not_replaced_by_the_form_day(self) -> None:
        sowing = sowing_for_model(_facts())
        assert sowing is not None
        self.assertEqual(sowing["sown_on"], "2026-10-08")
        self.assertEqual(sowing["date_source"], "播种日期")
        self.assertEqual(sowing["days_to_latest"], 34)
        self.assertEqual(sowing["variety"], "济麦22")
        self.assertNotIn("recorded_on", sowing)

    def test_form_day_is_not_called_the_sowing_day(self) -> None:
        facts = _facts()
        facts["key_node_records"]["nodes"][0]["answers"] = []
        sowing = sowing_for_model(facts)
        assert sowing is not None
        self.assertNotIn("sown_on", sowing)
        self.assertEqual(sowing["recorded_on"], "2026-10-10")
        self.assertEqual(sowing["date_source"], "填写日期")

    def test_wheat_gdd_uses_zero_base_and_skips_missing_days(self) -> None:
        self.assertEqual(gdd_base_c(_facts()), 0.0)
        maize = _facts()
        maize["crop_type"] = "玉米"
        maize["phase"] = {"observation_stages": [{"stage_code": "苗期"}]}
        self.assertEqual(gdd_base_c(maize), 10.0)
        # 2 日均温 5℃，9 日均温 1℃，10 日缺最低气温不计入。基数 0。
        window = gdd_between(
            _facts()["weather_daily"], "2026-10-08", "2026-10-10", 0.0
        )
        assert window is not None
        self.assertEqual(window["sum"], "6.0")
        self.assertEqual(window["days"], 2)
        packed = gdd_for_model(_facts())
        assert packed is not None
        self.assertEqual(packed["base_c"], 0)
        self.assertEqual(packed["since_sowing"]["sum"], "9.0")
        self.assertEqual(packed["since_sowing"]["days"], 3)
        self.assertTrue(packed["since_sowing"]["covers_sowing_day"])
        self.assertEqual(packed["since_sowing"]["to"], "2026-11-11")

    def test_below_base_day_counts_as_zero_not_as_missing(self) -> None:
        rows = [
            {"date": "2026-11-02", "min_c": 0.0, "max_c": 8.0},
        ]
        window = gdd_between(rows, "2026-11-02", "2026-11-02", 10.0)
        assert window is not None
        self.assertEqual(window["sum"], "0.0")
        self.assertEqual(window["days"], 1)

    def test_payload_gives_sowing_and_gdd_to_the_model(self) -> None:
        self.assertIn("积温", SYSTEM_PROMPT)
        self.assertIn("不要编一个该出苗的积温标准", SYSTEM_PROMPT)
        payload = narrative_input(_facts())
        self.assertEqual(payload["sowing"]["sown_on"], "2026-10-08")
        self.assertEqual(payload["gdd"]["since_sowing"]["sum"], "9.0")
        task = "\n".join(payload["conclusion_task"])
        self.assertIn("播种", task)
        self.assertIn("积温", task)
        self.assertIn("积温", payload["parameter_guide"]["gdd"])
        gaps = conclusion_gaps(
            {"conclusion": ["西北部已经有植被信号，东部仍看不出苗。"]},
            payload,
        )
        self.assertIn("sowing", gaps)
        self.assertIn("gdd", gaps)
        covered = conclusion_gaps(
            {
                "conclusion": [
                    "10月8日播种，到11月11日过了34天，积温9.0，偏低，出苗会慢。"
                    "西北部已经有植被信号，东部仍看不出苗。"
                ]
            },
            payload,
        )
        self.assertNotIn("sowing", covered)
        self.assertNotIn("gdd", covered)

    def test_sowing_extends_the_temperature_window(self) -> None:
        from app.reports.biweekly.enrichment import enrich_official_report_facts

        calls: dict = {}

        def _weather(**kwargs):
            calls.update(kwargs)
            return {
                "weather_daily": [
                    {
                        "date": "2026-10-01",
                        "min_c": 4.0,
                        "max_c": 14.0,
                        "precipitation_mm": 0.0,
                        "et0_mm": 1.0,
                    }
                ],
                "weather_source": "open-meteo",
                "weather_error": None,
            }

        facts = _facts()
        facts["key_node_records"]["nodes"][0]["answers"][0]["value"] = "2026-09-20"
        facts["period"] = {
            "start": "2026-11-20",
            "end_inclusive": "2026-12-03",
            "end_exclusive": "2026-12-04",
        }
        facts["weather_daily"] = [
            {
                "date": "2026-11-01",
                "min_c": 1.0,
                "max_c": 9.0,
                "precipitation_mm": 0.0,
                "et0_mm": 1.0,
            },
            {
                "date": "2026-11-21",
                "min_c": 0.0,
                "max_c": 8.0,
                "precipitation_mm": 1.0,
                "et0_mm": 1.0,
            },
        ]
        facts["weather_period"] = {
            "days": 1,
            "min_c": 0.0,
            "max_c": 8.0,
            "precipitation_mm": 1.0,
            "mean_c": 4.0,
        }
        self.assertGreaterEqual(
            antecedent_days_for(facts, "2026-11-20"), 50
        )
        filled = enrich_official_report_facts(
            facts,
            figures_dir=Path(tempfile.mkdtemp()),
            products=[],
            latitude=37.25,
            longitude=112.08,
            fetch_weather=_weather,
            fetch_truecolor=lambda **_k: None,
        )
        self.assertGreaterEqual(calls["antecedent_days"], 50)
        self.assertEqual(calls["period_start"], "2026-11-20")
        days = [row["date"] for row in filled["weather_daily"]]
        self.assertIn("2026-10-01", days)
        self.assertIn("2026-11-21", days)
