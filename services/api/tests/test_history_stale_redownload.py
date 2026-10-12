"""history-backfill 的 redownload_stale_versions：只重下非当前算法版本的 S2 景。"""

from __future__ import annotations

import asyncio
import pathlib
import re
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.satellite_batch import SatelliteHistoryBackfillRequest
from app.services import satellite_history as sh


class _Session:
    def __init__(self):
        self.added = []
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        self.execute = AsyncMock(return_value=result)
        self.commit = AsyncMock()

    def add(self, obj):
        self.added.append(obj)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class StaleRedownloadTests(unittest.TestCase):
    def _run(self, **kw):
        session = _Session()
        create = AsyncMock(return_value=([], [], None))
        with (
            patch("app.core.database.async_session", return_value=session),
            patch.object(sh, "create_satellite_batch_jobs", create),
            patch.object(sh, "find_invalid_satellite_lands", AsyncMock(return_value=set())),
        ):
            asyncio.run(
                sh.backfill_satellite_history(
                    land_ids=["20308"], date_from=date(2026, 8, 1),
                    date_to=date(2026, 10, 12), sensors=["S2"], **kw,
                )
            )
        return create.await_args.kwargs, session.added[0].params_json

    def test_flag_passes_current_version_to_jobs(self) -> None:
        kwargs, parent = self._run(redownload_stale_versions=True)
        self.assertEqual(
            kwargs["extra_params"],
            {"existing_algorithm_version": sh.CURRENT_S2_ALGORITHM_VERSION},
        )
        self.assertFalse(kwargs["force"])
        self.assertTrue(parent["redownload_stale_versions"])

    def test_default_unchanged(self) -> None:
        kwargs, parent = self._run()
        self.assertIsNone(kwargs["extra_params"])
        self.assertFalse(parent["redownload_stale_versions"])

    def test_force_and_stale_are_exclusive(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(
                sh.backfill_satellite_history(
                    land_ids=["1"], date_from=date(2026, 8, 1), date_to=date(2026, 8, 2),
                    force=True, redownload_stale_versions=True,
                )
            )

    def test_request_schema_default_false(self) -> None:
        self.assertFalse(SatelliteHistoryBackfillRequest().redownload_stale_versions)
        self.assertTrue(
            SatelliteHistoryBackfillRequest(redownload_stale_versions=True).redownload_stale_versions
        )

    def test_version_matches_ingest(self) -> None:
        src = pathlib.Path(__file__).resolve().parents[2] / "ingest/app/tasks/agri_lonlat.py"
        m = re.search(r'AGRI_OPTICAL_ALGORITHM_VERSION = "([^"]+)"', src.read_text())
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), sh.CURRENT_S2_ALGORITHM_VERSION)


if __name__ == "__main__":
    unittest.main()
