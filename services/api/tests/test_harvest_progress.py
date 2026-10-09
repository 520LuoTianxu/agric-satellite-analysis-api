"""收获占比 v3：分季、像元粘滞、单调；阈值配置/自适应、S1 佐证、置信度、按日插值；
回执解析与接口过滤。"""

from __future__ import annotations

import asyncio
import random
import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.harvest_progress import (
    HARVEST_PROGRESS_METHOD_VERSION,
    HarvestProgressThresholds,
    compute_harvest_series,
    interpolate_daily,
    load_threshold_profiles,
    match_threshold_profile,
    observation_confidence,
    scene_vegetation_index,
)

THR = HarvestProgressThresholds()
# NDVI 0.15→绿度 0、0.85→绿度 1；返青阈值 0.45≈NDVI 0.465，收获阈值 0.25≈NDVI 0.325。
GREEN, MID, BARE = 0.85, 0.6, 0.15


def _scene(
    day: str,
    ndvis: list[float],
    *,
    clear: list[int] | None = None,
    mndwi: list[float] | None = None,
    dlon: float = 0.0,
    **kw,
):
    pixels = []
    for i, v in enumerate(ndvis):
        p = {"lon": 115.0 + i * 1e-4 + dlon, "lat": 38.9, "NDVI": v, "clear": 1}
        if clear is not None:
            p["clear"] = clear[i]
        if mndwi is not None:
            p["MNDWI"] = mndwi[i]
        pixels.append(p)
    return {"date": day, "pixels": pixels, "scene_id": f"s_{day}", **kw}


def _by_day(rows):
    return {r["date"]: r for r in rows}


def _assert_monotonic_within_seasons(tc: unittest.TestCase, rows) -> None:
    last: dict[str, float] = {}
    for r in rows:
        if r["season_start"] is None:
            tc.assertEqual(r["harvested_pct"], 0.0)
            tc.assertEqual(r["status"], "off_season")
            continue
        prev = last.get(r["season_start"], 0.0)
        tc.assertGreaterEqual(r["harvested_pct"], prev, r)
        tc.assertAlmostEqual(r["newly_harvested_pct"], r["harvested_pct"] - prev, 1)
        last[r["season_start"]] = r["harvested_pct"]


