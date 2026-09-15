"""Unit tests for canonical land assessment-bundle / data-readiness helpers."""

from __future__ import annotations

import asyncio
import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from app.routers import internal_lands as lands_mod


def test_sync_loader_uses_land_id_without_translation() -> None:
    fake_bundle = {"land": {"land_id": "L1"}, "indices": []}
    # 该同步辅助函数会在调用时导入数据库模块；测试不应依赖本机的生产 DATABASE_URL。
    with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}, clear=False):
        with (
            patch("app.core.database_sync.SyncSession") as sess_cls,
            patch(
                "app.reports.land_assessment.data_loader.load_land_bundle",
                return_value=fake_bundle,
            ) as load,
        ):
            session = MagicMock()
            sess_cls.return_value = session
            out = lands_mod._sync_load_assessment_bundle("L1")

    assert out["land"]["land_id"] == "L1"
    load.assert_called_once_with(session, "L1", allow_http=False)
    session.close.assert_called_once()


def test_data_readiness_queries_direct_land_id() -> None:
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
    db.get = AsyncMock(return_value=MagicMock())
    db.execute = AsyncMock(side_effect=[weather_result, soil_result, coverage_result])

    async def call() -> lands_mod.DataReadinessOut:
        return await lands_mod.data_readiness(
            "L1",
            None,
            db,
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 31),
        )

    out = asyncio.run(call())

    assert out.land_id == "L1"
    assert out.span_days == 31
    weather_params = db.execute.call_args_list[0].args[1]
    assert weather_params["land_id"] == "L1"
    assert isinstance(weather_params["date_from"], date)
    coverage_params = db.execute.call_args_list[2].args[1]
    assert coverage_params["land_id"] == "L1"
    assert coverage_params["date_to"] == date(2024, 1, 31)
