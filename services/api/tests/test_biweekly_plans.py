"""Smart planting calendar lookup: exact land_id, plant_task windows, no substitution."""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import MagicMock

from agric_satellite_analysis_common.biweekly.models import ReportError
from app.core.config import settings
from app.services.biweekly_plans import (
    MISSING_NOTE,
    NO_ACTIVE_BATCH_NOTE,
    PLANTING_TASK_STATUSES,
    SOURCE_UNAVAILABLE_NOTE,
    SmartPlantingCatalog,
    attach_public_stages,
    build_seasons_from_rows,
    exact_land_id,
    load_smart_planting_calendar,
    public_stage_intervals,
)


def _batch(land_id=18407, batch_id=9510, name="小麦试点批次"):
    return {
        "batch_id": batch_id,
        "land_id": land_id,
        "crop_id": 4,
        "crop_name": "小麦",
        "batch_name": name,
        "start_time": "2027-10-25",
        "scheme_handling_profile": "WINTER_WHEAT_KEY_NODE",
        "project_crop_season_id": 6421,
    }


def _task(
    plan_id=9510, name="返青期", start="2028-02-21", finish="2028-02-28", task_id=1
):
    return {
        "plant_task_id": task_id,
        "plan_id": plan_id,
        "task_name": name,
        "plan_start": start,
        "plan_finish": finish,
        "del_flag": "0",
    }


