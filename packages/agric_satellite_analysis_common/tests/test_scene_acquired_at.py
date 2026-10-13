from datetime import datetime, timezone
from unittest import mock

from agric_satellite_analysis_common import result_apply
from agric_satellite_analysis_common.scene_time import (
    normalize_acquired_at,
    stac_item_acquired_at,
)


def test_normalize_stac_string_keeps_millis_utc():
    assert (
        normalize_acquired_at("2026-10-12T02:55:08.329Z")
        == "2026-10-12T02:55:08.329+00:00"
    )


def test_normalize_naive_and_offset_datetime():
    assert normalize_acquired_at(datetime(2026, 10, 12, 2, 55, 8)) == (
        "2026-10-12T02:55:08.000+00:00"
    )
    assert normalize_acquired_at("2026-10-12T10:55:08+08:00") == (
        "2026-10-12T02:55:08.000+00:00"
    )


def test_normalize_rejects_date_only_and_garbage():
    for v in (None, "", "2026-10-12", "not-a-date", 123):
        assert normalize_acquired_at(v) is None


def test_stac_item_acquired_at_from_item_and_dict():
    item = mock.Mock()
    item.datetime = datetime(2026, 10, 12, 2, 55, 8, 329000, tzinfo=timezone.utc)
    item.properties = {}
    assert stac_item_acquired_at(item) == "2026-10-12T02:55:08.329+00:00"
    d = {"properties": {"datetime": None, "start_datetime": "2026-10-12T02:55:08Z"}}
    assert stac_item_acquired_at(d) == "2026-10-12T02:55:08.000+00:00"
    assert stac_item_acquired_at(None) is None


def test_upsert_sql_writes_acquired_at_on_insert_and_update():
    sql = result_apply.UPSERT_SCENE_SQL
    assert "acquired_at," in sql
    assert "CAST(%(acquired_at)s AS timestamptz)" in sql
    assert (
        "acquired_at = COALESCE(EXCLUDED.acquired_at, "
        "agric_satellite.parcel_scene_products.acquired_at)" in sql
    )


def _apply(obj):
    session = mock.Mock()
    conn = session.connection.return_value
    with mock.patch.object(result_apply, "SyncSession", return_value=session):
        result_apply.apply_parcel_scene_product(obj)
    return conn.exec_driver_sql.call_args[0][1]


def _base():
    return {
        "land_id": "28800",
        "date": "2026-10-12",
        "sensor": "S2",
        "scene_id": "S2C_51TVL_20261012_0_L2A",
        "pixel_data": {"format": "lonlat_v1", "pixels": []},
    }


def test_apply_new_message_passes_acquired_at():
    obj = _base() | {"acquired_at": "2026-10-12T02:55:08.329Z"}
    assert _apply(obj)["acquired_at"] == "2026-10-12T02:55:08.329+00:00"


def test_apply_legacy_message_without_field_writes_null():
    assert _apply(_base())["acquired_at"] is None
