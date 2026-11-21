from agric_satellite_analysis_common.scheduled_land_filter import (
    MAX_SCHEDULE_LAND_AREA_MU,
    is_scheduled_land_allowed,
    scheduled_land_sql,
    should_bypass_schedule_land_filter,
)


def test_excluded_base_is_not_allowed():
    assert not is_scheduled_land_allowed("4", 1)
    assert not is_scheduled_land_allowed(62, None)


def test_oversized_land_is_not_allowed():
    assert is_scheduled_land_allowed("99", MAX_SCHEDULE_LAND_AREA_MU)
    assert not is_scheduled_land_allowed("99", MAX_SCHEDULE_LAND_AREA_MU + 0.01)


def test_unknown_area_is_left_to_existing_validation():
    assert is_scheduled_land_allowed("99", None)
    assert is_scheduled_land_allowed("99", "not-a-number")


def test_sql_filter_contains_both_schedule_rules():
    sql = scheduled_land_sql("p")
    assert "p.base_id" in sql
    assert "p.land_area_mu" in sql
    assert ":max_schedule_area_mu" in sql

def test_user_triggered_biweekly_flag_bypasses_filter_decision():
    assert not should_bypass_schedule_land_filter(None)
    assert not should_bypass_schedule_land_filter({})
    assert not should_bypass_schedule_land_filter({"force": True})
    assert should_bypass_schedule_land_filter({"bypass_schedule_land_filter": True})
    assert should_bypass_schedule_land_filter({"allow_excluded_bases": True})
    # Scheduled jobs without the flag stay subject to the exclusion list.
    assert not is_scheduled_land_allowed("10", 105.41)
