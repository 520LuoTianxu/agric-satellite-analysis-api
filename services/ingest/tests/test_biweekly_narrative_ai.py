"""Metric better/worse wording comes from the model response, not a local rule."""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import httpx
import structlog

from agric_satellite_analysis_common.logging import processor_chain
from app.reports.biweekly.narrative_ai import (
    SYSTEM_PROMPT,
    conclusion_gaps,
    generate_metric_judgment,
    judgment_paragraphs,
    narrative_input,
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
    def test_payload_explains_parameters_and_rounds_values(self) -> None:
        facts = _facts()
        facts["infield"] = [
            {
                "metric": "ndre",
                "stats": {
                    "median": 0.5712,
                    "p10": 0.506396,
                    "p90": 0.6045,
                    "valid_fraction": 0.98,
                },
                "relative": {
                    "flagged_pixels": 80,
                    "clustered_pixels": 78,
                    "regions": [{"name": "西南部", "share": 0.4}],
                },
            }
        ]
        facts["infield"][0]["spatial_split"] = "split"
        facts["infield"][0]["zones"] = [
            {
                "name": "西南部",
                "level": "lower",
                "median": 0.41,
                "valid_fraction": 0.96,
                "area_fraction": 0.18,
                "delta_median": -0.06,
            }
        ]
        payload = narrative_input(facts)
        self.assertIn("最近一次有效观测", payload["parameter_guide"]["metrics"])
        self.assertIn("有效降雨", payload["parameter_guide"]["weather"])
        self.assertIn("雨水账", payload["parameter_guide"]["weather"])
        self.assertNotIn("建议去地里看看确认一下", SYSTEM_PROMPT)
        self.assertNotIn("建议去地里看看确认一下", payload["parameter_guide"]["infield"])
        self.assertNotIn(
            "建议去地里看看确认一下", payload["parameter_guide"]["surrounding"]
        )
        self.assertIn("出苗", SYSTEM_PROMPT)
        self.assertIn("收获", SYSTEM_PROMPT)
        self.assertIn("方位", payload["parameter_guide"]["infield"])
        self.assertIn("compare_with", payload["parameter_guide"]["key_nodes"])
        self.assertNotIn("先别下结论", payload["parameter_guide"]["surrounding"])
        self.assertNotIn("四段", payload["parameter_guide"]["infield"])
        self.assertIn("不要按", SYSTEM_PROMPT)
        self.assertIn("写进结论", SYSTEM_PROMPT)
        self.assertNotIn("通常两三句", SYSTEM_PROMPT)
        self.assertIn("六到十句", SYSTEM_PROMPT)
        self.assertNotIn("通常是几句到一小段", SYSTEM_PROMPT)
        self.assertIn("写进结论", payload["parameter_guide"]["weather"])
        self.assertIn("similar", payload["parameter_guide"]["infield"])
        self.assertIn("NDRE", payload["stage_context"]["ask"])
        self.assertTrue(payload["conclusion_task"])
        self.assertEqual(payload["metrics"]["ndre"]["latest"], "0.57")
        self.assertIn("NDRE", payload["metrics"]["ndre"]["name"])
        self.assertIn("叶绿素", payload["metrics"]["ndre"]["meaning"])
        self.assertNotIn("绿度", payload["metrics"]["ndre"]["name"])
        self.assertEqual(payload["infield"][0]["p10"], "0.51")
        self.assertEqual(payload["infield"][0]["regions"][0]["share_percent"], 40)
        self.assertEqual(payload["infield"][0]["spatial_split"], "split")
        self.assertEqual(payload["infield"][0]["zones"][0]["name"], "西南部")
        self.assertEqual(payload["infield"][0]["zones"][0]["median"], "0.41")
        self.assertEqual(payload["infield"][0]["zones"][0]["level"], "lower")
        self.assertEqual(payload["stage_context"]["stage"], "拔节孕穗期")
        self.assertIn("NDRE", payload["stage_context"]["expected"])

    def test_stage_tasks_and_patterns_are_evidence_not_a_script(self) -> None:
        facts = _facts()
        facts["phase"] = {
            "observation_stages": [{"stage_code": "出苗期", "phase_label": "S1"}]
        }
        facts["key_node_records"] = {
            "nodes": [
                {
                    "task_name": "出苗水",
                    "task_code": "EMERGENCE_WATER",
                    "submitted_on": "2026-03-20",
                    "answers": [{"label": "说明", "value": "已灌一遍"}],
                },
                {
                    "task_name": "收获",
                    "task_code": "HARVEST",
                    "submitted_on": "2026-03-22",
                    "answers": [{"label": "说明", "value": "西南部已收"}],
                },
            ]
        }
        facts["field_patterns"] = {
            "persistent": [
                {
                    "metric": "ndvi",
                    "status": "ok",
                    "dates": ["2026-03-01", "2026-03-10", "2026-03-20"],
                    "regions": [{"name": "西南部", "share": 0.7}],
                    "n_persistent": 12,
                    "n_persistent_clustered": 12,
                }
            ],
            "uniformity": [
                {
                    "metric": "ndvi",
                    "status": "ok",
                    "direction": "wider",
                    "dates": ["2026-03-10", "2026-03-20"],
                    "spread": [0.08, 0.22],
                }
            ],
            "history": {"status": "absent", "line": "历史可比数据不足，暂不评价。"},
            "persistent_lines": ["持续偏弱（NDVI）：西南部有一小片一直比整块地弱。"],
        }
        payload = narrative_input(facts)
        self.assertEqual(payload["stage_context"]["stage"], "出苗期")
        self.assertIn("不能判断出苗", payload["stage_context"]["expected"])
        self.assertIn("哪些方位已经出苗", payload["stage_context"]["ask"])
        self.assertIn("重点看", "\n".join(payload["conclusion_task"]))
        packed = json.dumps(payload["field_patterns"], ensure_ascii=False)
        self.assertNotIn("持续偏弱（NDVI）", packed)
        self.assertNotIn("暂不评价", packed)
        self.assertEqual(payload["field_patterns"]["persistent"][0]["regions"], ["西南部"])
        self.assertEqual(payload["field_patterns"]["uniformity"][0]["direction"], "wider")
        self.assertEqual(payload["field_patterns"]["uniformity"][0]["spread"], ["0.08", "0.22"])
        self.assertEqual(payload["field_patterns"]["history_status"], "absent")
        hints = {node["task_name"]: node.get("compare_with") for node in payload["key_nodes"]}
        self.assertIn("NDMI", hints["出苗水"])
        self.assertIn("低覆盖", hints["收获"])
        self.assertNotIn("EMERGENCE_WATER", json.dumps(payload["key_nodes"], ensure_ascii=False))
        cleaned = normalize_judgment(
            {
                "conclusion": [
                    "西南部 NDVI 仍低，建议去地里看看确认一下。",
                    "建议去地里看看确认一下。",
                    "东北部这次看不清。",
                ]
            },
            {"metrics": {}},
        )
        self.assertEqual(cleaned["conclusion"], ["西南部 NDVI 仍低。", "东北部这次看不清。"])
        many = normalize_judgment(
            {
                "conclusion": ["西南部仍低。"] * 10,
                "field_notes": ["看西南部。"] * 10,
            },
            {"metrics": {}},
        )
        self.assertEqual(len(many["conclusion"]), 10)
        self.assertEqual(len(many["field_notes"]), 8)

    def test_thin_conclusion_is_asked_to_add_weather_soil_and_quiet_zones(self) -> None:
        facts = _facts()
        facts["phase"] = {
            "observation_stages": [{"stage_code": "出苗期", "phase_label": "S1"}]
        }
        facts["weather_period"] = {
            "days": 14,
            "min_c": -1.2,
            "max_c": 11.0,
            "mean_c": 4.5,
            "precipitation_mm": 3.2,
            "temperature_days": 14,
            "precipitation_days": 2,
        }
        facts["weather_same_period_prior_year"] = {
            "mean_c": 6.1,
            "precipitation_mm": 18.4,
        }
        facts["soil_context"] = {
            "status": "ready",
            "texture": "壤土",
            "ph": 7.2,
            "rootzone_awc_mm": 86,
        }
        facts["infield"] = [
            {
                "metric": "ndvi",
                "spatial_split": "split",
                "stats": {"median": 0.17, "p10": 0.12, "p90": 0.35},
                "zones": [
                    {"name": "西北部", "level": "higher", "median": 0.41},
                    {"name": "东部", "level": "similar", "median": 0.16},
                ],
            }
        ]
        payload = narrative_input(facts)
        thin = {
            "summary": "出苗期西北部已有信号。",
            "changes": [
                {"metric": "ndvi", "judgment": "unclear", "sentence": "NDVI 为 0.71。"}
            ],
            "conclusion": ["出苗期西北部 0.41 已有信号，其余方位仍低。"],
            "field_notes": [],
            "metric_notes": ["NDVI 本期是 0.71。"],
            "infield": [{"metric": "ndvi", "text": "西北部 0.41，东部 0.16。"}],
            "weather": ["这期有效降雨 3.20 毫米。"],
        }
        self.assertEqual(
            conclusion_gaps(
                normalize_judgment(thin, payload),
                payload,
            ),
            ["weather", "soil", "emergence", "length"],
        )
        fuller_lines = [
            "出苗期西北部 0.41 已有植被信号，东部 0.16 仍看不出苗，要重点看东部。",
            "全田 NDVI 是 0.71，东部 0.16 还没有跟上来。",
            "本期平均气温 4.50 度，有效降雨 3.20 毫米，比去年少。",
            "土壤是壤土，东部在这期雨少时更要盯。",
            "西北部 0.41 已经有信号，不要把全田偏低单独写成苗弱。",
            "这期先看东部苗有没有出来，再看西北部 0.41 是不是还在。",
        ]
        fuller = {
            "summary": "出苗期西北部已有信号，东部还没有。",
            "changes": [
                {"metric": "ndvi", "judgment": "unclear", "sentence": "NDVI 为 0.71。"}
            ],
            "conclusion": fuller_lines,
            "field_notes": ["重点看东部，看苗有没有出来。"],
            "metric_notes": ["NDVI 本期是 0.71。"],
            "infield": [{"metric": "ndvi", "text": "西北部 0.41，东部 0.16。"}],
            "weather": ["这期有效降雨 3.20 毫米，平均气温 4.50 度。"],
        }
        bodies = [thin, fuller]

        class _Response:
            def __init__(self, content: dict) -> None:
                self.status_code = 200
                self._content = content

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(self._content, ensure_ascii=False)
                            },
                        }
                    ],
                    "usage": {"completion_tokens": 400},
                }

        class _Client:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def post(self, url, headers, json):  # noqa: A002
                self.calls.append(json)
                return _Response(bodies[len(self.calls) - 1])

            def close(self) -> None:
                return None

        client = _Client()
        with patch.dict(
            os.environ,
            {
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_MODEL": "qwen3.7-flash",
            },
        ):
            out = generate_metric_judgment(facts, client=client)
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("上一稿", client.calls[0]["messages"][1]["content"])
        repair = client.calls[1]["messages"][1]["content"]
        self.assertIn("上一稿结论没有写全", repair)
        self.assertIn("有效降雨", repair)
        self.assertIn("仍看不出苗", repair)
        self.assertIn("六到十句", repair)
        self.assertIsNone(out["error"])
        written = "\n".join(out["conclusion"])
        self.assertIn("东部", written)
        self.assertIn("西北部", written)
        self.assertIn("有效降雨", written)
        self.assertIn("壤土", written)
        self.assertNotIn("conclusion_gaps", out["diagnostics"])

    def test_empty_field_notes_do_not_fail_the_narrative(self) -> None:
        content = {
            "summary": "NDRE 为 0.57。",
            "changes": [
                {
                    "metric": "ndre",
                    "judgment": "better",
                    "sentence": "NDRE 为 0.57。",
                }
            ],
            "conclusion": ["NDRE 为 0.57。"],
            "field_notes": [],
            "metric_notes": ["NDRE 本期是 0.57。"],
        }
        out, _client = self._judge(
            {"content": json.dumps(content, ensure_ascii=False)}
        )
        self.assertIsNone(out["error"])
        self.assertEqual(out["field_notes"], [])
        self.assertEqual(out["conclusion"], ["NDRE 为 0.57。"])

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
        self.assertNotIn("backColor", marked)
        self.assertNotIn("9.99", marked)
        self.assertIn("长势往好", marked)
        written = (
            "<b>NDRE</b> 为 "
            '<font face="YaHei-Bold" color="#1F4D38" backColor="#D9F2E3">0.57</font>，'
            "这处冠层起来了。"
            "<font color='#0000FF'>蓝</font>"
        )
        shown = section_lines({"conclusion": [written]}, "conclusion")[0]
        self.assertIn("<b>NDRE</b>", shown)
        self.assertIn('color="#1F4D38"', shown)
        self.assertIn('backColor="#D9F2E3"', shown)
        self.assertNotIn("0000FF", shown)
        plain = section_lines(
            {"conclusion": ["本期往好变化，数值 0.57。"]}, "conclusion"
        )[0]
        self.assertEqual(plain, "本期往好变化，数值 0.57。")
        parsed["conclusion"] = ["NDRE 往好变化，数值 0.57。", "另有一块地减产 3 成。"]
        parsed["weather"] = ["降水 12 毫米。"]
        again = normalize_judgment(parsed, payload)
        self.assertEqual(again["conclusion"], ["NDRE 往好变化，数值 0.57。"])
        self.assertEqual(again["weather"], [])
        parsed["conclusion"] = [
            "这是缺肥。",
            "不判定缺肥。",
            "本期雨水少，要留意浇水。",
            "地里东北部有一小片一直比整块地弱，建议抽空去田里看看那一块，但先别急着下结论说缺什么或闹病。",
            "和周围大约一公里的农田比，这块地稍微弱一点，不过周围种的不一定和咱一样，只能当个参考。",
        ]
        diagnosed = normalize_judgment(parsed, payload)
        self.assertEqual(
            diagnosed["conclusion"],
            [
                "本期有效降雨少，要留意浇水。",
                "地里东北部有一小片一直比整块地弱，建议抽空去田里看看那一块。",
                "和周围大约一公里的农田比，这块地稍微弱一点，不过周围种的不一定和咱一样。",
            ],
        )
        self.assertNotIn("先别", "\n".join(diagnosed["conclusion"]))
        self.assertNotIn("只能当", "\n".join(diagnosed["conclusion"]))
        self.assertNotIn("雨水", "\n".join(diagnosed["conclusion"]))

    def test_http_response_is_used_without_a_local_good_bad_rule(self) -> None:
        class _Response:
            status_code = 200

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
                                        "conclusion": ["NDRE 由 0.40 到 0.57。"],
                                        "field_notes": ["西南部值得看一眼。"],
                                        "metric_notes": ["NDRE 本期是 0.57。"],
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
            os.environ.pop("BAILIAN_MAX_TOKENS", None)
            os.environ.pop("BAILIAN_TIMEOUT_S", None)
            out = generate_metric_judgment(_facts(), client=client)
        self.assertEqual(client.body["max_tokens"], 16384)
        self.assertIs(client.body["enable_thinking"], False)
        self.assertNotIn("thinking_budget", client.body)
        self.assertEqual(client.body["model"], "deepseek-v4.1-flash-1017")
        self.assertEqual(out["model"], "deepseek-v4.1-flash-1017")
        self.assertEqual(out["changes"][0]["judgment"], "better")
        self.assertIn("0.57", out["changes"][0]["sentence"])
        prompt = client.body["messages"][0]["content"]
        self.assertIn("由你决定", prompt)
        self.assertNotIn("往好变化必须", prompt)
        self.assertIn("#1F4D38", prompt)
        self.assertIn("<b>", prompt)
        self.assertIn("本期事实", prompt)
        self.assertIn("metric_notes", prompt)
        self.assertIn("backColor", prompt)
        self.assertIn("生育阶段", prompt)
        self.assertNotIn("建议去地里看看确认一下", prompt)
        self.assertNotIn("delta > 0", prompt)
        self.assertNotIn('direction == "up"', prompt)
        user = client.body["messages"][1]["content"]
        self.assertIn("参数说明", user)
        self.assertIn("最近一次有效观测", user)
        self.assertIn("0.57", user)
        self.assertNotIn("0.506", user)

    def _capture(self):
        buf = io.StringIO()
        previous = structlog.get_config()
        structlog.configure(
            processors=processor_chain(),
            wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(file=buf),
            cache_logger_on_first_use=False,
        )
        import app.reports.biweekly.narrative_ai as narrative

        narrative.logger = structlog.get_logger("narrative-fixture")
        return buf, previous, narrative

    def _restore(self, previous, narrative) -> None:
        structlog.configure(**previous)
        narrative.logger = structlog.get_logger("biweekly.narrative")

    def _judge(self, message: dict, *, usage=None, finish_reason="stop", env=None):
        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                body = {
                    "choices": [{"finish_reason": finish_reason, "message": message}]
                }
                if usage is not None:
                    body["usage"] = usage
                return body

        class _Client:
            def post(self, url, headers, json):  # noqa: A002
                self.calls.append(json)
                self.body = json
                self.headers = headers
                return _Response()

            def close(self) -> None:
                return None

        client = _Client()
        client.calls = []
        values = {
            "BAILIAN_API_KEY": "test-key",
            "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
        }
        values.update(env or {})
        with patch.dict(os.environ, values, clear=False):
            for name in (
                "BAILIAN_MAX_TOKENS",
                "BAILIAN_TIMEOUT_S",
                "BAILIAN_ENABLE_THINKING",
                "BAILIAN_REASONING_EFFORT",
            ):
                if name not in (env or {}):
                    os.environ.pop(name, None)
            for key, value in (env or {}).items():
                os.environ[key] = value
            out = generate_metric_judgment(_facts(), client=client)
        return out, client

    def test_length_exhaustion_is_not_a_conclusion(self) -> None:
        """Isolated fixture: thinking used the whole 1800 budget and content stayed empty.

        This is not a replay of production P18. The reasoning text must not
        become the report, and HTTP 200 must not be stored as success.
        """
        reasoning = "思考过程：NDRE 为 0.57，但这不是正文。" * 80
        buf, previous, narrative = self._capture()
        try:
            out, client = self._judge(
                {"content": "", "reasoning_content": reasoning},
                usage={"completion_tokens": 1800},
                finish_reason="length",
                env={"BAILIAN_MAX_TOKENS": "1800"},
            )
        finally:
            text = buf.getvalue()
            self._restore(previous, narrative)
        self.assertEqual(out["error"], "truncated_response")
        self.assertIsNotNone(out["error"])
        self.assertEqual(out["conclusion"], [])
        self.assertEqual(out["changes"], [])
        self.assertEqual(out["metric_notes"], [])
        self.assertEqual(out["weather"], [])
        self.assertEqual(section_lines(out, "conclusion"), [])
        self.assertEqual(judgment_paragraphs(out), [])
        self.assertNotIn(reasoning, json.dumps(out, ensure_ascii=False))
        self.assertNotIn("思考过程", json.dumps(out, ensure_ascii=False))
        self.assertEqual(out["diagnostics"]["finish_reason"], "length")
        self.assertEqual(out["diagnostics"]["completion_tokens"], 1800)
        self.assertGreater(out["diagnostics"]["reasoning_chars"], 0)
        self.assertEqual(out["diagnostics"]["content_chars"], 0)
        self.assertEqual(out["diagnostics"]["http_status"], 200)
        self.assertEqual(len(client.calls), 2)
        self.assertIs(client.calls[0]["enable_thinking"], False)
        self.assertNotIn("thinking_budget", client.calls[0])
        self.assertGreater(client.calls[1]["max_tokens"], client.calls[0]["max_tokens"])
        self.assertIn("输出长度用尽", out["reader_note"])
        self.assertNotEqual(client.body["max_tokens"], 1800)
        self.assertEqual(client.body["model"], "deepseek-v4.1-flash-1017")
        self.assertNotIn("test-key", text)
        self.assertNotIn(reasoning, text)
        self.assertIn("truncated_response", text)
        self.assertIn("reasoning_chars", text)
        self.assertNotIn("exc_info", text)

    def test_enough_budget_keeps_markup_and_drops_invented_numbers(self) -> None:
        written = (
            "<b>NDRE</b> 为 "
            '<font face="YaHei-Bold" color="#1F4D38" backColor="#D9F2E3">0.57</font>。'
        )
        content = {
            "summary": "NDRE 为 0.57。",
            "changes": [
                {"metric": "ndre", "judgment": "better", "sentence": written},
                {
                    "metric": "evi",
                    "judgment": "worse",
                    "sentence": "EVI 降到 9.99。",
                },
            ],
            "conclusion": [written, "另有一块地降到 9.99。"],
            "field_notes": ["西南部值得看一眼。"],
            "metric_notes": ["NDRE 本期是 0.57。"],
            "weather": ["这期雨水不多。"],
        }
        out, client = self._judge(
            {
                "content": json.dumps(content, ensure_ascii=False),
                "reasoning_content": "不要用这段思考。",
            },
            usage={"completion_tokens": 900},
            finish_reason="stop",
        )
        self.assertIsNone(out["error"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.body["max_tokens"], 16384)
        self.assertIs(client.body["enable_thinking"], False)
        self.assertNotIn("thinking_budget", client.body)
        self.assertEqual(client.body["model"], "deepseek-v4.1-flash-1017")
        shown = section_lines(out, "conclusion")[0]
        self.assertIn("<b>NDRE</b>", shown)
        self.assertIn('color="#1F4D38"', shown)
        self.assertIn('backColor="#D9F2E3"', shown)
        self.assertIn("0.57", shown)
        self.assertEqual(out["metric_notes"], ["NDRE 本期是 0.57。"])
        self.assertEqual(out["weather"], ["这期有效降雨不多。"])
        self.assertEqual(out["field_notes"], ["西南部值得看一眼。"])
        self.assertNotIn("9.99", json.dumps(out["changes"], ensure_ascii=False))
        self.assertNotIn("9.99", json.dumps(out["conclusion"], ensure_ascii=False))
        self.assertNotIn("不要用这段思考", json.dumps(out, ensure_ascii=False))

    def test_truncated_partial_empty_invalid_and_timeout_are_distinct(self) -> None:
        partial, _client = self._judge(
            {"content": '{"conclusion": ["没写完'},
            finish_reason="length",
            usage={"completion_tokens": 8192},
        )
        self.assertEqual(partial["error"], "truncated_response")
        self.assertEqual(partial["conclusion"], [])

        partial_fields, _client = self._judge(
            {
                "content": json.dumps(
                    {"conclusion": ["NDRE 为 0.57。"]}, ensure_ascii=False
                )
            },
            finish_reason="length",
            usage={"completion_tokens": 8192},
        )
        self.assertEqual(partial_fields["error"], "truncated_response")
        self.assertEqual(partial_fields["conclusion"], ["NDRE 为 0.57。"])
        self.assertIn("输出长度用尽", partial_fields["reader_note"])
        self.assertIn("changes", partial_fields["diagnostics"]["missing_sections"])
        self.assertNotIn("field_notes", partial_fields["diagnostics"]["missing_sections"])

        usable, usable_client = self._judge(
            {
                "content": json.dumps(
                    {
                        "summary": "NDRE 为 0.57。",
                        "changes": [
                            {
                                "metric": "ndre",
                                "judgment": "better",
                                "sentence": "NDRE 为 0.57。",
                            }
                        ],
                        "conclusion": ["NDRE 为 0.57。"],
                        "field_notes": ["西南部值得看一眼。"],
                        "metric_notes": ["NDRE 本期是 0.57。"],
                    },
                    ensure_ascii=False,
                )
            },
            finish_reason="length",
            usage={"completion_tokens": 8192},
        )
        self.assertIsNone(usable["error"])
        self.assertEqual(usable["conclusion"], ["NDRE 为 0.57。"])
        self.assertTrue(usable["diagnostics"]["truncated"])
        self.assertNotIn("reader_note", usable)
        self.assertEqual(len(usable_client.calls), 1)

        empty, _client = self._judge({"content": "   "}, finish_reason="stop")
        self.assertEqual(empty["error"], "empty_content")
        self.assertEqual(section_lines(empty, "conclusion"), [])

        reasoned, _client = self._judge(
            {"content": None, "reasoning_content": "只有思考，没有正文。"},
            finish_reason="stop",
        )
        self.assertEqual(reasoned["error"], "empty_with_reasoning")
        self.assertNotIn("只有思考", json.dumps(reasoned, ensure_ascii=False))

        invalid, _client = self._judge({"content": "这不是 JSON"}, finish_reason="stop")
        self.assertEqual(invalid["error"], "invalid_json")

        blank_fields, _client = self._judge(
            {"content": json.dumps({"conclusion": [], "summary": ""})},
            finish_reason="stop",
        )
        self.assertEqual(blank_fields["error"], "empty_fields")

        class _TimeoutClient:
            def post(self, url, headers, json):  # noqa: A002
                raise httpx.ReadTimeout("timed out")

            def close(self) -> None:
                return None

        with patch.dict(
            os.environ,
            {
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
                "BAILIAN_TIMEOUT_S": "15",
            },
        ):
            timed = generate_metric_judgment(_facts(), client=_TimeoutClient())
        self.assertEqual(timed["error"], "timeout")
        self.assertGreaterEqual(timed["diagnostics"]["timeout_s"], 30)
        self.assertEqual(timed["diagnostics"]["attempts"], 2)
        self.assertEqual(timed["conclusion"], [])
        self.assertIn("请求超时", timed["reader_note"])
        self.assertNotIn("test-key", json.dumps(timed))

    def test_dropped_connection_retries_once_and_names_the_interruption(self) -> None:
        """RemoteProtocolError is one retry, then a named note. Not a length stop."""
        written = {
            "summary": "NDRE 为 0.57。",
            "changes": [
                {
                    "metric": "ndre",
                    "judgment": "better",
                    "sentence": "NDRE 为 0.57。",
                }
            ],
            "conclusion": ["NDRE 为 0.57。"],
            "field_notes": ["西南部值得看一眼。"],
            "metric_notes": ["NDRE 本期是 0.57。"],
        }

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(written, ensure_ascii=False)
                            },
                        }
                    ]
                }

        class _Client:
            def __init__(self, fail_times: int) -> None:
                self.fail_times = fail_times
                self.calls = 0

            def post(self, url, headers, json):  # noqa: A002
                self.calls += 1
                self.headers = headers
                if self.calls <= self.fail_times:
                    try:
                        raise ConnectionError("peer closed, token=test-key")
                    except ConnectionError as cause:
                        raise httpx.RemoteProtocolError(
                            "Server disconnected without sending a response."
                        ) from cause
                return _Response()

            def close(self) -> None:
                return None

        env = {
            "BAILIAN_API_KEY": "test-key",
            "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
        }
        buf, previous, narrative = self._capture()
        try:
            with patch.dict(os.environ, env):
                dropped = _Client(fail_times=2)
                failed = generate_metric_judgment(_facts(), client=dropped)
                recovered_client = _Client(fail_times=1)
                recovered = generate_metric_judgment(_facts(), client=recovered_client)
        finally:
            text = buf.getvalue()
            self._restore(previous, narrative)

        self.assertEqual(dropped.calls, 2)
        self.assertEqual(failed["error"], "connection_error")
        self.assertEqual(failed["diagnostics"]["attempts"], 2)
        self.assertEqual(failed["diagnostics"]["exception_type"], "RemoteProtocolError")
        self.assertEqual(failed["diagnostics"]["cause_type"], "ConnectionError")
        reason = failed["diagnostics"]["disconnect_reason"]
        self.assertIn("Server disconnected", reason)
        self.assertIn("peer closed", reason)
        self.assertIn("token=[redacted]", reason)
        self.assertNotIn("test-key", reason)
        self.assertEqual(failed["conclusion"], [])
        self.assertIn("模型连接中断", failed["reader_note"])
        self.assertNotIn("输出长度用尽", failed["reader_note"])
        self.assertNotIn("请求超时", failed["reader_note"])
        self.assertIn("connection_error", text)
        self.assertIn("RemoteProtocolError", text)
        self.assertIn("disconnect_reason", text)
        self.assertIn("cause_type", text)
        self.assertNotIn("test-key", text)
        self.assertNotIn("test-key", json.dumps(failed))
        self.assertNotIn("exc_info", text)

        self.assertEqual(recovered_client.calls, 2)
        self.assertIsNone(recovered["error"])
        self.assertTrue(recovered["diagnostics"]["recovered"])
        self.assertEqual(recovered["conclusion"], ["NDRE 为 0.57。"])
        self.assertNotIn("reader_note", recovered)

        class _Once:
            def __init__(self) -> None:
                self.calls = 0

            def post(self, url, headers, json):  # noqa: A002
                self.calls += 1
                raise RuntimeError("boom test-key")

            def close(self) -> None:
                return None

        with patch.dict(os.environ, env):
            unexpected = _Once()
            broken = generate_metric_judgment(_facts(), client=unexpected)
        self.assertEqual(unexpected.calls, 1)
        self.assertEqual(broken["error"], "RuntimeError")
        self.assertEqual(
            broken["reader_note"],
            "本期模型文字未完成。数值和图表仍是程序计算结果。",
        )
        self.assertNotIn("boom", json.dumps(broken))
        self.assertNotIn("test-key", json.dumps(broken))

    def test_content_list_ignores_reasoning_part(self) -> None:
        out, _client = self._judge(
            {
                "content": [
                    {"type": "reasoning", "text": "思考里写 NDRE 为 0.57。"},
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "summary": "NDRE 为 0.57。",
                                "changes": [
                                    {
                                        "metric": "ndre",
                                        "judgment": "better",
                                        "sentence": "NDRE 为 0.57。",
                                    }
                                ],
                                "conclusion": ["NDRE 为 0.57。"],
                                "field_notes": ["西南部值得看一眼。"],
                                "metric_notes": ["NDRE 本期是 0.57。"],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ]
            }
        )
        self.assertIsNone(out["error"])
        self.assertEqual(out["conclusion"], ["NDRE 为 0.57。"])
        self.assertNotIn("思考里写", json.dumps(out, ensure_ascii=False))

    def test_narrative_exception_still_writes_the_pdf(self) -> None:
        names = [
            "app.reports.biweekly.official_pdf",
            "app.reports.biweekly.enrichment",
            "app.reports.biweekly.generate",
        ]
        saved = {name: sys.modules.get(name) for name in names}
        captured: dict = {}
        try:
            pdf_mod = ModuleType("app.reports.biweekly.official_pdf")

            def render(facts, dest, figures_dir=None):
                captured["narrative"] = facts.get("ai_narrative")
                Path(dest).write_bytes(b"%PDF-1.4\n%%EOF\n")
                return Path(dest)

            pdf_mod.render_official_biweekly_pdf = render
            enrich = ModuleType("app.reports.biweekly.enrichment")
            enrich.enrich_official_report_facts = lambda facts, **kwargs: dict(facts)
            sys.modules["app.reports.biweekly.official_pdf"] = pdf_mod
            sys.modules["app.reports.biweekly.enrichment"] = enrich
            sys.modules.pop("app.reports.biweekly.generate", None)
            gen = importlib.import_module("app.reports.biweekly.generate")
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "out.pdf"

                def boom(_facts, **_kwargs):
                    raise RuntimeError("narrative blew up")

                facts = {"land_id": "32696"}
                with patch(
                    "app.reports.biweekly.narrative_ai.generate_metric_judgment",
                    side_effect=boom,
                ):
                    written = gen.generate_official_biweekly_pdf(facts, dest)
                self.assertTrue(written.is_file())
                self.assertEqual(captured["narrative"]["error"], "RuntimeError")
                self.assertIn("模型文字未完成", facts["ai_narrative"]["reader_note"])
                self.assertEqual(captured["narrative"]["conclusion"], [])
                self.assertNotIn("narrative blew up", json.dumps(captured["narrative"]))

                def truncated(_facts, **_kwargs):
                    return {
                        "error": "truncated_response",
                        "conclusion": [],
                        "summary": "",
                        "changes": [],
                        "diagnostics": {"reasoning_chars": 4249},
                    }

                with patch(
                    "app.reports.biweekly.narrative_ai.generate_metric_judgment",
                    side_effect=truncated,
                ):
                    gen.generate_official_biweekly_pdf({"land_id": "32696"}, dest)
                self.assertEqual(captured["narrative"]["error"], "truncated_response")
                self.assertEqual(captured["narrative"]["conclusion"], [])
                self.assertNotIn("reasoning_content", captured["narrative"])
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def _complete_message(self) -> dict:
        return {
            "content": json.dumps(
                {
                    "summary": "NDRE 为 0.57。",
                    "changes": [
                        {
                            "metric": "ndre",
                            "judgment": "better",
                            "sentence": "NDRE 为 0.57。",
                        }
                    ],
                    "conclusion": ["NDRE 为 0.57。"],
                    "field_notes": ["西南部值得看一眼。"],
                    "metric_notes": ["NDRE 本期是 0.57。"],
                },
                ensure_ascii=False,
            )
        }

    def test_empty_8192_content_recovers_once_and_drops_reasoning(self) -> None:
        """Fixture shaped like land 32694 P15: length, 8192 tokens, empty content.

        The reasoning string is synthetic padding, not the production trace.
        """
        reasoning = "R" * 14791
        calls = {"n": 0}

        class _Response:
            def __init__(self, payload: dict):
                self.status_code = 200
                self._payload = payload

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return self._payload

        class _Client:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def post(self, url, headers, json):  # noqa: A002
                self.calls.append(json)
                calls["n"] += 1
                if calls["n"] == 1:
                    return _Response(
                        {
                            "choices": [
                                {
                                    "finish_reason": "length",
                                    "message": {
                                        "content": "",
                                        "reasoning_content": reasoning,
                                    },
                                }
                            ],
                            "usage": {"completion_tokens": 8192},
                        }
                    )
                return _Response(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": self_message(),
                                    "reasoning_content": reasoning,
                                },
                            }
                        ],
                        "usage": {"completion_tokens": 400},
                    }
                )

            def close(self) -> None:
                return None

        def self_message() -> str:
            return json.dumps(
                {
                    "summary": "NDRE 为 0.57。",
                    "changes": [
                        {
                            "metric": "ndre",
                            "judgment": "better",
                            "sentence": "NDRE 为 0.57。",
                        }
                    ],
                    "conclusion": ["NDRE 为 0.57。"],
                    "field_notes": ["西南部值得看一眼。"],
                    "metric_notes": ["NDRE 本期是 0.57。"],
                },
                ensure_ascii=False,
            )

        client = _Client()
        with patch.dict(
            os.environ,
            {
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
            },
        ):
            for name in (
                "BAILIAN_MAX_TOKENS",
                "BAILIAN_TIMEOUT_S",
                "BAILIAN_ENABLE_THINKING",
                "BAILIAN_REASONING_EFFORT",
            ):
                os.environ.pop(name, None)
            out = generate_metric_judgment(_facts(), client=client)
        self.assertEqual(len(client.calls), 2)
        self.assertIs(client.calls[0]["enable_thinking"], False)
        self.assertIs(client.calls[1]["enable_thinking"], False)
        self.assertNotIn("thinking_budget", client.calls[0])
        self.assertGreater(client.calls[1]["max_tokens"], 8192)
        self.assertIsNone(out["error"])
        self.assertEqual(out["conclusion"], ["NDRE 为 0.57。"])
        self.assertTrue(out["diagnostics"]["recovered"])
        self.assertEqual(out["diagnostics"]["attempts"], 2)
        self.assertNotIn(reasoning, json.dumps(out, ensure_ascii=False))
        self.assertNotIn("reader_note", out)

    def test_second_empty_stops_and_budget_above_model_max_is_clamped(self) -> None:
        reasoning = "R" * 14791

        class _Response:
            status_code = 200

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "", "reasoning_content": reasoning},
                        }
                    ],
                    "usage": {"completion_tokens": 8192},
                }

        class _Client:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def post(self, url, headers, json):  # noqa: A002
                self.calls.append(json)
                return _Response()

            def close(self) -> None:
                return None

        client = _Client()
        with patch.dict(
            os.environ,
            {
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
                "BAILIAN_MAX_TOKENS": "999999999",
            },
        ):
            out = generate_metric_judgment(_facts(), client=client)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["max_tokens"], 393216)
        self.assertTrue(out["diagnostics"]["max_tokens_clamped"])
        self.assertEqual(out["error"], "truncated_response")
        self.assertIn("输出长度用尽", out["reader_note"])
        self.assertNotIn(reasoning, json.dumps(out, ensure_ascii=False))

    def test_http_400_retries_without_thinking_field_and_429_stops_at_two(self) -> None:
        ok = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "NDRE 为 0.57。",
                                "changes": [
                                    {
                                        "metric": "ndre",
                                        "judgment": "better",
                                        "sentence": "NDRE 为 0.57。",
                                    }
                                ],
                                "conclusion": ["NDRE 为 0.57。"],
                                "field_notes": ["西南部值得看一眼。"],
                                "metric_notes": ["NDRE 本期是 0.57。"],
                            },
                            ensure_ascii=False,
                        )
                    },
                }
            ]
        }

        class _Status:
            def __init__(self, code: int, payload: dict | None = None):
                self.status_code = code
                self._payload = payload or {}

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    request = httpx.Request(
                        "POST", "https://example.test/v1/chat/completions"
                    )
                    response = httpx.Response(self.status_code, request=request)
                    raise httpx.HTTPStatusError(
                        "status", request=request, response=response
                    )

            def json(self) -> dict:
                return self._payload

        class _Client:
            def __init__(self, codes: list[int]):
                self.codes = codes
                self.calls: list[dict] = []

            def post(self, url, headers, json):  # noqa: A002
                self.calls.append(json)
                code = self.codes[len(self.calls) - 1]
                return _Status(code, ok if code == 200 else None)

            def close(self) -> None:
                return None

        with patch.dict(
            os.environ,
            {
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
                "BAILIAN_ENABLE_THINKING": "1",
            },
        ):
            rejected = _Client([400, 200])
            out = generate_metric_judgment(_facts(), client=rejected)
        self.assertEqual(len(rejected.calls), 2)
        self.assertIs(rejected.calls[0]["enable_thinking"], True)
        self.assertEqual(rejected.calls[0]["reasoning_effort"], "low")
        self.assertNotIn("enable_thinking", rejected.calls[1])
        self.assertNotIn("thinking_budget", rejected.calls[0])
        self.assertIsNone(out["error"])

        with patch.dict(
            os.environ,
            {
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
            },
        ):
            limited = _Client([429, 429, 200])
            failed = generate_metric_judgment(_facts(), client=limited)
        self.assertEqual(len(limited.calls), 2)
        self.assertEqual(failed["error"], "http_error")
        self.assertEqual(failed["diagnostics"]["http_status"], 429)
        self.assertIn("暂时没有返回", failed["reader_note"])
        self.assertNotIn("test-key", json.dumps(failed))

        with patch.dict(
            os.environ,
            {
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
            },
        ):
            unavailable = _Client([503, 200])
            recovered = generate_metric_judgment(_facts(), client=unavailable)
        self.assertEqual(len(unavailable.calls), 2)
        self.assertIsNone(recovered["error"])

    def test_non_deepseek_does_not_send_thinking_switch(self) -> None:
        out, client = self._judge(
            {
                "content": json.dumps(
                    {
                        "summary": "NDRE 为 0.57。",
                        "changes": [
                            {
                                "metric": "ndre",
                                "judgment": "better",
                                "sentence": "NDRE 为 0.57。",
                            }
                        ],
                        "conclusion": ["NDRE 为 0.57。"],
                        "field_notes": ["西南部值得看一眼。"],
                        "metric_notes": ["NDRE 本期是 0.57。"],
                    },
                    ensure_ascii=False,
                )
            },
            env={"BAILIAN_MODEL": "qwen3.7-flash"},
        )
        self.assertIsNone(out["error"])
        self.assertNotIn("enable_thinking", client.body)
        self.assertNotIn("thinking_budget", client.body)
        self.assertLessEqual(client.body["max_tokens"], 32768)

    def test_reader_note_is_one_line_without_a_new_page(self) -> None:
        from reportlab.platypus import PageBreak, Paragraph

        from app.reports.biweekly.official_pdf import _register_fonts, build_story

        _register_fonts()

        facts = {
            "land_id": "32694",
            "period": {"start": "2026-03-21", "end_inclusive": "2026-04-03"},
            "ai_narrative": {
                "error": "truncated_response",
                "conclusion": [],
                "reader_note": "本期模型文字未完成：输出长度用尽。数值和图表仍是程序计算结果。",
            },
        }
        story = build_story(facts, Path("/tmp/biweekly-note-fixture"))
        texts = []
        for item in story:
            if isinstance(item, Paragraph):
                texts.append(item.getPlainText())
        joined = "\n".join(texts)
        self.assertIn("输出长度用尽", joined)
        self.assertNotIn("reasoning_content", joined)
        note_at = next(
            i
            for i, item in enumerate(story)
            if isinstance(item, Paragraph) and "输出长度用尽" in item.getPlainText()
        )
        self.assertFalse(isinstance(story[note_at - 1], PageBreak))

    def test_missing_weather_talk_is_not_left_in_the_conclusion(self) -> None:
        self.assertIn("不要写「没有可用」", SYSTEM_PROMPT)
        facts = _facts()
        facts["water_risk"] = {
            "lines": [
                "叶子里的水：11月11日为0.00，10月28日为-0.10，变化+0.10，多了一些。",
                "有效降雨：没有可用的降雨或蒸散记录，不能据此判断这半月是亏是余。",
            ]
        }
        payload = narrative_input(facts)
        self.assertIsNone(payload["weather"])
        self.assertEqual(len(payload["water_risk_lines"]), 1)
        self.assertNotIn("没有可用", "".join(payload["water_risk_lines"]))
        cleaned = normalize_judgment(
            {
                "conclusion": [
                    "本期没有可用的气温和降雨记录，冷热和有效降雨都无从对照，"
                    "干湿只能看NDMI：0.00比上期的-0.10多了一些，水分略有改善但仍在零附近。",
                    "本期没有可用的气温和降雨记录。",
                ]
            },
            payload,
        )
        self.assertEqual(len(cleaned["conclusion"]), 1)
        text = cleaned["conclusion"][0]
        self.assertNotIn("没有可用", text)
        self.assertNotIn("无从对照", text)
        self.assertNotIn("没有记录", text)
        self.assertIn("0.00", text)
        self.assertIn("-0.10", text)
