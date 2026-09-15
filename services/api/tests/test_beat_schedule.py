"""周指数计划辅助的单测，不连数据库。"""

from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from app.services.beat_schedule import WEEKLY_INDEX_KEYS, index_task_name, weekly_date_window


class WeeklyDateWindowTests(TestCase):
    def test_fresh_layer_is_skipped(self) -> None:
        today = date(2026, 9, 15)
        latest = today - timedelta(days=3)
        self.assertIsNone(weekly_date_window(latest, today=today))

    def test_stale_layer_starts_the_day_after(self) -> None:
        today = date(2026, 9, 15)
        latest = today - timedelta(days=10)
        window = weekly_date_window(latest, today=today)
        self.assertEqual(window, (date(2026, 9, 6), today))

    def test_no_layers_uses_seven_day_window(self) -> None:
        today = date(2026, 9, 15)
        window = weekly_date_window(None, today=today)
        self.assertEqual(window, (date(2026, 9, 8), today))


class WeeklyIndexTests(TestCase):
    def test_only_canonical_optical_task_is_scheduled(self) -> None:
        self.assertEqual(WEEKLY_INDEX_KEYS, ("agri_optical",))
        self.assertEqual(
            index_task_name("agri_optical"),
            "app.tasks.agri_lonlat.process_agri_optical_lonlat",
        )

    def test_unknown_task_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            index_task_name("ndvi")
