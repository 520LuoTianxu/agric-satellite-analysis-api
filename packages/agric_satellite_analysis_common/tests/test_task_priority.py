"""统一任务优先级及 Redis 适配测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from agric_satellite_analysis_common.mq_schemas import TaskMessage
from agric_satellite_analysis_common.task_priority import (
    BACKGROUND_TASK_PRIORITY,
    CELERY_BACKGROUND_PRIORITY,
    INTERACTIVE_REPORT_PRIORITY,
    MANUAL_TASK_PRIORITY,
    celery_priority_for,
    inject_default_celery_priority,
    normalize_task_priority,
)


class TaskPriorityTests(unittest.TestCase):
    def test_business_priority_is_normalized(self) -> None:
        self.assertEqual(normalize_task_priority(None), BACKGROUND_TASK_PRIORITY)
        self.assertEqual(normalize_task_priority(999), INTERACTIVE_REPORT_PRIORITY)
        self.assertEqual(normalize_task_priority(-1), BACKGROUND_TASK_PRIORITY)
        self.assertEqual(MANUAL_TASK_PRIORITY, 5)

    def test_redis_priority_reverses_business_order(self) -> None:
        # Kombu Redis list 越小越先出队；1 是故意避开的最小有效值，避免
        # Kombu 将 priority=0 当成未传入并替换为默认值。
        self.assertEqual(celery_priority_for(INTERACTIVE_REPORT_PRIORITY), 1)
        self.assertEqual(celery_priority_for(MANUAL_TASK_PRIORITY), 4)
        self.assertEqual(celery_priority_for(BACKGROUND_TASK_PRIORITY), 9)

    def test_task_message_is_backward_compatible(self) -> None:
        old = TaskMessage(task_id="t1", type="weather_backfill", land_id="L1")
        self.assertEqual(old.priority, BACKGROUND_TASK_PRIORITY)
        self.assertEqual(TaskMessage(task_id="t2", priority=9).priority, 9)
        with self.assertRaises(ValidationError):
            TaskMessage(task_id="bad", priority=10)

    def test_missing_celery_priority_is_background(self) -> None:
        properties: dict[str, object] = {}
        inject_default_celery_priority(properties=properties)
        self.assertEqual(properties["priority"], CELERY_BACKGROUND_PRIORITY)

        explicit = {"priority": 1}
        inject_default_celery_priority(properties=explicit)
        self.assertEqual(explicit["priority"], 1)

    def test_create_app_enables_priority_queue_controls(self) -> None:
        from agric_satellite_analysis_common.celery_app import create_celery_app

        with patch("agric_satellite_analysis_common.celery_app.settings.redis_url", "redis://localhost/15"):
            app = create_celery_app(name="priority-test", include=[])
        self.assertEqual(app.conf.worker_prefetch_multiplier, 1)
        self.assertTrue(app.conf.task_inherit_parent_priority)
        self.assertEqual(app.conf.task_queue_max_priority, 9)
        self.assertEqual(app.conf.broker_transport_options["priority_steps"], list(range(10)))
        self.assertNotIn(
            "priority_steps", app.conf.result_backend_transport_options
        )


if __name__ == "__main__":
    unittest.main()
