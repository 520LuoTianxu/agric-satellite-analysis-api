"""定时任务 AdminTaskRun 旁路跟踪测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.admin_task_tracking import track_admin_task_run


class AdminTaskTrackingTests(unittest.TestCase):
    def test_auto_task_is_created_and_reported_without_changing_result(self):
        @track_admin_task_run(
            task_key="overview-refresh",
            task_name="app.tasks.overview_preagg.refresh_overview_stats",
            execution_key=lambda _args, kwargs: str(kwargs.get("window_days", 60)),
            params=lambda _args, kwargs: {"window_days": kwargs.get("window_days", 60)},
        )
        def task(window_days=60, **_kwargs):
            return {"window_days": window_days, "status": "success"}

        with (
            patch(
                "agric_satellite_analysis_common.internal_api.internal_api_enabled",
                return_value=True,
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.ensure_admin_task_run",
                return_value={"run_id": "run-1"},
            ) as ensure,
            patch(
                "agric_satellite_analysis_common.internal_api.update_admin_task_run_status"
            ) as update,
        ):
            result = task(window_days=30)

        self.assertEqual(result["window_days"], 30)
        ensure.assert_called_once_with(
            "overview-refresh",
            "app.tasks.overview_preagg.refresh_overview_stats",
            "30",
            params={"window_days": 30},
        )
        self.assertEqual([call.args[1] for call in update.call_args_list], ["running", "success"])


if __name__ == "__main__":
    unittest.main()