class SmartPlanTests(unittest.TestCase):
    def test_exact_land_id_rejects_wildcards(self):
        self.assertEqual(exact_land_id("6417"), 6417)
        with self.assertRaises(ReportError) as ctx:
            exact_land_id("%6417%")
        self.assertEqual(ctx.exception.code, "LAND_ID_INVALID")
        with self.assertRaises(ReportError):
            exact_land_id("16417%")

    def test_does_not_attach_another_lands_batch(self):
        payload = build_seasons_from_rows(
            "6417",
            [_batch(land_id=16417, batch_id=99)],
            [_task(plan_id=99, name="返青期", start="2025-03-01", finish="2025-03-10")],
        )
        self.assertFalse(payload["plan_available"])
        self.assertEqual(payload["plan_status"], "missing")
        self.assertEqual(payload["seasons"], [])
        self.assertEqual(payload["note"], MISSING_NOTE)

    def test_periods_omit_pre_s1_work_but_keep_those_stages(self):
        tasks = [
            _task(
                name="整地播种期", start="2025-09-06", finish="2025-10-17", task_id=1
            ),
            _task(
                name="出苗至三叶期", start="2025-10-18", finish="2025-11-10", task_id=2
            ),
            _task(name="成熟期", start="2026-05-20", finish="2026-06-06", task_id=3),
        ]
        payload = build_seasons_from_rows("18407", [_batch()], tasks)
        season = payload["seasons"][0]
        self.assertEqual(season["start"], "2025-09-06")
        self.assertEqual(season["end_inclusive"], "2026-06-06")
        self.assertEqual(season["observation_start"], "2025-10-18")
        self.assertEqual(season["periods"][0]["period_id"], "1")
        self.assertEqual(season["periods"][0]["start"], "2025-10-18")
        self.assertEqual(season["periods"][0]["end_inclusive"], "2025-10-31")
        self.assertTrue(all(row["start"] >= "2025-10-18" for row in season["periods"]))
        names = [row["source_stage_name"] for row in season["native_plan"]["intervals"]]
        self.assertEqual(names, ["整地播种期", "出苗至三叶期", "成熟期"])

    def test_without_s1_periods_start_at_the_first_monitored_stage(self):
        tasks = [
            _task(name="播种期", start="2026-09-20", finish="2026-10-05", task_id=1),
            _task(
                name="拔节孕穗期", start="2027-03-11", finish="2027-04-10", task_id=2
            ),
            _task(name="成熟期", start="2027-05-20", finish="2027-06-05", task_id=3),
        ]
        season = build_seasons_from_rows("18407", [_batch()], tasks)["seasons"][0]
        self.assertEqual(season["start"], "2026-09-20")
        self.assertEqual(season["observation_start"], "2027-03-11")
        self.assertEqual(season["periods"][0]["start"], "2027-03-11")
        self.assertIn(
            "播种期",
            [row["source_stage_name"] for row in season["native_plan"]["intervals"]],
        )

    def test_only_pre_s1_tasks_keep_the_season_and_emit_no_periods(self):
        tasks = [
            _task(name="整地播种期", start="2026-09-06", finish="2026-10-01", task_id=1)
        ]
        payload = build_seasons_from_rows("18407", [_batch()], tasks)
        season = payload["seasons"][0]
        self.assertEqual(season["start"], "2026-09-06")
        self.assertIsNone(season["observation_start"])
        self.assertEqual(season["periods"], [])
        self.assertEqual(
            season["native_plan"]["intervals"][0]["source_stage_name"], "整地播种期"
        )

    def test_builds_planting_season_and_official_intervals(self):
        tasks = [
            _task(
                name="整地播种期", start="2027-09-20", finish="2027-11-01", task_id=1
            ),
            _task(
                name="出苗至三叶期", start="2027-11-02", finish="2027-11-17", task_id=2
            ),
            _task(name="越冬期", start="2028-01-19", finish="2028-01-25", task_id=3),
            _task(name="起身期", start="2028-03-10", finish="2028-03-13", task_id=4),
            _task(name="拔节期", start="2028-03-28", finish="2028-04-13", task_id=5),
            _task(
                name="抽穗至开花期", start="2028-04-21", finish="2028-05-04", task_id=6
            ),
        ]
        payload = build_seasons_from_rows("18407", [_batch()], tasks)
        self.assertTrue(payload["plan_available"])
        season = payload["seasons"][0]
        self.assertEqual(season["season_id"], "2027/28")
        self.assertEqual(season["label"], "2027/28 种植季")
        self.assertEqual(season["crop_name"], "小麦")
        self.assertEqual(season["start"], "2027-09-20")
        self.assertEqual(season["end_inclusive"], "2028-05-04")
        self.assertEqual(season["observation_start"], "2027-11-02")
        self.assertTrue(season["periods"])
        self.assertEqual(season["periods"][0]["start"], "2027-11-02")
        self.assertEqual(season["periods"][0]["label"], "第1期")
        self.assertTrue(all(row["start"] >= "2027-11-02" for row in season["periods"]))
        self.assertLessEqual(season["periods"][0]["days"], 14)
        plan = season["native_plan"]
        self.assertEqual(plan["plan_label"], "本田方案")
        by_source = {row["source_stage_name"]: row for row in plan["intervals"]}
        self.assertEqual(by_source["整地播种期"]["stage_code"], "播种期")
        self.assertIsNone(by_source["整地播种期"]["phase_label"])
        self.assertEqual(by_source["起身期"]["stage_code"], "拔节孕穗期")
        self.assertEqual(by_source["起身期"]["phase_label"], "S2")
        self.assertEqual(by_source["拔节期"]["stage_code"], "拔节孕穗期")
        self.assertEqual(by_source["越冬期"]["tags"], ["DORMANCY"])
        self.assertEqual(by_source["抽穗至开花期"]["end_exclusive"], "2028-05-05")

        catalog = SmartPlantingCatalog(payload)
        self.assertTrue(catalog.plan_available)
        native = catalog.native_plan("18407", "2027/28")
        self.assertEqual(native["batch_id"], "9510")
        with self.assertRaises(ReportError) as ctx:
            catalog.native_plan("6417", "2027/28")
        self.assertEqual(ctx.exception.code, "SEASON_NOT_FOUND")

    def test_public_stages_keep_not_applicable_pre_monitoring(self):
        tasks = [
            _task(
                plan_id=8801,
                name="播种前准备",
                start="2026-09-20",
                finish="2026-10-05",
                task_id=1,
            ),
            _task(
                plan_id=8801,
                name="播种期",
                start="2026-10-06",
                finish="2026-10-15",
                task_id=2,
            ),
            _task(
                plan_id=8801,
                name="出苗期",
                start="2026-10-16",
                finish="2026-10-31",
                task_id=3,
            ),
            _task(
                plan_id=8801,
                name="拔节孕穗期",
                start="2027-03-20",
                finish="2027-04-10",
                task_id=4,
            ),
            _task(
                plan_id=8801,
                name="成熟期",
                start="2027-05-20",
                finish="2027-06-05",
                task_id=5,
            ),
        ]
        calendar = build_seasons_from_rows(
            "18509", [_batch(land_id=18509, batch_id=8801, name="小麦2026/27")], tasks
        )
        season = calendar["seasons"][0]
        by_source = {
            row["source_stage_name"]: row for row in season["native_plan"]["intervals"]
        }
        self.assertEqual(by_source["播种前准备"]["mapping_status"], "NOT_APPLICABLE")
        self.assertIsNone(by_source["播种前准备"]["phase_label"])
        self.assertEqual(by_source["播种期"]["mapping_status"], "NOT_APPLICABLE")
        self.assertEqual(by_source["出苗期"]["phase_label"], "S1")
        self.assertEqual(by_source["拔节孕穗期"]["phase_label"], "S2")
        self.assertEqual(by_source["成熟期"]["phase_label"], "S3")
        self.assertEqual(season["observation_start"], "2026-10-16")
        self.assertEqual(season["periods"][0]["start"], "2026-10-16")
        self.assertTrue(all(row["start"] >= "2026-10-16" for row in season["periods"]))
        self.assertEqual(season["start"], "2026-09-20")

        payload = attach_public_stages(
            {
                "land_id": "18509",
                "seasons": [dict(season)],
                "season_id": season["season_id"],
                "periods": season["periods"],
            },
            calendar,
        )
        names = [row["name"] for row in payload["seasons"][0]["stages"]]
        self.assertEqual(
            names, ["播种前准备", "播种期", "出苗期", "拔节孕穗期", "成熟期"]
        )
        pre = payload["seasons"][0]["stages"][0]
        self.assertEqual(pre["mapping_status"], "NOT_APPLICABLE")
        self.assertIsNone(pre["phase_label"])
        self.assertFalse(pre["monitored"])
        self.assertEqual(pre["start"], "2026-09-20")
        self.assertEqual(pre["end_inclusive"], "2026-10-05")
        self.assertEqual(payload["stages"], payload["seasons"][0]["stages"])
        self.assertEqual(payload["native_plan"]["batch_id"], "8801")
        self.assertEqual(len(public_stage_intervals(None)), 0)
        self.assertNotIn("孕穗", names)
        # Aliases stay on the plan only when present; this land has official 拔节孕穗期, not 起身期.
        self.assertNotIn("起身期", names)

    def test_mysql_disabled_is_documented(self):
        saved_enabled = settings.mysql_source_enabled
        saved_url = settings.mysql_source_url
        settings.mysql_source_enabled = False
        settings.mysql_source_url = ""
        try:
            payload = asyncio.run(load_smart_planting_calendar("6417"))
        finally:
            settings.mysql_source_enabled = saved_enabled
            settings.mysql_source_url = saved_url
        self.assertEqual(payload["plan_status"], "smart_source_unavailable")
        self.assertEqual(payload["note"], SOURCE_UNAVAILABLE_NOTE)
        self.assertEqual(payload["land_id"], "6417")
        self.assertFalse(payload["plan_available"])


