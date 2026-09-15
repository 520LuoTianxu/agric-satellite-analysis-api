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
_TEST_STUBS = {
    "app.core.database_sync": SimpleNamespace(SyncSession=MagicMock()),
    "app.core.logging": SimpleNamespace(logger=MagicMock()),
    "app.tasks.storage_tasks": SimpleNamespace(upload_file_via_storage=MagicMock()),
    "app.models.tables": SimpleNamespace(
        Job=object, SoilProfile=object, WeatherDaily=object, Field=object
    ),
    "app.reports.land_assessment.scorecard_view": SimpleNamespace(
        scorecard_public_view=lambda x: x
    ),
    "app.reports.land_assessment.service": SimpleNamespace(
        generate_assessment_pdf=MagicMock()
    ),
    "app.worker": SimpleNamespace(celery_app=MagicMock()),
    "sqlalchemy": SimpleNamespace(func=MagicMock(), select=MagicMock()),
    "sqlalchemy.orm.attributes": SimpleNamespace(flag_modified=MagicMock()),
}

# 只在导入被测模块时注入轻量替身，导入完成后立即恢复 sys.modules，避免污染其他测试。
with patch.dict(sys.modules, _TEST_STUBS):
    from app.tasks import assessment_report as ar  # noqa: E402


class BootstrapPullsReadyTests(unittest.TestCase):
    def test_not_ready_when_weather_or_soil_missing(self) -> None:
        session = MagicMock()
        with (
            patch.object(ar, "_celery_ids_ready", return_value=(True, [])),
            patch.object(ar, "_weather_row_count", return_value=0),
            patch.object(ar, "_soil_ready", return_value=False),
            patch.object(ar, "_active_backfill_jobs", return_value=0),
            patch.object(ar, "_agri_rs_coverage_ok", return_value={"ok": False}),
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
            patch.object(ar, "_agri_rs_coverage_ok", return_value={"ok": False}),
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

    def test_not_ready_while_rs_jobs_active_without_coverage(self) -> None:
        session = MagicMock()
        with (
            patch.object(ar, "_celery_ids_ready", return_value=(True, [])),
            patch.object(ar, "_weather_row_count", return_value=30),
            patch.object(ar, "_soil_ready", return_value=True),
            patch.object(ar, "_active_backfill_jobs", return_value=3),
            patch.object(ar, "_agri_rs_coverage_ok", return_value={"ok": False}),
        ):
            status = ar.bootstrap_pulls_ready(
                session,
                field_id=uuid.uuid4(),
                date_from=None,
                date_to=None,
                wait_celery_ids=["c1"],
                wave_cutoff=datetime.now(timezone.utc),
            )
        self.assertFalse(status["ready"])
        self.assertFalse(status["rs_ok"])
        self.assertEqual(status["active_rs_jobs"], 3)

    def test_ready_when_coverage_ok_despite_active_skip_jobs(self) -> None:
        """Staggered skip-noop backfills must not block when scenes already exist."""
        session = MagicMock()
        coverage = {
            "ok": True,
            "land_id": "15411",
            "s2_dates": 300,
            "s1_dates": 100,
            "s2_min": 36,
            "s1_min": 18,
            "span_days": 1096,
        }
        with (
            patch.object(ar, "_celery_ids_ready", return_value=(True, [])),
            patch.object(ar, "_weather_row_count", return_value=1000),
            patch.object(ar, "_soil_ready", return_value=True),
            patch.object(ar, "_active_backfill_jobs", return_value=26),
            patch.object(ar, "_agri_rs_coverage_ok", return_value=coverage),
        ):
            status = ar.bootstrap_pulls_ready(
                session,
                field_id=uuid.uuid4(),
                date_from="2023-09-14",
                date_to="2026-09-14",
                wait_celery_ids=["c1"],
                wave_cutoff=datetime.now(timezone.utc),
                started_at=datetime.now(timezone.utc),
                min_wait_seconds=0,
            )
        self.assertTrue(status["ready"])
        self.assertTrue(status["rs_ok"])
        self.assertTrue(status["rs_coverage_ok"])
        self.assertEqual(status["active_rs_jobs"], 26)

    def test_require_rs_coverage_blocks_empty_coverage(self) -> None:
        session = MagicMock()
        with (
            patch.object(ar, "_celery_ids_ready", return_value=(True, [])),
            patch.object(ar, "_weather_row_count", return_value=30),
            patch.object(ar, "_soil_ready", return_value=True),
            patch.object(ar, "_active_backfill_jobs", return_value=0),
            patch.object(ar, "_agri_rs_coverage_ok", return_value={"ok": False}),
            patch.object(ar, "_data_readiness_http", return_value=None),
        ):
            status = ar.bootstrap_pulls_ready(
                session,
                field_id=uuid.uuid4(),
                date_from="2024-01-01",
                date_to="2024-12-31",
                wait_celery_ids=["c1"],
                wave_cutoff=datetime.now(timezone.utc),
                started_at=datetime.now(timezone.utc)
                - __import__("datetime").timedelta(seconds=60),
                min_wait_seconds=45,
                require_rs_coverage=True,
            )
        self.assertFalse(status["ready"])
        self.assertFalse(status["rs_ok"])

    def test_resolve_wait_started_at_uses_remote_created_at(self) -> None:
        remote = {"created_at": "2026-09-15T03:00:00+00:00", "started_at": None}
        fake = SimpleNamespace(
            internal_api_enabled=lambda: True,
            get_job=lambda *_a, **_k: remote,
        )
        with patch.dict(sys.modules, {"openfarm_common.internal_api": fake}):
            started = ar._resolve_wait_started_at(None, "job-1")
        self.assertEqual(started.year, 2026)
        self.assertEqual(started.hour, 3)


if __name__ == "__main__":
    unittest.main()
