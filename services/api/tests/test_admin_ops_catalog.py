"""管理员运维任务目录的架构约束测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.routers.admin_ops import _TASK_CATALOG, _task_outputs


class AdminOpsCatalogTests(unittest.TestCase):
    def test_weekly_index_is_replaced_by_api_mysql_sync(self) -> None:
        self.assertNotIn("weekly-index", _TASK_CATALOG)
        self.assertIn("mysql-land-sync", _TASK_CATALOG)
        self.assertIn("smart-land-backfill", _TASK_CATALOG)
        self.assertEqual(
            _TASK_CATALOG["mysql-land-sync"]["task_name"],
            "app.services.mysql_land_sync.run_land_sync",
        )
        self.assertEqual(
            _TASK_CATALOG["smart-land-backfill"]["task_name"],
            "app.services.smart_land_backfill.run_smart_land_backfill",
        )
        self.assertEqual(_TASK_CATALOG["daily-satellite"]["schedule"], "每天 01:00（北京时间）")
        self.assertEqual(_TASK_CATALOG["overview-refresh"]["schedule"], "每天 04:00（北京时间）")
        self.assertEqual(
            _TASK_CATALOG["mysql-land-sync"]["schedule"],
            "每天 22:00（北京时间，API 机）",
        )

    def test_mysql_sync_enabled_state_comes_from_api_source_switch(self) -> None:
        with patch("app.routers.admin_ops.settings.mysql_source_enabled", True):
            tasks = {task.key: task for task in _task_outputs()}
        self.assertTrue(tasks["mysql-land-sync"].enabled)
        self.assertTrue(tasks["smart-land-backfill"].enabled)
        self.assertNotIn("weekly-index", tasks)


if __name__ == "__main__":
    unittest.main()
