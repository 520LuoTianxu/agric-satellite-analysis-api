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
        payload = narrative_input(facts)
        self.assertIn("最近一次有效观测", payload["parameter_guide"]["metrics"])
        self.assertEqual(payload["metrics"]["ndre"]["latest"], "0.57")
        self.assertIn("叶绿素", payload["metrics"]["ndre"]["meaning"])
        self.assertEqual(payload["infield"][0]["p10"], "0.51")
        self.assertEqual(payload["infield"][0]["regions"][0]["share_percent"], 40)

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
        self.assertEqual(client.body["max_tokens"], 8192)
        self.assertEqual(client.body["model"], "deepseek-v4.1-flash-1017")
        self.assertEqual(out["model"], "deepseek-v4.1-flash-1017")
        self.assertEqual(out["changes"][0]["judgment"], "better")
        self.assertIn("0.57", out["changes"][0]["sentence"])
        prompt = client.body["messages"][0]["content"]
        self.assertIn("由你决定", prompt)
        self.assertNotIn("往好变化必须", prompt)
        self.assertIn("#1F4D38", prompt)
        self.assertIn("<b>", prompt)
        self.assertIn("发挥", prompt)
        self.assertIn("metric_notes", prompt)
        self.assertIn("backColor", prompt)
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
                self.body = json
                self.headers = headers
                return _Response()

            def close(self) -> None:
                return None

        client = _Client()
        values = {
            "BAILIAN_API_KEY": "test-key",
            "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
        }
        values.update(env or {})
        with patch.dict(os.environ, values, clear=False):
            os.environ.pop("BAILIAN_MAX_TOKENS", None)
            os.environ.pop("BAILIAN_TIMEOUT_S", None)
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
        self.assertGreaterEqual(client.body["max_tokens"], 4096)
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
        self.assertEqual(client.body["max_tokens"], 8192)
        self.assertEqual(client.body["model"], "deepseek-v4.1-flash-1017")
        shown = section_lines(out, "conclusion")[0]
        self.assertIn("<b>NDRE</b>", shown)
        self.assertIn('color="#1F4D38"', shown)
        self.assertIn('backColor="#D9F2E3"', shown)
        self.assertIn("0.57", shown)
        self.assertEqual(out["metric_notes"], ["NDRE 本期是 0.57。"])
        self.assertEqual(out["weather"], ["这期雨水不多。"])
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

        usable, _client = self._judge(
            {
                "content": json.dumps(
                    {"conclusion": ["NDRE 为 0.57。"]}, ensure_ascii=False
                )
            },
            finish_reason="length",
            usage={"completion_tokens": 8192},
        )
        self.assertIsNone(usable["error"])
        self.assertEqual(usable["conclusion"], ["NDRE 为 0.57。"])
        self.assertTrue(usable["diagnostics"]["truncated"])

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
        self.assertEqual(timed["conclusion"], [])
        self.assertNotIn("test-key", json.dumps(timed))

    def test_content_list_ignores_reasoning_part(self) -> None:
        out, _client = self._judge(
            {
                "content": [
                    {"type": "reasoning", "text": "思考里写 NDRE 为 0.57。"},
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"conclusion": ["NDRE 为 0.57。"]}, ensure_ascii=False
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

                with patch(
                    "app.reports.biweekly.narrative_ai.generate_metric_judgment",
                    side_effect=boom,
                ):
                    written = gen.generate_official_biweekly_pdf(
                        {"land_id": "32696"}, dest
                    )
                self.assertTrue(written.is_file())
                self.assertEqual(captured["narrative"]["error"], "RuntimeError")
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
