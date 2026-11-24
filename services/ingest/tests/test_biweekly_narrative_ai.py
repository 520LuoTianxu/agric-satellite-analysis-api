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
        parsed["conclusion"] = ["这是干旱。", "不判定干旱。"]
        diagnosed = normalize_judgment(parsed, payload)
        self.assertEqual(diagnosed["conclusion"], ["不判定干旱。"])

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
        self.assertIn("field_notes", partial_fields["diagnostics"]["missing_sections"])

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
