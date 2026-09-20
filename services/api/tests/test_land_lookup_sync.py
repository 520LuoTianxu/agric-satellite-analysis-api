"""地块详情查询缺失时的 Smart 同步兜底测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.routers.lands import _get_land_or_sync


def _land(land_id: str = "25831") -> SimpleNamespace:
    return SimpleNamespace(land_id=land_id, deleted_at=None)


@pytest.mark.asyncio
async def test_missing_land_is_synced_from_smart_and_read_again() -> None:
    db = AsyncMock()
    db.get = AsyncMock(side_effect=[None, _land()])
    sync = AsyncMock(return_value={"status": "completed", "synced_land_ids": ["25831"]})

    with patch("app.routers.lands.sync_selected_lands", sync):
        result = await _get_land_or_sync("25831", db)

    assert result.land_id == "25831"
    sync.assert_awaited_once_with(["25831"])
    db.rollback.assert_awaited_once()
    assert db.get.await_count == 2


@pytest.mark.asyncio
async def test_existing_land_does_not_trigger_smart_sync() -> None:
    db = AsyncMock()
    db.get = AsyncMock(return_value=_land())
    sync = AsyncMock()

    with patch("app.routers.lands.sync_selected_lands", sync):
        result = await _get_land_or_sync("25831", db)

    assert result.land_id == "25831"
    sync.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_land_still_returns_not_found_when_smart_has_no_row() -> None:
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    sync = AsyncMock(return_value={"status": "not_found", "missing_land_ids": ["25831"]})

    with patch("app.routers.lands.sync_selected_lands", sync):
        with pytest.raises(HTTPException) as exc_info:
            await _get_land_or_sync("25831", db)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Land parcel not found in Smart source"


@pytest.mark.asyncio
async def test_smart_sync_failure_is_not_reported_as_land_not_found() -> None:
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    sync = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch("app.routers.lands.sync_selected_lands", sync):
        with pytest.raises(HTTPException) as exc_info:
            await _get_land_or_sync("25831", db)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Smart 地块数据同步失败"
