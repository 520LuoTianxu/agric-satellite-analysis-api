from datetime import date, datetime, timezone

from app.routers.agri import _SCENE_COLS
from app.schemas.agri import SceneProductOut


def _base():
    return {
        "land_id": "28800",
        "tile_id": "51TVL",
        "date": date(2026, 10, 12),
        "sensor": "S2",
        "scene_id": "S2C_51TVL_20261012_0_L2A",
    }


def test_scene_cols_select_acquired_at():
    assert "acquired_at" in _SCENE_COLS


def test_scene_out_serializes_acquired_at_iso_with_tz():
    out = SceneProductOut.model_validate(
        _base()
        | {"acquired_at": datetime(2026, 10, 12, 2, 55, 8, 329000, tzinfo=timezone.utc)}
    )
    dumped = out.model_dump(mode="json")
    assert dumped["acquired_at"] == "2026-10-12T02:55:08.329000Z"
    assert dumped["date"] == "2026-10-12"


def test_scene_out_acquired_at_optional():
    assert SceneProductOut.model_validate(_base()).acquired_at is None
