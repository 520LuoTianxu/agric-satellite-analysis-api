"""D4.1: load_field_bundle prefers internal HTTP when configured."""

from __future__ import annotations

import os
import unittest
import uuid
from unittest.mock import patch

from app.reports.land_assessment import data_loader as dl


class LoadFieldBundleHttpTests(unittest.TestCase):
    def test_http_path_used_when_enabled(self) -> None:
        fid = uuid.uuid4()
        fake = {
            "field": {"id": str(fid), "name": "西叩"},
            "indices": [],
            "soil": {},
            "weather_summary": {},
            "weather_stress": {},
            "weather_history": {},
            "suitability": {},
            "site_admission": None,
        }
        with (
            patch.dict(
                os.environ,
                {
                    "API_BASE_URL": "http://api.test",
                    "INTERNAL_API_TOKEN": "tok",
                    "INGEST_PG_WRITES": "0",
                },
                clear=False,
            ),
            patch(
                "openfarm_common.internal_api.assessment_bundle",
                return_value=fake,
            ) as ab,
        ):
            out = dl.load_field_bundle(None, fid)
            self.assertEqual(out["field"]["name"], "西叩")
            ab.assert_called_once_with(str(fid))

    def test_http_failure_raises_when_pg_reads_disallowed(self) -> None:
        fid = uuid.uuid4()
        with (
            patch.dict(
                os.environ,
                {
                    "API_BASE_URL": "http://api.test",
                    "INTERNAL_API_TOKEN": "tok",
                    "INGEST_PG_WRITES": "0",
                },
                clear=False,
            ),
            patch(
                "openfarm_common.internal_api.assessment_bundle",
                side_effect=RuntimeError("boom"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                dl.load_field_bundle(None, fid)


if __name__ == "__main__":
    unittest.main()
