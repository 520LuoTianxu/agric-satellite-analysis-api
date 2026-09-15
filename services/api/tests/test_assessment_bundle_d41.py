"""Unit tests for D4.1 assessment-bundle / data-readiness helpers."""

from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_data_readiness_binds_native_dates(self) -> None:
        field = MagicMock(tags_json=["agri:LAND1"])
        field_result = MagicMock()
        field_result.scalar_one_or_none.return_value = field

        weather_result = MagicMock()
        weather_result.scalar.return_value = 31
        soil_result = MagicMock()
        soil_result.scalar.return_value = 1
        coverage_result = MagicMock()
        coverage_result.mappings.return_value.first.return_value = {
            "s2_dates": 3,
            "s1_dates": 2,
        }

        db = MagicMock()
        db.execute = AsyncMock(
            side_effect=[field_result, weather_result, soil_result, coverage_result]
        )

        async def call() -> fields_mod.DataReadinessOut:
            return await fields_mod.data_readiness(
                str(uuid.uuid4()),
                None,
                db,
                date_from=date(2024, 1, 1),
                date_to=date(2024, 1, 31),
            )

        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(call())
        finally:
            loop.close()

        self.assertEqual(out.span_days, 31)
        weather_params = db.execute.call_args_list[1].args[1]
        self.assertIsInstance(weather_params["d0"], date)
        self.assertIsInstance(weather_params["d1"], date)
        coverage_params = db.execute.call_args_list[3].args[1]
        self.assertEqual(coverage_params["d0"], date(2024, 1, 1))
        self.assertEqual(coverage_params["d1"], date(2024, 1, 31))


if __name__ == "__main__":
    unittest.main()
