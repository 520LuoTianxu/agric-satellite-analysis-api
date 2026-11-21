"""Smart planting calendar lookup: exact land_id, plant_task windows, no substitution."""

from __future__ import annotations

import asyncio
import unittest
from agric_satellite_analysis_common.biweekly.models import ReportError
from app.core.config import settings
from app.services.biweekly_plans import (
    MISSING_NOTE,
    SOURCE_UNAVAILABLE_NOTE,
    SmartPlantingCatalog,
    build_seasons_from_rows,
    exact_land_id,
    load_smart_planting_calendar,
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


def _task(plan_id=9510, name="返青期", start="2028-02-21", finish="2028-02-28", task_id=1):
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

    def test_builds_planting_season_and_official_intervals(self):
        tasks = [
            _task(name="整地播种期", start="2027-09-20", finish="2027-11-01", task_id=1),
            _task(name="出苗至三叶期", start="2027-11-02", finish="2027-11-17", task_id=2),
            _task(name="越冬期", start="2028-01-19", finish="2028-01-25", task_id=3),
            _task(name="起身期", start="2028-03-10", finish="2028-03-13", task_id=4),
            _task(name="拔节期", start="2028-03-28", finish="2028-04-13", task_id=5),
            _task(name="抽穗至开花期", start="2028-04-21", finish="2028-05-04", task_id=6),
        ]
        payload = build_seasons_from_rows("18407", [_batch()], tasks)
        self.assertTrue(payload["plan_available"])
        season = payload["seasons"][0]
        self.assertEqual(season["season_id"], "2027/28")
        self.assertEqual(season["label"], "2027/28 种植季")
        self.assertEqual(season["crop_name"], "小麦")
        self.assertEqual(season["start"], "2027-09-20")
        self.assertEqual(season["end_inclusive"], "2028-05-04")
        self.assertTrue(season["periods"])
        self.assertEqual(season["periods"][0]["start"], "2027-09-20")
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


if __name__ == "__main__":
    unittest.main()
