"""File-backend official store for catalog/router unit tests.

These tests are not about the SQLAlchemy adapter. In this environment
DATABASE_URL often points at the compose hostname `db`, so a live
SqlAlchemyOfficialStore would fail closed with 503. Production still
fail-closes when the store is attached and the database is down.
"""

from __future__ import annotations

from unittest.mock import patch

from agric_satellite_analysis_common.biweekly.official_ref import OfficialStoreNotMigrated


class LegacyWorkDirOfficialStore:
    def get(self, *args, **kwargs):
        raise OfficialStoreNotMigrated()

    upsert = apply_event = latest_for_period = list_for_land_season = get


def patch_official_store_not_migrated():
    return patch(
        "app.services.biweekly_official_store.build_official_store",
        return_value=LegacyWorkDirOfficialStore(),
    )
