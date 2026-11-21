"""Smart wheat mapping loader: table rows or in-code Smart-name fallback."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.config import settings
from app.services.biweekly_mapping import load_wheat_phase_mapping
from agric_satellite_analysis_common.biweekly.phase_policy import MAPPING_VERSION, resolve_wheat_stage


class _Conn:
    def __init__(self, rows=None, error=None):
        self.rows = rows or []
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, params=None):
        if self.error:
            raise self.error
        if "LIKE" in str(sql):
            raise AssertionError("mapping query must not use LIKE")
        self.params = params
        result = MagicMock()
        result.mappings.return_value.all.return_value = self.rows
        return result


class _Engine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


class WheatMappingTests(unittest.TestCase):
    def setUp(self):
        self._enabled = settings.mysql_source_enabled
        self._url = settings.mysql_source_url

    def tearDown(self):
        settings.mysql_source_enabled = self._enabled
        settings.mysql_source_url = self._url

    def test_fallback_when_mysql_disabled(self):
        settings.mysql_source_enabled = False
        settings.mysql_source_url = ""
        payload = asyncio.run(load_wheat_phase_mapping())
        self.assertEqual(payload["source"], "in_code_pilot")
        self.assertFalse(payload["table_available"])
        self.assertEqual(payload["mapping_version"], "wheat-smart-pilot-1")
        self.assertEqual(payload["crop_id"], 4)
        self.assertEqual(payload["crop_name"], "小麦")
        names = [row["stage_name"] for row in payload["rows"]]
        self.assertEqual(
            names,
            [
                "播种前准备",
                "播种期",
                "出苗期",
                "分蘖期",
                "越冬期",
                "返青期",
                "拔节孕穗期",
                "抽穗期",
                "灌浆期",
                "成熟期",
            ],
        )
        self.assertNotIn("起身期", names)
        self.assertNotIn("孕穗", names)
        winter = next(row for row in payload["rows"] if row["stage_name"] == "越冬期")
        self.assertEqual(winter["phase_label"], "S1")
        self.assertEqual(winter["context_tags"], "DORMANCY")
        joint = next(row for row in payload["rows"] if row["stage_name"] == "拔节孕穗期")
        self.assertEqual(joint["phase_label"], "S2")

    def test_missing_table_falls_back_without_raising(self):
        settings.mysql_source_enabled = True
        settings.mysql_source_url = "mysql+asyncmy://user:pass@127.0.0.1:3306/smart_agric"
        conn = _Conn(error=Exception("Table 'smart_agric.crop_remote_phase_mapping' doesn't exist"))
        with patch("app.services.mysql_land_sync._mysql_engine", return_value=_Engine(conn)):
            payload = asyncio.run(load_wheat_phase_mapping())
        self.assertEqual(payload["source"], "in_code_pilot")
        self.assertFalse(payload["table_available"])
        self.assertIn("不存在", payload["note"])

    def test_reads_smart_table_rows(self):
        settings.mysql_source_enabled = True
        settings.mysql_source_url = "mysql+asyncmy://user:pass@127.0.0.1:3306/smart_agric"
        rows = [
            {
                "crop_id": 4,
                "crop_name": "小麦",
                "stage_name": "拔节孕穗期",
                "mapping_status": "MAPPED",
                "phase_code": "TRANS",
                "phase_label": "S2",
                "context_tags": "",
                "review_status": "PENDING_AGRONOMY_REVIEW",
                "mapping_version": MAPPING_VERSION,
            }
        ]
        conn = _Conn(rows=rows)
        with patch("app.services.mysql_land_sync._mysql_engine", return_value=_Engine(conn)):
            payload = asyncio.run(load_wheat_phase_mapping())
        self.assertEqual(payload["source"], "smart_agric.crop_remote_phase_mapping")
        self.assertTrue(payload["table_available"])
        self.assertEqual(payload["rows"][0]["stage_name"], "拔节孕穗期")
        self.assertEqual(conn.params["crop_id"], 4)
        self.assertEqual(conn.params["mapping_version"], "wheat-smart-pilot-1")

    def test_table_rows_override_aliases(self):
        rows = [
            {
                "stage_name": "拔节孕穗期",
                "mapping_status": "MAPPED",
                "phase_code": "TRANS",
                "phase_label": "S2",
                "context_tags": "",
            }
        ]
        resolved = resolve_wheat_stage("起身期", rows)
        self.assertEqual(resolved["stage_code"], "拔节孕穗期")
        self.assertEqual(resolved["phase_label"], "S2")


if __name__ == "__main__":
    unittest.main()
