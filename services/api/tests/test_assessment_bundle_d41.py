"""Unit tests for D4.1 assessment-bundle / data-readiness helpers."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.routers import internal_fields as fields_mod


class AssessmentBundleSyncTests(unittest.TestCase):
    def test_sync_loader_passes_allow_http_false(self) -> None:
        fake_bundle = {"field": {"id": "f1"}, "indices": []}
        with (
            patch.object(fields_mod, "_parse_field_uuid", return_value="fid"),
            patch("app.core.database_sync.SyncSession") as sess_cls,
            patch(
                "app.reports.land_assessment.data_loader.load_field_bundle",
                return_value=fake_bundle,
            ) as load,
        ):
            session = MagicMock()
            sess_cls.return_value = session
            out = fields_mod._sync_load_assessment_bundle(
                "00000000-0000-0000-0000-000000000001"
            )
            self.assertEqual(out["field"]["id"], "f1")
            load.assert_called_once()
            kwargs = load.call_args.kwargs
            self.assertFalse(kwargs.get("allow_http", True))
            session.close.assert_called_once()


class DataReadinessModelTests(unittest.TestCase):
    def test_model_defaults(self) -> None:
        out = fields_mod.DataReadinessOut(field_id="f1")
        self.assertEqual(out.weather_rows, 0)
        self.assertFalse(out.soil_ok)


if __name__ == "__main__":
    unittest.main()
