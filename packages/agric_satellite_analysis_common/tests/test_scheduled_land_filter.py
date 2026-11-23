from agric_satellite_analysis_common.scheduled_land_filter import (
    MAX_SCHEDULE_LAND_AREA_MU,
    is_scheduled_land_allowed,
    scheduled_land_sql,
    should_bypass_schedule_land_filter,
)

# Previously hardcoded schedule exclusions, including SQL-only base 46.
FORMER_EXCLUDED_BASE_IDS = ("4", "6", "10", "17", "28", "33", "52", "62", "46")


def test_former_excluded_bases_and_base_46_are_allowed_within_area_limit():
    for base_id in FORMER_EXCLUDED_BASE_IDS:
        assert is_scheduled_land_allowed(base_id, 1)
        assert is_scheduled_land_allowed(int(base_id), None)
        assert is_scheduled_land_allowed(base_id, MAX_SCHEDULE_LAND_AREA_MU)


def test_oversized_land_is_not_allowed_regardless_of_base():
    assert is_scheduled_land_allowed("99", MAX_SCHEDULE_LAND_AREA_MU)
    assert not is_scheduled_land_allowed("99", MAX_SCHEDULE_LAND_AREA_MU + 0.01)
    for base_id in FORMER_EXCLUDED_BASE_IDS:
        assert not is_scheduled_land_allowed(base_id, MAX_SCHEDULE_LAND_AREA_MU + 0.01)


def test_unknown_area_is_left_to_existing_validation():
    assert is_scheduled_land_allowed("99", None)
    assert is_scheduled_land_allowed("99", "not-a-number")
    assert is_scheduled_land_allowed("46", "not-a-number")


def test_sql_filter_is_area_only():
    sql = scheduled_land_sql("p")
    assert "p.land_area_mu" in sql
    assert ":max_schedule_area_mu" in sql
    assert "base_id" not in sql
    assert "NOT IN" not in sql


def test_user_triggered_biweekly_flag_bypasses_filter_decision():
    assert not should_bypass_schedule_land_filter(None)
    assert not should_bypass_schedule_land_filter({})
    assert not should_bypass_schedule_land_filter({"force": True})
    assert should_bypass_schedule_land_filter({"bypass_schedule_land_filter": True})
    assert should_bypass_schedule_land_filter({"allow_excluded_bases": True})
    # The flag only decides whether a job skips the remaining area rule.
    assert is_scheduled_land_allowed("10", 105.41)
    assert not is_scheduled_land_allowed("10", MAX_SCHEDULE_LAND_AREA_MU + 1)
