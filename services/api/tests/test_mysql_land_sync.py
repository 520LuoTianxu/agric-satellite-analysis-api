from datetime import date, datetime, timezone

import pytest

from app.services.mysql_land_sync import (
    next_sync_at,
    normalize_source_row,
    parse_wgs_land_path,
    subtract_months,
)


def _row(**overrides):
    row = {
        "farms_id": "1001",
        "farms_name": "测试项目",
        "base_id": "12",
        "base_name": "测试租户",
        "province_code": "43",
        "province_name": "湖南省",
        "city_code": "4301",
        "city_name": "长沙市",
        "county_code": "4301",
        "county_name": "岳麓区",
        "town_code": "4301",
        "town_name": "测试镇",
        "village_code": "4301",
        "village_name": "测试村",
        "org_code": "ORG",
        "org_name": "测试组织",
        "group_id": "1001",
        "group_name": "测试项目",
        "land_id": "2001",
        "land_name": "一号地块",
        "land_area": "10.50",
        "planting_type": "轮作",
        "business_category": "联营",
        "source_status": "0",
        "wgs_land_path": "112.8709,28.2330|112.8708,28.2327|112.8712,28.2326",
    }
    row.update(overrides)
    return row


def test_parse_confirmed_pipe_coordinate_format():
    geometry = parse_wgs_land_path(_row()["wgs_land_path"])
    assert geometry.geom_type == "MultiPolygon"
    assert len(geometry.geoms) == 1
    assert geometry.bounds[0] == pytest.approx(112.8708)


def test_invalid_coordinate_is_rejected():
    with pytest.raises(ValueError, match="invalid coordinate"):
        parse_wgs_land_path("112.8|bad")


def test_planting_type_stays_in_source_properties():
    parcel = normalize_source_row(_row())
    assert parcel.land_area_mu == pytest.approx(10.5)
    assert parcel.source_properties["planting_type"] == "轮作"
    assert parcel.source_properties["business_category"] == "联营"
    assert "crop_type" not in parcel.source_properties


def test_subtract_months_uses_calendar_months():
    assert subtract_months(date(2024, 3, 31), 1) == date(2024, 2, 29)
    assert subtract_months(date(2026, 9, 17), 24) == date(2024, 9, 17)


def test_next_sync_at_is_beijing_22_00():
    now = datetime(2026, 9, 17, 13, 0, tzinfo=timezone.utc)
    assert next_sync_at(now) == datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)


def test_base_name_is_normalized_and_hashed():
    parcel = normalize_source_row(_row())
    assert parcel.base_name == "测试租户"
    assert normalize_source_row(_row(base_name="  ")).base_name is None
    assert normalize_source_row(_row(base_name="新租户")).sync_hash != parcel.sync_hash


def test_source_sql_reads_base_name_from_baseinfo():
    from app.services.mysql_land_sync import SOURCE_SQL, _land_values, _selected_source_query

    assert "agriculture_baseinfo" in SOURCE_SQL.text
    query, _ = _selected_source_query(["2001"])
    assert "AS base_name" in query.text
    values = _land_values(normalize_source_row(_row()), None, datetime.now(timezone.utc))
    assert values["base_name"] == "测试租户"
