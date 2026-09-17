"""Tests for SQLAlchemy sync URL resolution."""

from __future__ import annotations

import unittest

from agric_satellite_analysis_common import settings as settings_mod


class ResolveSyncDatabaseUrlTests(unittest.TestCase):
    def test_prefers_explicit_sync_url(self) -> None:
        self.assertEqual(
            settings_mod.resolve_sync_database_url(
                "postgresql://sync:pw@db:5432/openfarm",
                "postgresql+asyncpg://async:pw@db:5432/openfarm",
            ),
            "postgresql://sync:pw@db:5432/openfarm",
        )

    def test_converts_asyncpg_url_when_sync_missing(self) -> None:
        self.assertEqual(
            settings_mod.resolve_sync_database_url(
                "",
                "postgresql+asyncpg://openfarm:openfarm_dev@db:5432/openfarm",
            ),
            "postgresql://openfarm:openfarm_dev@db:5432/openfarm",
        )

    def test_whitespace_only_sync_url_falls_back_to_async(self) -> None:
        self.assertEqual(
            settings_mod.resolve_sync_database_url(
                "   ",
                "postgresql+asyncpg://openfarm:openfarm_dev@db:5432/openfarm",
            ),
            "postgresql://openfarm:openfarm_dev@db:5432/openfarm",
        )

    def test_strips_surrounding_whitespace(self) -> None:
        self.assertEqual(
            settings_mod.resolve_sync_database_url(
                "  postgresql://agri:pw@db:3433/agri_mate  ",
                "",
            ),
            "postgresql://agri:pw@db:3433/agri_mate",
        )

    def test_rejects_empty_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            settings_mod.resolve_sync_database_url("", "")

    def test_rejects_unparseable_url(self) -> None:
        with self.assertRaisesRegex(ValueError, "Could not parse SQLAlchemy URL"):
            settings_mod.resolve_sync_database_url(
                "jdbc:postgresql://db:5432/openfarm", ""
            )
