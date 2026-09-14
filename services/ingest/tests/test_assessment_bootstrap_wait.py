"""Unit tests: assessment bootstrap readiness helper (no sys.modules stubs)."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.tasks.assessment_report import bootstrap_pulls_ready


class BootstrapPullsReadyTests(unittest.TestCase):
    def test_not_ready_when_weather_or_soil_missing(self) -> None:
        session = MagicMock()
        with (
            patch(
                "app.tasks.assessment_report._celery_ids_ready",
                return_value=(True, []),
            ),
            patch("app.tasks.assessment_report._weather_row_count", return_value=0),
            patch("app.tasks.assessment_report._soil_ready", return_value=False),
            patch("app.tasks.assessment_report._active_backfill_jobs", return_value=0),
        ):
            status = bootstrap_pulls_ready(
                session,
                field_id=uuid.uuid4(),
                date_from="2024-01-01",
                date_to="2024-01-31",
                wait_celery_ids=["c1"],
                wave_cutoff=datetime.now(timezone.utc),
            )
        self.assertFalse(status["ready"])
        self.assertFalse(status["weather_ok"])
        self.assertFalse(status["soil_ok"])

    def test_ready_when_pulls_complete(self) -> None:
        session = MagicMock()
        with (
            patch(
                "app.tasks.assessment_report._celery_ids_ready",
                return_value=(True, []),
            ),
            patch("app.tasks.assessment_report._weather_row_count", return_value=30),
            patch("app.tasks.assessment_report._soil_ready", return_value=True),
            patch("app.tasks.assessment_report._active_backfill_jobs", return_value=0),
        ):
            status = bootstrap_pulls_ready(
                session,
                field_id=uuid.uuid4(),
                date_from="2024-01-01",
                date_to="2024-01-31",
                wait_celery_ids=["c1", "c2"],
                wave_cutoff=datetime.now(timezone.utc),
            )
        self.assertTrue(status["ready"])
        self.assertTrue(status["rs_ok"])

    def test_not_ready_while_rs_jobs_active(self) -> None:
        session = MagicMock()
        with (
            patch(
                "app.tasks.assessment_report._celery_ids_ready",
                return_value=(True, []),
            ),
            patch("app.tasks.assessment_report._weather_row_count", return_value=30),
            patch("app.tasks.assessment_report._soil_ready", return_value=True),
            patch("app.tasks.assessment_report._active_backfill_jobs", return_value=3),
        ):
            status = bootstrap_pulls_ready(
                session,
                field_id=uuid.uuid4(),
                date_from=None,
                date_to=None,
                wait_celery_ids=None,
                wave_cutoff=datetime.now(timezone.utc),
                started_at=datetime.now(timezone.utc),
                min_wait_seconds=0,
            )
        self.assertFalse(status["ready"])
        self.assertFalse(status["rs_ok"])
        self.assertEqual(status["active_rs_jobs"], 3)


if __name__ == "__main__":
    unittest.main()