class _Conn:
    def __init__(self, batches=None, tasks=None):
        self.batches = batches or []
        self.tasks = tasks or []
        self.calls: list[tuple[str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, params=None):
        statement = str(sql)
        self.calls.append((statement, params))
        if "crop_batch_land_scheme" in statement:
            raise AssertionError("planting lookup must not use crop_batch_land_scheme")
        if "LIKE" in statement.upper():
            raise AssertionError("planting lookup must not use LIKE")
        result = MagicMock()
        if "agriculture_plant_task" in statement:
            rows = self.tasks
        elif "agriculture_batch_task" in statement:
            rows = self.batches
        else:
            raise AssertionError(statement)
        result.mappings.return_value.all.return_value = rows
        return result


class _Engine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def _install_engine(conn):
    """替换延迟导入的 MySQL 引擎，不加载地块同步模块的空间依赖。"""
    module = types.ModuleType("app.services.mysql_land_sync")
    module._mysql_engine = lambda: _Engine(conn)
    previous = sys.modules.get("app.services.mysql_land_sync")
    sys.modules["app.services.mysql_land_sync"] = module
    return previous


def _restore_engine(previous) -> None:
    if previous is None:
        sys.modules.pop("app.services.mysql_land_sync", None)
    else:
        sys.modules["app.services.mysql_land_sync"] = previous


class SmartPlantingQueryTests(unittest.TestCase):
    def setUp(self):
        self._enabled = settings.mysql_source_enabled
        self._url = settings.mysql_source_url
        settings.mysql_source_enabled = True
        settings.mysql_source_url = (
            "mysql+asyncmy://user:pass@127.0.0.1:3306/smart_agric"
        )

    def tearDown(self):
        settings.mysql_source_enabled = self._enabled
        settings.mysql_source_url = self._url

    def _load(self, conn, land_id="23860"):
        previous = _install_engine(conn)
        try:
            return asyncio.run(load_smart_planting_calendar(land_id))
        finally:
            _restore_engine(previous)

    def test_active_batches_come_from_batch_task_status(self):
        conn = _Conn(
            batches=[_batch(land_id=23860, batch_id=9510)],
            tasks=[_task(name="出苗期", start="2025-10-18", finish="2025-11-10")],
        )
        payload = self._load(conn)
        batch_sql, batch_params = conn.calls[0]
        self.assertIn("agriculture_batch_task", batch_sql)
        self.assertIn("status IN", batch_sql)
        self.assertNotIn("crop_batch_land_scheme", batch_sql)
        self.assertNotIn("agriculture_crop_batch.land_id", batch_sql)
        self.assertEqual(batch_params["land_id"], 23860)
        self.assertEqual(batch_params["crop_id"], 4)
        self.assertEqual(
            tuple(batch_params["planting_statuses"]), PLANTING_TASK_STATUSES
        )
        self.assertEqual(PLANTING_TASK_STATUSES, ("0", "1", "2"))
        self.assertNotIn("3", PLANTING_TASK_STATUSES)
        self.assertNotIn("4", PLANTING_TASK_STATUSES)
        self.assertIn("agriculture_plant_task", conn.calls[1][0])
        self.assertEqual(conn.calls[1][1]["plan_ids"], [9510])
        self.assertTrue(payload["plan_available"])
        self.assertEqual(payload["seasons"][0]["land_id"], "23860")
        self.assertEqual(payload["seasons"][0]["batch_id"], "9510")

    def test_finished_or_unlinked_land_stays_empty(self):
        conn = _Conn(batches=[])
        payload = self._load(conn)
        self.assertEqual(len(conn.calls), 1)
        self.assertFalse(payload["plan_available"])
        self.assertEqual(payload["plan_status"], "missing")
        self.assertEqual(payload["note"], NO_ACTIVE_BATCH_NOTE)
        self.assertEqual(payload["seasons"], [])

    def test_drops_a_row_whose_land_id_is_not_the_request(self):
        conn = _Conn(batches=[_batch(land_id=16417, batch_id=99)])
        payload = self._load(conn)
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(payload["seasons"], [])
        self.assertEqual(payload["note"], NO_ACTIVE_BATCH_NOTE)


if __name__ == "__main__":
    unittest.main()
