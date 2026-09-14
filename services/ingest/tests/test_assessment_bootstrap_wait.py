"""Unit tests: assessment bootstrap readiness helper."""

from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Lightweight stubs so importing assessment_report does not need full stack.
sys.modules.setdefault("app.core.database_sync", SimpleNamespace(SyncSession=MagicMock()))
sys.modules.setdefault("app.core.logging", SimpleNamespace(logger=MagicMock()))
sys.modules.setdefault(
    "app.tasks.storage_tasks",
    SimpleNamespace(upload_file_via_storage=MagicMock()),
)
sys.modules.setdefault(
    "app.models.tables",
    SimpleNamespace(Job=object, SoilProfile=object, WeatherDaily=object),
)
sys.modules.setdefault(
    "app.reports.land_assessment.scorecard_view",
    SimpleNamespace(scorecard_public_view=lambda x: x),
)
sys.modules.setdefault(
    "app.reports.land_assessment.service",
    SimpleNamespace(generate_assessment_pdf=MagicMock()),
)
sys.modules.setdefault("app.worker", SimpleNamespace(celery_app=MagicMock()))
sys.modules.setdefault("sqlalchemy", SimpleNamespace(func=MagicMock(), select=MagicMock()))
sys.modules.setdefault(
    "sqlalchemy.orm.attributes",
    SimpleNamespace(flag_modified=MagicMock()),
)

from app.tasks import assessment_report as ar  # noqa: E402


class BootstrapPullsReadyTests(unittest.TestCase):
    def test_not_ready_when_weather_or_soil_missing(self) -> None:
        session = MagicMock()
        with (
            patch.object(ar, "_celery_ids_ready", return_value=(True, [])),
            patch.object(ar, "_weather_row_count", return_value=0),
            patch.object(ar, "_soil_ready", return_value=False),
            patch.object(ar, "_active_backfill_jobs", return_value=0),
        ):
            status = ar.bootstrap_pulls_ready(
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
            patch.object(ar, "_celery_ids_ready", return_value=(True, [])),
            patch.object(ar, "_weather_row_count", return_value=30),
            patch.object(ar, "_soil_ready", return_value=True),
            patch.object(ar, "_active_backfill_jobs", return_value=0),
        ):
            status = ar.bootstrap_pulls_ready(
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
            patch.object(ar, "_celery_ids_ready", return_value=(True, [])),
            patch.object(ar, "_weather_row_count", return_value=30),
            patch.object(ar, "_soil_ready", return_value=True),
            patch.object(ar, "_active_backfill_jobs", return_value=3),
        ):
            status = ar.bootstrap_pulls_ready(
                session,
                field_id=uuid.uuid4(),
                date_from=None,
                date_to=None,
                wait_celery_ids=None,
                wave_cutoff=datetime.now(timezone.utc),
            )
        self.assertFalse(status["ready"])
        self.assertFalse(status["rs_ok"])
        self.assertEqual(status["active_rs_jobs"], 3)


if __name__ == "__main__":
    unittest.main()
