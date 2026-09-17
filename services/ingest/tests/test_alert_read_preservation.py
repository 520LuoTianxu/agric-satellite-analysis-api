"""重算仍命中的预警保留 ID，避免数据库级联清除个人已读。"""

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.tasks.agri_alerts import _delete_open_rs_alerts, _emit_rules
from app.tasks.indices import INDEX_REGISTRY


class AlertReadPreservationTests(unittest.TestCase):
    def test_repeated_scene_does_not_recreate_or_delete_matching_alerts(self):
        today = date(2026, 9, 17)
        threshold = (today, "ndvi_threshold")
        drop = (today, "ndvi_drop")
        active = set()
        session = MagicMock()
        created = _emit_rules(
            session,
            land_id="A",
            scene_date=today,
            current_mean=0.1,
            historical_means=[0.6, 0.6, 0.6, 0.1],
            index_def=INDEX_REGISTRY["ndvi"],
            weather_ctx=None,
            existing={threshold, drop},
            active_keys=active,
        )
        self.assertEqual(created, 0)
        session.add.assert_not_called()
        self.assertEqual(active, {threshold, drop})
        kept = [SimpleNamespace(date=day, rule_name=rule) for day, rule in active]
        old = SimpleNamespace(date=date(2026, 9, 10), rule_name="ndvi_threshold")
        session.execute.return_value.scalars.return_value.all.return_value = [
            *kept,
            old,
        ]
        self.assertEqual(_delete_open_rs_alerts(session, "A", ["ndvi"], active), 1)
        session.delete.assert_called_once_with(old)

    def test_new_scene_creates_new_unread_alerts(self):
        session = MagicMock()
        active = set()
        created = _emit_rules(
            session,
            land_id="A",
            scene_date=date(2026, 9, 18),
            current_mean=0.1,
            historical_means=[0.1],
            index_def=INDEX_REGISTRY["ndvi"],
            weather_ctx=None,
            existing={(date(2026, 9, 17), "ndvi_threshold")},
            active_keys=active,
        )
        self.assertEqual(created, 1)
        session.add.assert_called_once()


if __name__ == "__main__":
    unittest.main()
