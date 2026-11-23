"""Metric better/worse wording comes from the model response, not a local rule."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from app.reports.biweekly.narrative_ai import (
    generate_metric_judgment,
    judgment_paragraphs,
    normalize_judgment,
    section_lines,
)


def _facts() -> dict:
    return {
        "period": {"start": "2026-03-18", "end_inclusive": "2026-03-31"},
        "phase": {
            "observation_stages": [{"stage_code": "拔节孕穗期", "phase_label": "S2"}]
        },
        "report_metrics": {"primary": ["ndre", "evi"], "supporting": ["ndvi"]},
        "comparison": {
            "latest_date": "2026-03-31",
            "baseline_date": "2026-03-19",
            "metrics": {
                "ndre": {
                    "latest": 0.57,
                    "baseline": 0.40,
                    "delta": 0.17,
                    "direction": "up",
                    "statistic": "mean",
                },
                "evi": {
                    "latest": 0.71,
                    "baseline": 0.45,
                    "delta": 0.26,
                    "direction": "up",
                    "statistic": "mean",
                },
                "ndvi": {
                    "latest": 0.71,
                    "baseline": 0.57,
                    "delta": 0.14,
                    "direction": "up",
                    "statistic": "mean",
                },
            },
        },
    }


class NarrativeAiTests(unittest.TestCase):
    def test_missing_key_does_not_invent_better_or_worse(self) -> None:
        with patch.dict(os.environ, {"BAILIAN_API_KEY": ""}, clear=False):
            os.environ.pop("BAILIAN_API_KEY", None)
            out = generate_metric_judgment(_facts())
        self.assertFalse(out["llm_configured"])
        self.assertEqual(out["changes"], [])
        self.assertEqual(judgment_paragraphs(out), [])

    def test_model_judgment_is_colored_and_invented_numbers_are_dropped(self) -> None:
        payload = {
            "metrics": {
                "ndre": {"latest": 0.57, "delta": 0.17},
                "evi": {"latest": 0.71, "delta": 0.26},
            }
        }
        parsed = {
            "summary": "NDRE 升高 0.17，本期往好变化。",
            "changes": [
                {
                    "metric": "ndre",
                    "judgment": "better",
                    "sentence": "NDRE 升高 0.17，长势往好。",
                },
                {
                    "metric": "evi",
                    "judgment": "worse",
                    "sentence": "EVI 降到 9.99，往差。",
                },
                {"metric": "lai", "judgment": "better", "sentence": "新增指标"},
            ],
        }
        normalized = normalize_judgment(parsed, payload)
        self.assertEqual(
            [row["metric"] for row in normalized["changes"]], ["ndre", "evi"]
        )
        self.assertEqual(
            normalized["changes"][0]["sentence"], "NDRE 升高 0.17，长势往好。"
        )
        self.assertEqual(normalized["changes"][1]["sentence"], "")
        marked = "\n".join(
            judgment_paragraphs(
                {"summary": normalized["summary"], "changes": normalized["changes"]}
            )
        )
        self.assertIn("<font color='#1F4D38'><b>NDRE 往好变化</b></font>", marked)
        self.assertIn("<font color='#9C2B1B'><b>EVI 往差变化</b></font>", marked)
        self.assertNotIn("9.99", marked)
        parsed["conclusion"] = ["NDRE 往好变化，数值 0.57。", "另有一块地减产 3 成。"]
        parsed["weather"] = ["降水 12 毫米。"]
        again = normalize_judgment(parsed, payload)
        self.assertEqual(again["conclusion"], ["NDRE 往好变化，数值 0.57。"])
        self.assertEqual(again["weather"], [])
        painted = section_lines(again, "conclusion")
        self.assertIn("<font color='#1F4D38'><b>往好变化</b></font>", painted[0])

    def test_http_response_is_used_without_a_local_good_bad_rule(self) -> None:
        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "summary": "两项主要指标都在升高。",
                                        "changes": [
                                            {
                                                "metric": "ndre",
                                                "judgment": "better",
                                                "sentence": "NDRE 由 0.40 到 0.57。",
                                            }
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }

        class _Client:
            def post(self, url, headers, json):  # noqa: A002
                self.url = url
                self.body = json
                return _Response()

            def close(self) -> None:
                return None

        client = _Client()
        with patch.dict(
            os.environ,
            {
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
            },
        ):
            out = generate_metric_judgment(_facts(), client=client)
        self.assertEqual(out["model"], "deepseek-v4.1-flash-1017")
        self.assertEqual(out["changes"][0]["judgment"], "better")
        self.assertIn("0.57", out["changes"][0]["sentence"])
        prompt = client.body["messages"][0]["content"]
        self.assertIn("不足以判断", prompt)
        self.assertNotIn("delta > 0", prompt)
        self.assertNotIn('direction == "up"', prompt)
