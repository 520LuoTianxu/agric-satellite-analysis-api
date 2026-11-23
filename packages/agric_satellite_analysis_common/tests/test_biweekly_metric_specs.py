"""Biweekly report metric definitions and growth-phase metric sets."""

from __future__ import annotations

from agric_satellite_analysis_common.biweekly.metric_specs import (
    METRIC_SPECS,
    REPORT_METRIC_ORDER,
    metric_label,
)
from agric_satellite_analysis_common.biweekly.phase_policy import (
    PHASE_FORMAL_NAMES,
    PHASE_LABELS,
    PHASE_METRICS,
    PLAIN_NAMES,
    display_phase,
    phase_metric_profile,
)


def test_metric_names_are_formal_with_one_sentence_purpose():
    assert metric_label("ndvi") == "归一化植被指数（NDVI）"
    assert metric_label("evi") == "增强型植被指数（EVI）"
    assert metric_label("ndre") == "归一化差值红边指数（NDRE）"
    assert metric_label("ndmi") == "归一化差值水分指数（NDMI）"
    for key in REPORT_METRIC_ORDER:
        spec = METRIC_SPECS[key]
        # 用途说明只有一句，句号由排版时补上。
        assert spec.purpose and "。" not in spec.purpose
        assert spec.formula
        # 兼容旧键名，但取值不再是「绿度」「叶片营养」这类替代词。
        assert PLAIN_NAMES[key] == spec.label
        assert "绿度" not in PLAIN_NAMES[key]
        assert "营养" not in PLAIN_NAMES[key]


def test_display_ranges_follow_each_index_definition():
    ndvi = METRIC_SPECS["ndvi"]
    assert ndvi.valid_range == (-1.0, 1.0)
    assert ndvi.display_range == (0.0, 1.0)
    assert ndvi.low_reference == 0.3
    # EVI 的数学取值不以 1 为上限；NDMI 以 0 为分界。
    assert METRIC_SPECS["evi"].valid_range[1] > 1.0
    assert METRIC_SPECS["evi"].display_range == (0.0, 1.2)
    assert METRIC_SPECS["ndre"].display_range == (0.0, 0.8)
    ndmi = METRIC_SPECS["ndmi"]
    assert ndmi.scale_kind == "diverging"
    assert ndmi.display_range == (-0.5, 0.5)
    assert ndmi.low_reference == 0.0
    for spec in METRIC_SPECS.values():
        assert spec.unit == "无量纲"


def test_growth_phases_use_different_metric_sets():
    assert set(PHASE_METRICS) == {"S1", "S2", "S3"}
    primaries = {label: tuple(phase_metric_profile(label)["primary"]) for label in PHASE_METRICS}
    assert primaries == {"S1": ("ndvi",), "S2": ("ndre", "evi"), "S3": ("ndre", "ndmi")}
    assert len(set(primaries.values())) == 3
    for label in PHASE_METRICS:
        profile = phase_metric_profile(label)
        assert profile["formal_name"] == PHASE_FORMAL_NAMES[label]
        assert profile["rationale"]
        assert not set(profile["primary"]) & set(profile["supporting"])
    # 接口契约里的阶段短名不变。
    assert set(PHASE_LABELS) == {"S1", "S2", "S3"}
    assert phase_metric_profile(None)["phase_label"] is None


def test_display_phase_reports_formal_name_and_prior_stages():
    intervals = [
        {"stage_code": "GR", "start": "2026-03-01", "end_exclusive": "2026-04-04"},
        {"stage_code": "JT", "start": "2026-04-04", "end_exclusive": "2026-04-16"},
        {"stage_code": "HA", "start": "2026-04-16", "end_exclusive": "2026-05-02"},
    ]
    result = display_phase(
        planned_intervals=intervals,
        observation_dates=["2026-04-06", "2026-04-11"],
        window_start="2026-04-04",
        end_exclusive="2026-04-18",
        prior_observation_dates=["2026-03-27"],
    )
    assert result["display_phase"] == "S2"
    assert result["phase_formal_name"] == "营养生长与生殖生长并进阶段"
    assert result["primary_metrics"] == ["ndre", "evi"]
    assert result["phase_metrics"]["S2"]["primary"] == ["ndre", "evi"]
    assert [row["phase_label"] for row in result["prior_observation_stages"]] == ["S1"]
    assert result["prior_observation_stages"][0]["date"] == "2026-03-27"
    assert result["next_week"]["phase_formal_name"] == "生殖生长阶段"
    background = result["stage_background"]
    assert set(background) >= {"拔节孕穗期"}
    assert background["拔节孕穗期"]["trend"] in {"low", "rise", "stable", "decline"}
