"""assessment-bundle should use Settings/.env when process env has no DATABASE_URL*."""

from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import patch

from sqlalchemy.engine.url import make_url


class SyncDatabaseUrlFromSettingsTests(unittest.TestCase):
    def test_resolve_uses_settings_when_os_environ_is_empty(self) -> None:
        from app.core.config import settings
        from agric_satellite_analysis_common.settings import resolve_sync_database_url

        with patch.dict(
            os.environ,
            {"DATABASE_URL": "", "DATABASE_URL_SYNC": ""},
            clear=False,
        ):
            url = resolve_sync_database_url(
                getattr(settings, "database_url_sync", ""),
                settings.database_url,
            )

        make_url(url)
        self.assertTrue(url.startswith("postgresql://"))

    def test_database_sync_engine_ignores_empty_os_environ(self) -> None:
        import app.core.database_sync as db_sync

        with patch.dict(
            os.environ,
            {"DATABASE_URL": "", "DATABASE_URL_SYNC": ""},
            clear=False,
        ):
            db_sync = importlib.reload(db_sync)

        make_url(str(db_sync.sync_engine.url))
        self.assertTrue(str(db_sync.sync_engine.url).startswith("postgresql://"))