class HarvestSeriesTests(unittest.TestCase):
    def test_method_version(self) -> None:
        self.assertEqual(HARVEST_PROGRESS_METHOD_VERSION, "s2s1_season_monotonic_v3")

    def test_gradual_harvest_rises_0_to_100(self) -> None:
        scenes = [
            _scene("2026-06-01", [BARE] * 10),
            _scene("2026-06-20", [0.5] * 10),
            _scene("2026-07-01", [0.8] * 10),
            _scene("2026-08-01", [GREEN] * 10),
            _scene("2026-09-01", [0.8] * 10),
            _scene("2026-09-10", [0.8] * 8 + [BARE] * 2),
            _scene("2026-09-20", [0.8] * 4 + [BARE] * 6),
            _scene("2026-09-30", [BARE] * 10),
            _scene("2026-10-10", [0.2] * 10),
        ]
        rows = compute_harvest_series(scenes, parcel_area_mu=100, thresholds=THR)
        pct = [r["harvested_pct"] for r in rows]
        self.assertEqual(pct, [0, 0, 0, 0, 0, 20, 60, 100, 100])
        self.assertEqual(
            [r["newly_harvested_pct"] for r in rows], [0, 0, 0, 0, 0, 20, 40, 40, 0]
        )
        d = _by_day(rows)
        self.assertEqual(d["2026-06-01"]["status"], "off_season")
        self.assertEqual(d["2026-06-20"]["season_start"], "2026-06-20")
        self.assertEqual(d["2026-08-01"]["status"], "growing")
        self.assertEqual(d["2026-09-20"]["status"], "harvesting")
        self.assertEqual(d["2026-09-20"]["harvested_area_mu"], 60.0)
        self.assertEqual(d["2026-09-30"]["status"], "harvested")
        self.assertEqual(d["2026-09-30"]["peak_date"], "2026-08-01")
        self.assertTrue(all(r["confirmed"] for r in rows))
        _assert_monotonic_within_seasons(self, rows)

    def test_winter_bare_soil_and_snow_after_previous_harvest_is_off_season(
        self,
    ) -> None:
        # v1 把冬季裸土/休眠（NDVI≤0.3）与上一季峰值比较，判成大面积“已收获”。
        scenes = [
            _scene("2025-07-01", [0.7] * 10),
            _scene("2025-08-01", [GREEN] * 10),
            _scene("2025-09-25", [BARE] * 10),
            _scene("2025-10-05", [BARE] * 10),
            _scene("2025-12-20", [0.3] * 10),
            # 积雪：像元 NDVI 很低但 MNDWI(=NDSI) 高，应剔除。
            _scene("2026-01-15", [0.05] * 10, mndwi=[0.8] * 10),
            _scene("2026-01-26", [0.25] * 10),
            _scene("2026-03-07", [0.24] * 10),
            _scene("2026-03-22", [0.4] * 10),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        d = _by_day(rows)
        self.assertNotIn("2026-01-15", d)  # 雪景有效像元为 0，整天剔除
        self.assertEqual(d["2025-09-25"]["harvested_pct"], 100.0)
        for day in ("2025-12-20", "2026-01-26", "2026-03-07", "2026-03-22"):
            self.assertEqual(d[day]["status"], "off_season", day)
            self.assertEqual(d[day]["harvested_pct"], 0.0, day)
        _assert_monotonic_within_seasons(self, rows)

    def test_single_date_cloud_outlier_is_ignored(self) -> None:
        scenes = [
            _scene("2026-07-01", [0.7] * 10),
            _scene("2026-07-10", [GREEN] * 10),
            # 未被 clear 掩膜识别的云影：整片骤降又恢复 → 凹陷日剔除
            _scene("2026-07-15", [0.1] * 10),
            _scene("2026-07-20", [GREEN] * 10),
            # 局部云影：3 个像元骤降，下一期恢复 → 像元级确认失败，不算收获
            _scene("2026-07-25", [GREEN] * 7 + [0.1] * 3),
            _scene("2026-07-30", [GREEN] * 10),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertNotIn("2026-07-15", _by_day(rows))
        self.assertTrue(all(r["harvested_pct"] == 0 for r in rows))
        self.assertTrue(all(r["status"] == "growing" for r in rows))

    def test_two_seasons_reset_at_green_up(self) -> None:
        scenes = [
            _scene("2026-03-20", [0.5] * 10),  # 冬小麦返青
            _scene("2026-04-20", [GREEN] * 10),
            _scene("2026-05-20", [0.7] * 10),
            _scene("2026-06-05", [0.7] * 5 + [BARE] * 5),
            _scene("2026-06-12", [BARE] * 10),
            _scene("2026-06-25", [0.25] * 10),
            _scene("2026-07-10", [0.55] * 10),  # 夏玉米返青 → 新一季
            _scene("2026-07-20", [0.7] * 10),
            _scene("2026-08-10", [GREEN] * 10),
            _scene("2026-09-20", [0.7] * 6 + [BARE] * 4),
            _scene("2026-09-30", [BARE] * 10),
            _scene("2026-10-08", [BARE] * 10),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        d = _by_day(rows)
        self.assertEqual(d["2026-06-05"]["harvested_pct"], 50.0)
        self.assertEqual(d["2026-06-12"]["harvested_pct"], 100.0)
        self.assertEqual(d["2026-06-25"]["harvested_pct"], 100.0)
        self.assertEqual(d["2026-06-25"]["season_start"], "2026-03-20")
        self.assertEqual(d["2026-07-10"]["season_start"], "2026-07-10")
        self.assertEqual(d["2026-07-10"]["harvested_pct"], 0.0)
        self.assertEqual(d["2026-07-10"]["newly_harvested_pct"], 0.0)
        self.assertEqual(d["2026-09-20"]["harvested_pct"], 40.0)
        self.assertEqual(d["2026-09-20"]["newly_harvested_pct"], 40.0)
        self.assertEqual(d["2026-09-30"]["harvested_pct"], 100.0)
        _assert_monotonic_within_seasons(self, rows)

    def test_fully_cloudy_scene_is_not_treated_as_all_valid(self) -> None:
        # v1 回归：clear 全为 0 时回退到全部像元，云的 NDVI≈0 被当成 100% 已收获。
        scenes = [
            _scene("2026-07-01", [0.7] * 10),
            _scene("2026-08-01", [GREEN] * 10),
            _scene("2026-08-10", [0.02] * 10, clear=[0] * 10),
            _scene("2026-08-20", [GREEN] * 10),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertNotIn("2026-08-10", _by_day(rows))
        self.assertTrue(all(r["harvested_pct"] == 0 for r in rows))

    def test_partially_cloudy_scene_needs_min_valid_pct(self) -> None:
        scenes = [
            _scene("2026-07-01", [0.7] * 4),
            _scene("2026-08-01", [GREEN] * 4),
            _scene("2026-09-01", [BARE] * 4, clear=[1, 0, 0, 0]),
            _scene("2026-09-10", [BARE, BARE, GREEN, 0.05], clear=[1, 1, 1, 0]),
            _scene("2026-09-20", [BARE] * 4),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        d = _by_day(rows)
        self.assertNotIn("2026-09-01", d)
        self.assertEqual(d["2026-09-10"]["valid_pct"], 75.0)
        self.assertEqual(d["2026-09-10"]["harvested_pct"], 50.0)

    def test_decloud_products_are_not_harvest_evidence(self) -> None:
        scenes = [
            _scene("2026-07-01", [0.7] * 4),
            _scene("2026-08-01", [GREEN] * 4),
            {
                **_scene("2026-08-15", [BARE] * 4),
                "scene_id": "x_2026-08-15_S2_decloud",
                "source": "uncrtaints_decloud",
            },
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertNotIn("2026-08-15", _by_day(rows))

    def test_index_selected_by_product_radiometry(self) -> None:
        self.assertEqual(scene_vegetation_index(None, None), "NDVI")
        self.assertEqual(
            scene_vegetation_index(
                "stac-optical-lonlat-v4", "S2A_50SLJ_20260103_0_L2A"
            ),
            "EVI",
        )
        self.assertEqual(
            scene_vegetation_index(
                "stac-optical-lonlat-v4",
                "S2B_MSIL2A_20260715T030519_R075_T50SLJ_20260715T060222",
            ),
            "NDVI",
        )
        self.assertEqual(
            scene_vegetation_index(
                "stac-optical-lonlat-v4", "S2B_T50SLJ_20260715T031456_L2A"
            ),
            "NDVI",
        )

    def test_earth_search_scene_uses_evi_and_saturated_ndvi_is_ignored(self) -> None:
        def es(day: str, ndvi: float, evi: float):
            s = _scene(day, [ndvi] * 10)
            for p in s["pixels"]:
                p["EVI"] = evi
            s["algorithm_version"] = "stac-optical-lonlat-v4"
            s["stac_item_id"] = f"S2A_50SLJ_{day.replace('-', '')}_0_L2A"
            return s

        # 重复扣偏移：茂密作物 NDVI 饱和为 1.0、收获后裸土 NDVI 仍有 0.4；EVI 正常。
        scenes = [
            es("2026-07-01", 0.9, 0.45),
            es("2026-08-01", 1.0, 0.65),
            es("2026-09-25", 0.4, 0.12),
            es("2026-10-05", 0.4, 0.11),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertTrue(all(r["vegetation_index"] == "EVI" for r in rows))
        self.assertEqual(_by_day(rows)["2026-10-05"]["harvested_pct"], 100.0)

    def test_scene_with_implausible_index_values_is_dropped(self) -> None:
        scenes = [
            _scene("2026-07-01", [0.7] * 10),
            _scene("2026-08-01", [GREEN] * 10),
            _scene("2026-08-10", [1.0] * 10),  # NDVI 全部饱和：定标异常
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertNotIn("2026-08-10", _by_day(rows))

    def test_pixels_aligned_across_product_grids(self) -> None:
        # 新旧产品网格相差数米：像元身份需跨网格延续，否则旧网格像元永远“未收获”。
        scenes = [
            _scene("2026-07-01", [0.7] * 10),
            _scene("2026-08-01", [GREEN] * 10),
            _scene("2026-09-01", [GREEN] * 10),
            _scene("2026-09-20", [BARE] * 10, dlon=3e-5),
            _scene("2026-09-30", [BARE] * 10, dlon=3e-5),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        d = _by_day(rows)
        self.assertEqual(d["2026-09-20"]["crop_pixel_count"], 10)
        self.assertEqual(d["2026-09-30"]["harvested_pct"], 100.0)

    def test_latest_unconfirmed_candidates_are_provisional(self) -> None:
        scenes = [
            _scene("2026-07-01", [0.7] * 10),
            _scene("2026-08-01", [GREEN] * 10),
            _scene("2026-09-20", [GREEN] * 7 + [BARE] * 3),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        last = rows[-1]
        self.assertEqual(last["harvested_pct"], 30.0)
        self.assertFalse(last["confirmed"])
        self.assertTrue(rows[-2]["confirmed"])

    def test_random_noise_never_decreases_within_season(self) -> None:
        rng = random.Random(7)
        start = date(2026, 6, 1)
        scenes = []
        for k in range(40):
            day = start + timedelta(days=5 * k)
            base = 0.2 + 0.65 * max(0.0, min(1.0, (k - 3) / 6))
            if k >= 20:
                base = max(BARE, base - 0.1 * (k - 20))
            vals = [min(0.95, max(0.0, base + rng.gauss(0, 0.08))) for _ in range(30)]
            clear = [1 if rng.random() > 0.1 else 0 for _ in range(30)]
            scenes.append(_scene(day.isoformat(), vals, clear=clear))
        rows = compute_harvest_series(scenes, thresholds=THR)
        _assert_monotonic_within_seasons(self, rows)
        self.assertGreater(max(r["harvested_pct"] for r in rows), 90)

    def test_thresholds_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "HARVEST_PROGRESS_HARVEST_GREEN": "0.3",
                "HARVEST_PROGRESS_CONFIRM_DAYS": "1",
                "HARVEST_PROGRESS_PEAK_DROP": "0.6",
            },
        ):
            thr = HarvestProgressThresholds.from_env()
        self.assertEqual(thr.harvest_green, 0.3)
        self.assertEqual(thr.confirm_days, 5)  # 下限保护
        self.assertIn("60%", thr.rule_zh())


# ── v3：阈值来源 / S1 / 置信度 / 插值 ─────────────────────────────────

_SEASON = [
    ("06-01", BARE),
    ("06-20", MID),
    ("07-10", GREEN),
    ("08-01", GREEN),
    ("08-20", GREEN),
    ("09-10", 0.7),
    ("09-20", BARE),
    ("09-30", BARE),
]


def _season_scenes(year: int = 2026, *, last: str | None = None):
    return [
        _scene(f"{year}-{md}", [v] * 10)
        for md, v in _SEASON
        if last is None or md <= last
    ]


def _s1(day: str, vh: float, vv: float, *, orbit: int = 142, version: str = "v6"):
    pixels = [
        {"lon": 115.0 + i * 1e-4, "lat": 38.9, "VH_db": vh, "VV_db": vv}
        for i in range(20)
    ]
    return {
        "date": day,
        "pixels": pixels,
        "relative_orbit": orbit,
        "algorithm_version": version,
    }


_S1_PEAK = [_s1("2026-07-01", -15, -8), _s1("2026-07-13", -15, -8)]


class ThresholdSourceTests(unittest.TestCase):
    def test_profile_priority_and_clamping(self) -> None:
        profiles = load_threshold_profiles(
            '{"Corn": {"harvest_green": 0.2}, "corn@13": {"harvest_green": 0.22},'
            ' "@21": {"harvest_window_days": 75}, "bad": 3}'
        )
        self.assertNotIn("bad", profiles)
        self.assertEqual(match_threshold_profile(profiles, "corn", "13")[0], "corn@13")
        self.assertEqual(match_threshold_profile(profiles, "CORN", "11")[0], "corn")
        self.assertEqual(match_threshold_profile(profiles, None, "21")[0], "@21")
        self.assertIsNone(match_threshold_profile(profiles, "wheat", "11"))
        thr = THR.with_overrides({"harvest_green": 5, "confirm_days": 7.6, "x": 1})
        self.assertEqual(thr.harvest_green, 0.9)
        self.assertEqual(thr.confirm_days, 7)
        self.assertEqual(load_threshold_profiles("not json"), {})

    def test_profile_beats_adaptive(self) -> None:
        rows = compute_harvest_series(
            _season_scenes(),
            thresholds=THR,
            crop_type="corn",
            region_code="13",
            profiles={"corn@13": {"harvest_green": 0.2}},
        )
        self.assertEqual(rows[-1]["threshold_source"], "profile:corn@13")
        self.assertEqual(rows[-1]["thresholds"]["harvest_green"], 0.2)

    def test_single_season_falls_back_to_default(self) -> None:
        rows = compute_harvest_series(_season_scenes(), thresholds=THR, profiles={})
        self.assertEqual(rows[-1]["threshold_source"], "default")
        self.assertEqual(rows[-1]["thresholds"]["harvest_green"], THR.harvest_green)

    def test_multi_season_history_gives_bounded_adaptive_thresholds(self) -> None:
        scenes = [s for y in (2024, 2025, 2026) for s in _season_scenes(y)]
        rows = compute_harvest_series(scenes, thresholds=THR, profiles={})
        t = rows[-1]["thresholds"]
        self.assertEqual(rows[-1]["threshold_source"], "adaptive")
        self.assertEqual(t["adaptive"]["seasons"], 3)
        self.assertTrue(0.40 <= t["season_green"] <= 0.50)
        self.assertTrue(0.20 <= t["harvest_green"] <= 0.30)
        self.assertTrue(t["harvest_green"] < t["confirm_green"] < t["season_green"])
        # 结果仍是每季 0→100%。
        by = _by_day(rows)
        for y in (2024, 2025, 2026):
            self.assertEqual(by[f"{y}-09-30"]["harvested_pct"], 100.0)
        _assert_monotonic_within_seasons(self, rows)

    def test_adaptive_can_be_disabled(self) -> None:
        scenes = [s for y in (2024, 2025, 2026) for s in _season_scenes(y)]
        rows = compute_harvest_series(
            scenes, thresholds=THR, profiles={}, adaptive=False
        )
        self.assertEqual(rows[-1]["threshold_source"], "default")

    def test_fixed_earth_search_products_use_ndvi(self) -> None:
        self.assertEqual(
            scene_vegetation_index(
                "stac-optical-lonlat-v5", "S2A_50SLJ_20261003_0_L2A"
            ),
            "NDVI",
        )


class Sentinel1Tests(unittest.TestCase):
    def test_s1_drop_agrees_and_confirms_latest_candidates(self) -> None:
        s1 = [*_S1_PEAK, _s1("2026-09-22", -16, -7)]
        rows = compute_harvest_series(
            _season_scenes(last="09-20"), thresholds=THR, s1_scenes=s1, profiles={}
        )
        last = rows[-1]
        self.assertEqual(last["date"], "2026-09-20")
        self.assertEqual(last["harvested_pct"], 100.0)
        self.assertEqual(last["s1_agreement"], "agree")
        self.assertEqual(last["s1_date"], "2026-09-22")
        self.assertAlmostEqual(last["s1_delta_ratio_db"], -2.0)
        self.assertTrue(last["confirmed"])
        self.assertEqual(last["confirmed_by"], "s1")
        self.assertIn("s1_confirmed", last["confidence_reasons"])

    def test_without_s1_latest_candidates_stay_provisional_and_capped(self) -> None:
        rows = compute_harvest_series(
            _season_scenes(last="09-20"), thresholds=THR, profiles={}
        )
        last = rows[-1]
        self.assertFalse(last["confirmed"])
        self.assertIsNone(last["confirmed_by"])
        self.assertIn("unconfirmed", last["confidence_reasons"])
        self.assertLessEqual(last["confidence"], 0.74)
        self.assertEqual(last["confidence_level"], "medium")
        # S1 不改变占比。
        with_s1 = compute_harvest_series(
            _season_scenes(last="09-20"),
            thresholds=THR,
            s1_scenes=[*_S1_PEAK, _s1("2026-09-22", -16, -7)],
            profiles={},
        )
        self.assertEqual(
            [r["harvested_pct"] for r in rows], [r["harvested_pct"] for r in with_s1]
        )

    def test_s1_still_vegetated_disagrees(self) -> None:
        s1 = [*_S1_PEAK, _s1("2026-09-22", -15, -8), _s1("2026-10-01", -15, -8)]
        rows = _by_day(
            compute_harvest_series(
                _season_scenes(), thresholds=THR, s1_scenes=s1, profiles={}
            )
        )
        r = rows["2026-09-20"]
        self.assertEqual(r["s1_agreement"], "disagree")
        self.assertIn("s1_disagree", r["confidence_reasons"])
        self.assertLessEqual(r["confidence"], 0.74)

    def test_s1_not_compared_across_orbits_versions_or_before_peak(self) -> None:
        s1 = [
            *_S1_PEAK,
            _s1("2026-09-22", -25, -20, orbit=40),
            _s1("2026-09-23", -25, -20, version="legacy"),
            _s1("2026-06-21", -25, -10),
        ]
        rows = compute_harvest_series(
            _season_scenes(), thresholds=THR, s1_scenes=s1, profiles={}
        )
        self.assertTrue(all(r["s1_agreement"] is None for r in rows))


class ConfidenceTests(unittest.TestCase):
    def test_ideal_observation_is_high(self) -> None:
        score, level, reasons = observation_confidence(
            valid_pct=100,
            valid_pixel_count=500,
            gap_days=5,
            margin=0.2,
            pending_fraction=0,
        )
        self.assertEqual((score, level, reasons), (1.0, "high", []))

    def test_each_factor_lowers_score_with_reason(self) -> None:
        base = dict(
            valid_pct=100,
            valid_pixel_count=500,
            gap_days=5,
            margin=0.2,
            pending_fraction=0.0,
        )
        cases = {
            "low_valid_pct": dict(valid_pct=55),
            "few_pixels": dict(valid_pixel_count=10),
            "long_gap": dict(gap_days=40),
            "small_margin": dict(margin=0.0),
            "unconfirmed": dict(pending_fraction=1.0),
        }
        for reason, change in cases.items():
            score, _, reasons = observation_confidence(**{**base, **change})
            self.assertLess(score, 1.0, reason)
            self.assertIn(reason, reasons)
        score, level, _ = observation_confidence(**{**base, "pending_fraction": 1.0})
        self.assertEqual(level, "medium")
        score, level, reasons = observation_confidence(
            valid_pct=52,
            valid_pixel_count=20,
            gap_days=45,
            margin=-0.05,
            pending_fraction=1.0,
            s1_agreement="disagree",
        )
        self.assertEqual(level, "low")
        self.assertIn("s1_disagree", reasons)

    def test_s1_agreement_only_counts_when_available(self) -> None:
        kw = dict(valid_pct=60, valid_pixel_count=500, gap_days=5, margin=0.1)
        none, _, _ = observation_confidence(pending_fraction=0, **kw)
        agree, _, r = observation_confidence(
            pending_fraction=0, s1_agreement="agree", **kw
        )
        amb, _, _ = observation_confidence(
            pending_fraction=0, s1_agreement="ambiguous", **kw
        )
        self.assertGreater(agree, none)
        self.assertEqual(amb, none)
        self.assertIn("s1_agree", r)

    def test_series_rows_carry_confidence(self) -> None:
        rows = compute_harvest_series(_season_scenes(), thresholds=THR, profiles={})
        for r in rows:
            self.assertTrue(0.0 <= r["confidence"] <= 1.0)
            self.assertIn(r["confidence_level"], {"high", "medium", "low"})
            self.assertIsInstance(r["confidence_reasons"], list)
        self.assertIsNone(rows[0]["gap_days"])
        self.assertEqual(_by_day(rows)["2026-06-20"]["gap_days"], 19)
        self.assertIn("long_gap", _by_day(rows)["2026-06-20"]["confidence_reasons"])


class InterpolationTests(unittest.TestCase):
    ROWS = [
        {"date": "2026-08-30", "status": "off_season", "harvested_pct": 0.0,
         "season_start": None, "confidence": 0.9},
        {"date": "2026-09-01", "status": "growing", "harvested_pct": 0.0,
         "season_start": "2026-06-25", "confidence": 0.9, "parcel_area_mu": 100.0},
        {"date": "2026-09-05", "status": "harvesting", "harvested_pct": 40.0,
         "season_start": "2026-06-25", "confidence": 0.8, "parcel_area_mu": 100.0},
        {"date": "2026-09-08", "status": "harvesting", "harvested_pct": 41.0,
         "season_start": "2026-06-25", "confidence": 0.6, "confirmed": False,
         "parcel_area_mu": 100.0},
        {"date": "2026-09-20", "status": "growing", "harvested_pct": 0.0,
         "season_start": "2027-01-01", "confidence": 0.9},
    ]  # fmt: skip

    def test_daily_linear_monotonic_flagged(self) -> None:
        out = interpolate_daily(self.ROWS)
        by = _by_day(out)
        # 观测行原样保留。
        for r in self.ROWS:
            self.assertFalse(by[r["date"]]["interpolated"])
            self.assertEqual(by[r["date"]]["harvested_pct"], r["harvested_pct"])
        self.assertEqual(
            [by[f"2026-09-0{d}"]["harvested_pct"] for d in (2, 3, 4)],
            [10.0, 20.0, 30.0],
        )
        mid = by["2026-09-03"]
        self.assertTrue(mid["interpolated"])
        self.assertEqual(mid["confidence"], round(0.7 * 0.8, 3))
        self.assertEqual(mid["confidence_reasons"], ["interpolated"])
        self.assertEqual(mid["harvested_area_mu"], 20.0)
        self.assertEqual(mid["newly_harvested_pct"], 10.0)
        self.assertIn("unconfirmed", by["2026-09-07"]["confidence_reasons"])
        # 季外/跨季不插值，最后一期之后不外推。
        self.assertNotIn("2026-08-31", by)
        self.assertNotIn("2026-09-10", by)
        self.assertNotIn("2026-09-21", by)
        dates = [r["date"] for r in out]
        self.assertEqual(dates, sorted(dates))
        season = [
            r["harvested_pct"] for r in out if r.get("season_start") == "2026-06-25"
        ]
        self.assertEqual(season, sorted(season))


class SceneResultTargetTests(unittest.TestCase):
    def test_extracts_s2_land_and_date(self) -> None:
        from app.services.harvest_progress import harvest_target_from_scene_result

        env = {"extras": {"land_id": "L1", "date": "2026-09-30", "sensor": "S2"}}
        self.assertEqual(
            harvest_target_from_scene_result(env, {"scene_upserts": 1}),
            ("L1", date(2026, 9, 30)),
        )
        # S1 入库也触发重算（S1 佐证会改变置信度/确认）。
        self.assertEqual(
            harvest_target_from_scene_result(
                {"extras": {"land_id": "L1", "sensor": "S1"}}, {"scene_upserts": 1}
            ),
            ("L1", None),
        )
        self.assertIsNone(
            harvest_target_from_scene_result(
                {"extras": {"land_id": "L1", "sensor": "S3"}}, {"scene_upserts": 1}
            )
        )
        self.assertIsNone(harvest_target_from_scene_result(env, {"scene_upserts": 0}))
        self.assertEqual(
            harvest_target_from_scene_result(
                {"apply": {"result": {"land_id": "L2"}}},
                {"apply": {"oss": {"scene_upserts": 2}}},
            ),
            ("L2", None),
        )


class HarvestProgressEndpointTests(unittest.TestCase):
    def _call(
        self, rows, *, include_zero: bool, stored: bool = True, interpolate="none"
    ):
        from app.routers import agri

        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        with (
            patch.object(agri, "_agri_ready", AsyncMock()),
            patch(
                "app.services.harvest_progress.load_land_area_mu",
                AsyncMock(return_value=(True, 30.0)),
            ),
            patch(
                "app.services.harvest_progress.list_stored",
                AsyncMock(return_value=rows if stored else []),
            ),
            patch(
                "app.services.harvest_progress.compute_land_series",
                AsyncMock(return_value=rows),
            ),
            patch("app.services.harvest_progress.enqueue_lands", AsyncMock()) as enq,
        ):
            out = asyncio.run(
                agri.get_harvest_progress(
                    "L1",
                    MagicMock(),
                    db,
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 10, 9),
                    include_zero=include_zero,
                    interpolate=interpolate,
                )
            )
        return out, enq

    ROWS = [
        {
            "date": "2026-08-01",
            "harvested_pct": 0.0,
            "newly_harvested_pct": 0.0,
            "harvested_area_mu": 0.0,
            "status": "growing",
            "valid_pct": 100.0,
            "season_start": "2026-06-25",
            "vegetation_index": "EVI",
        },
        {
            "date": "2026-09-20",
            "harvested_pct": 40.0,
            "newly_harvested_pct": 40.0,
            "harvested_area_mu": 12.0,
            "status": "harvesting",
            "valid_pct": 90.0,
            "season_start": "2026-06-25",
            "vegetation_index": "EVI",
            "confirmed": False,
        },
    ]

    def test_excludes_zero_by_default(self) -> None:
        out, _ = self._call(self.ROWS, include_zero=False)
        self.assertEqual([str(i.date) for i in out.items], ["2026-09-20"])
        self.assertEqual(out.source, "stored")
        self.assertTrue(out.heuristic)
        self.assertEqual(out.parcel_area_mu, 30.0)
        self.assertEqual(out.method_version, "s2s1_season_monotonic_v3")
        self.assertEqual(str(out.items[0].season_start), "2026-06-25")
        self.assertFalse(out.items[0].confirmed)

    def test_include_zero_and_live_fallback_enqueues(self) -> None:
        out, enq = self._call(self.ROWS, include_zero=True, stored=False)
        self.assertEqual(len(out.items), 2)
        self.assertEqual(out.source, "live")
        enq.assert_awaited_once()

    def test_interpolate_daily(self) -> None:
        out, _ = self._call(self.ROWS, include_zero=True, interpolate="daily")
        self.assertEqual(out.interpolate, "daily")
        interp = [i for i in out.items if i.interpolated]
        self.assertEqual(len(interp), 49)
        self.assertTrue(all(i.valid_pct is None for i in interp))
        pcts = [i.harvested_pct for i in out.items]
        self.assertEqual(pcts, sorted(pcts))
        out, _ = self._call(self.ROWS, include_zero=True)
        self.assertFalse(any(i.interpolated for i in out.items))


if __name__ == "__main__":
    unittest.main()
