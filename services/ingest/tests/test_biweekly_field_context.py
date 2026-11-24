"""Key-node parsing, weather context lines and photo review. No live database."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from agric_satellite_analysis_common.biweekly.field_records import (
    parse_workbox_rows,
    select_key_nodes,
    workbox_season_keys,
)


def _row(**kwargs):
    base = {
        "task_code": "EMERGENCE_CHECK",
        "task_name_snapshot": "出苗检查",
        "task_status": "PENDING_BRANCH_REVIEW",
        "submitted_at": "2026-11-02",
        "available_date": "2026-11-01",
        "stage_code": "",
        "executor_json": json.dumps(
            {
                "submitData": {
                    "landName": "不应出现的地块名",
                    "landId": 30872,
                    "gapLabel": "无断垄",
                    "countInput": "38",
                    "formPhotos": [
                        {
                            "url": "https://bucket.oss-cn-beijing.aliyuncs.com/a.jpg?Signature=secret"
                        }
                    ],
                }
            },
            ensure_ascii=False,
        ),
        "branch_manager_json": "{}",
        "regional_manager_json": "null",
    }
    base.update(kwargs)
    return base


class FieldRecordTests(unittest.TestCase):
    def test_season_keys_try_both_wheat_years(self):
        self.assertEqual(
            workbox_season_keys("2025/26", "小麦"),
            ["2025-WINTER-WHEAT", "2026-WINTER-WHEAT"],
        )
        self.assertEqual(
            workbox_season_keys("2026-WINTER-WHEAT"), ["2026-WINTER-WHEAT"]
        )

    def test_parser_drops_names_and_signatures(self):
        parsed = parse_workbox_rows([_row()])
        node = parsed["nodes"][0]
        blob = json.dumps(parsed, ensure_ascii=False)
        self.assertNotIn("不应出现", blob)
        self.assertNotIn("Signature", blob)
        self.assertNotIn("secret", blob)
        self.assertEqual(node["answers"][0]["label"], "断垄")
        self.assertEqual(node["answers"][1]["value"], "38")
        coded = parse_workbox_rows(
            [
                _row(
                    executor_json=json.dumps(
                        {
                            "submitData": {
                                "offlineInspection": "是",
                                "devFert": 1040,
                                "reportReasonOther": "播种时已镇压",
                                "gapLabel": "无断垄",
                            }
                        },
                        ensure_ascii=False,
                    )
                )
            ]
        )
        labels = [item["label"] for item in coded["nodes"][0]["answers"]]
        values = [item["value"] for item in coded["nodes"][0]["answers"]]
        self.assertNotIn("offlineInspection", "".join(labels))
        self.assertNotIn("devFert", "".join(labels + values))
        self.assertIn("播种时已镇压", values)
        self.assertIn("无断垄", values)
        self.assertEqual(node["photo_count"], 1)
        self.assertTrue(parsed["photos"][0]["url"].endswith("/a.jpg"))

    def test_selects_stage_node_outside_the_period(self):
        parsed = parse_workbox_rows(
            [
                _row(),
                _row(
                    task_code="BASE_FERTILIZER",
                    task_name_snapshot="底肥撒施",
                    submitted_at="2026-09-01",
                    executor_json=json.dumps({"submitData": {"totalArea": "12"}}),
                ),
            ]
        )
        selected = select_key_nodes(
            parsed["nodes"],
            period_start="2026-11-20",
            period_end="2026-12-03",
            stage_text="出苗期",
        )
        self.assertEqual([item["task_code"] for item in selected], ["EMERGENCE_CHECK"])
        self.assertEqual(selected[0]["relevance"], "stage")


class ConclusionContextTests(unittest.TestCase):
    def test_prints_temperature_prior_year_and_node(self):
        from app.reports.biweekly.conclusion_context import build_conclusion_context

        facts = {
            "period": {"start": "2026-11-20", "end_inclusive": "2026-12-03"},
            "phase": {"observation_stages": [{"stage_code": "出苗期"}]},
            "weather_period": {
                "min_c": 1,
                "max_c": 9,
                "mean_c": 5,
                "temperature_days": 14,
            },
            "weather_same_period_prior_year": {
                "start": "2025-11-20",
                "end": "2025-12-03",
                "mean_c": 3,
            },
            "key_node_records": {
                "status": "ready",
                "nodes": parse_workbox_rows([_row()])["nodes"],
                "photos": [],
            },
            "soil_context": {"status": "ready", "ph": 7.2, "texture": "壤土"},
            "photo_review": {"status": "not_reviewed", "count": 1},
        }
        lines = build_conclusion_context(facts)["lines"]
        joined = "\n".join(lines)
        self.assertNotIn("最低", joined)
        self.assertNotIn("去年同期", joined)
        self.assertNotIn("无断垄", joined)
        self.assertNotIn("不描述照片", joined)
        self.assertNotIn("没有对应", joined)
        self.assertIn("出苗检查", joined)
        self.assertIn("土壤底子", joined)
        self.assertIn("壤土", joined)
        self.assertIn("中性", joined)
        self.assertIn("7.2", joined)
        self.assertNotIn("pH", joined)
        self.assertLessEqual(len(lines), 2)
        self.assertNotIn("不应出现", joined)
        english = dict(facts)
        english["soil_context"] = {"status": "ready", "texture": "silt loam", "ph": 8.1}
        soil_line = "\n".join(build_conclusion_context(english)["lines"])
        self.assertIn("粉壤土", soil_line)
        self.assertNotIn("loam", soil_line)
        self.assertIn("偏碱", soil_line)


class PhotoReviewTests(unittest.TestCase):
    def test_unset_vision_model_does_not_call_http(self):
        from app.reports.biweekly.photo_review import attach_photo_review

        facts = {
            "key_node_records": {
                "photos": [
                    {"url": "https://bucket.example/a.jpg", "task_name": "出苗检查"}
                ]
            }
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAILIAN_VISION_MODEL", None)
            attach_photo_review(facts, client=object())
        self.assertEqual(facts["photo_review"]["status"], "not_reviewed")
        self.assertNotIn("https://", json.dumps(facts["photo_review"]))

    def test_vision_call_keeps_the_note_and_not_the_url(self):
        from app.reports.biweekly.photo_review import attach_photo_review

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"usable": False, "note": "画面发糊。"},
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }

        class _Client:
            def post(self, url, headers, json):  # noqa: A002
                self.body = json
                return _Response()

            def close(self):
                return None

        facts = {
            "key_node_records": {
                "photos": [
                    {"url": "https://bucket.example/a.jpg", "task_name": "出苗检查"}
                ]
            }
        }
        client = _Client()
        with patch.dict(
            os.environ,
            {
                "BAILIAN_VISION_MODEL": "qwen-vl-example",
                "BAILIAN_API_KEY": "test-key",
                "BAILIAN_MODEL": "deepseek-v4.1-flash-1017",
            },
        ):
            attach_photo_review(facts, client=client)
        self.assertEqual(client.body["model"], "qwen-vl-example")
        self.assertNotEqual(client.body["model"], "deepseek-v4.1-flash-1017")
        self.assertIn("画面看不清", facts["photo_review"]["note"])
        self.assertNotIn("a.jpg", json.dumps(facts["photo_review"]))
