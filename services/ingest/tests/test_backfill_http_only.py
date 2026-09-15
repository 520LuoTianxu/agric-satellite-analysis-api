"""http_only backfill orchestration does not open SyncSession."""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class BackfillHttpOnlyTests(unittest.TestCase):
    def test_http_only_land_dispatches_without_session(self) -> None:
        # Import inside test so docker image deps are available.
        from app.tasks import backfill as bf

        celery = MagicMock()
        sends = []

        def _send(name, args=None, kwargs=None, countdown=0):
            sends.append((name, args, kwargs, countdown))
            return SimpleNamespace(id="t1")

        celery.send_task.side_effect = _send
        s1_delay = MagicMock(return_value=SimpleNamespace(id="s1t"))

        with (
            patch.object(bf, "celery_app", celery),
            patch.object(bf, "get_db_session") as get_db,
            patch("app.core.http_mode.ingest_http_only", return_value=True),
            patch(
                "app.core.http_mode.resolve_land_http",
                return_value={"land_id": "7570", "tile_id": "tile-7570"},
            ),
            patch("app.core.http_mode.patch_job_http"),
            patch("app.core.http_mode.get_job_http", return_value=None),
            patch.dict(
                sys.modules,
                {
                    "app.tasks.sentinel1": SimpleNamespace(
                        backfill_s1_for_land=SimpleNamespace(delay=s1_delay)
                    )
                },
            ),
            patch.object(
                bf.settings,
                "index_backfill_months",
                36,
                create=True,
            ),
            patch.object(
                bf.settings,
                "index_backfill_chunk_days",
                90,
                create=True,
            ),
        ):
            out = bf.backfill_indices_for_land.run(
                "7570",
                months=3,
                date_from="2026-06-01",
                date_to="2026-09-01",
            )

        get_db.assert_not_called()
        self.assertEqual(out["status"], "dispatched")
        self.assertTrue(out.get("http_only"))
        self.assertGreater(out["jobs"], 0)
        self.assertTrue(any("process_agri_optical_lonlat" in s[0] for s in sends))
        optical = next(s for s in sends if "process_agri_optical_lonlat" in s[0])
        self.assertIsNone(optical[1])
        self.assertEqual(
            optical[2]["land_id"], "7570"
        )
        self.assertEqual(optical[2]["date_from"], "2026-06-01")


if __name__ == "__main__":
    unittest.main()
