"""Unit tests for season-growth facts builder (synthetic series, no DB)."""

from __future__ import annotations

import unittest
from datetime import date

from app.reports.season_growth.facts import (
    FOOTER_DISCLAIMER,
    _build_s1_appendix,
    _build_s2_appendix,
    _build_timeline,
    _drought_summary,
    _flood_summary,
    _methodology,
    _peak,
    _series_mean,
    compute_confidence,
    drought_class_cn,
    facts_for_llm,
    flood_class_cn,
    phenology_stage_estimate,
    program_core_conclusion,
    program_next_season_actions,
    looks_like_remote_ops_advice,
    quality_cn,
    compute_growth_grade_shares,
    classify_growth_grade,
    _pick_rgb_scene,
    build_spatial_block,
)


def _s2_series():
    rows = []
    for d, ndvi, ndmi, official in [
        ("2026-06-05", 0.32, 0.18, True),
        ("2026-06-20", 0.45, 0.20, True),
        ("2026-07-05", 0.58, 0.22, True),
        ("2026-07-20", 0.66, 0.15, True),
        ("2026-08-05", 0.70, 0.05, True),
        ("2026-08-20", 0.55, -0.05, True),
        ("2026-09-05", 0.40, 0.02, True),
    ]:
        rows.append(
            {
                "date": d,
                "scene_id": f"S2-{d}",
                "ndvi_avg": ndvi,
                "evi_avg": ndvi - 0.05,
                "mndwi_avg": -0.1,
                "ndmi_avg": ndmi,
                "official": official,
                "decloud_quality": "good",
                "cloud_cover": 5.0,
                "parcel_cloud_cover_pct": 5.0,
                "cloud_pct": 5.0,
                "clear": True,
            }
        )
    return rows


class SeasonGrowthFactsTests(unittest.TestCase):
    def test_peak_and_mean(self) -> None:
        pts = [{"date": r["date"], "value": r["ndvi_avg"]} for r in _s2_series()]
        peak = _peak(pts)
        self.assertIsNotNone(peak)
        assert peak is not None
        self.assertEqual(peak["date"], "2026-08-05")
        self.assertAlmostEqual(peak["value"], 0.70, places=3)
        mean = _series_mean(pts)
        self.assertIsNotNone(mean)
        self.assertGreater(mean or 0, 0.4)

    def test_drought_summary_counts(self) -> None:
        drought = _drought_summary(_s2_series(), season_months=(6, 7, 8, 9))
        self.assertIn("counts", drought)
        self.assertIn("drought_scene_count", drought)
        self.assertIsInstance(drought["drought_scene_count"], int)
        self.assertGreater(sum(drought["counts"].values()), 0)
        self.assertIn("scene_classes", drought)
        self.assertEqual(len(drought["scene_classes"]), len(_s2_series()))
        self.assertEqual(drought["classified"], drought["scene_classes"])
        for sc in drought["scene_classes"]:
            self.assertIn("date", sc)
            self.assertIn("class", sc)

    def test_flood_summary_no_s1(self) -> None:
        flood = _flood_summary([])
        self.assertEqual(flood["status"], "no_s1_data")
        self.assertEqual(flood["scene_count"], 0)
        self.assertEqual(flood["scenes"], [])

    def test_flood_summary_with_vv(self) -> None:
        s1 = [
            {"date": "2026-07-01", "scene_id": "S1A_20260701", "vv_avg": -12.0, "vh_avg": -18.0, "relative_orbit": 10},
            {"date": "2026-07-13", "scene_id": "S1A_20260713", "vv_avg": -11.5, "vh_avg": -17.5, "relative_orbit": 10},
            {"date": "2026-07-25", "scene_id": "S1A_20260725", "vv_avg": -12.2, "vh_avg": -18.1, "relative_orbit": 10},
            {"date": "2026-08-06", "scene_id": "S1A_20260806", "vv_avg": -11.8, "vh_avg": -17.8, "relative_orbit": 10},
        ]
        flood = _flood_summary(s1)
        self.assertEqual(flood["status"], "ok")
        self.assertEqual(flood["scene_count"], 4)
        self.assertIsNotNone(flood["vv_median"])
        self.assertIn("counts", flood)
        self.assertEqual(len(flood["scenes"]), 4)
        for sc in flood["scenes"]:
            self.assertIn("date", sc)
            self.assertIn("vv", sc)
            self.assertIn("vh", sc)
            self.assertIn("relative_orbit", sc)
            self.assertIn("class", sc)

    def test_appendix_and_timeline(self) -> None:
        s2 = _s2_series()
        drought = _drought_summary(s2, season_months=(6, 7, 8, 9))
        s1 = [
            {"date": "2026-07-01", "scene_id": "S1A", "vv_avg": -12.0, "vh_avg": -18.0, "relative_orbit": 10},
            {"date": "2026-07-13", "scene_id": "S1A", "vv_avg": -11.5, "vh_avg": -17.5, "relative_orbit": 10},
            {"date": "2026-07-25", "scene_id": "S1A", "vv_avg": -12.2, "vh_avg": -18.1, "relative_orbit": 10},
            {"date": "2026-08-06", "scene_id": "S1A", "vv_avg": -18.5, "vh_avg": -24.0, "relative_orbit": 10},
        ]
        flood = _flood_summary(s1)
        s2_app = _build_s2_appendix(s2, drought)
        self.assertEqual(len(s2_app), len(s2))
        self.assertIn("drought_class_cn", s2_app[0])
        self.assertIn("ndvi", s2_app[0])
        s1_app = _build_s1_appendix(flood)
        self.assertEqual(len(s1_app), len(flood["scenes"]))
        self.assertIn("flood_class_cn", s1_app[0])
        ndvi_ts = [{"date": r["date"], "value": r["ndvi_avg"], "official": True} for r in s2]
        tl = _build_timeline(
            start=date(2026, 6, 1),
            end=date(2026, 9, 30),
            s2_rows=s2,
            s1_rows=s1,
            drought=drought,
            flood=flood,
            ndvi_ts=ndvi_ts,
            crops=["玉米"],
            peak_month=8,
        )
        self.assertEqual(len(tl), 4)
        self.assertEqual(tl[0]["month"], "2026-06")
        self.assertIn("估计", tl[0]["crop_stage_estimate"])
        self.assertNotIn("播种", tl[0]["crop_stage_estimate"])
        self.assertIn("s2_growth", tl[0])
        self.assertIn("moisture", tl[0])
        self.assertIn("s1_flood", tl[0])
        meth = _methodology()
        self.assertIn("drought", meth)
        self.assertIn("flood", meth)
        self.assertIn("-17.0", meth["flood"])
        self.assertIn("-15.0", meth["flood"])
        self.assertIn("3.0", meth["flood"])
        self.assertEqual(drought_class_cn("severe"), "重度")
        self.assertEqual(flood_class_cn("watch"), "关注")
        self.assertEqual(flood_class_cn(None), "缺测/未定")
        self.assertEqual(quality_cn("good"), "良好")
        self.assertEqual(quality_cn("official"), "官方")
        self.assertEqual(quality_cn("bad"), "较差")
        self.assertIn("quality_cn", s2_app[0])
        self.assertEqual(s2_app[0]["quality_cn"], "良好")

    def test_phenology_estimate_is_labeled(self) -> None:
        label = phenology_stage_estimate(["玉米"], 6)
        self.assertIn("估计", label)
        self.assertNotIn("播种", label)
        self.assertIn("苗期", label)
        generic = phenology_stage_estimate([], 7, peak_month=7)
        self.assertIn("估计", generic)

    def test_confidence_rules(self) -> None:
        high_growth = compute_confidence(
            official_s2=27,
            peak_exists=True,
            drought_counts={"unreliable": 48, "severe": 8, "normal": 17},
            s1_count=15,
            flood_scene_count=0,
            harvest={"status": "detected", "confidence": "low"},
        )
        self.assertEqual(high_growth["growth"]["level"], "high")
        self.assertEqual(high_growth["growth"]["level_cn"], "高")
        self.assertEqual(high_growth["drought"]["level"], "medium")
        self.assertEqual(high_growth["flood"]["level"], "high")
        self.assertEqual(high_growth["harvest"]["level"], "low")

        low_growth = compute_confidence(
            official_s2=4,
            peak_exists=False,
            drought_counts={"unreliable": 10},
            s1_count=2,
            flood_scene_count=0,
            harvest={"status": "uncertain", "confidence": "low"},
        )
        self.assertEqual(low_growth["growth"]["level"], "low")
        self.assertEqual(low_growth["flood"]["level"], "low")
        self.assertEqual(low_growth["harvest"]["level"], "low")

        harvest_high = compute_confidence(
            official_s2=20,
            peak_exists=True,
            drought_counts={"normal": 20},
            s1_count=10,
            flood_scene_count=2,
            harvest={"status": "detected", "confidence": "high"},
        )
        self.assertEqual(harvest_high["harvest"]["level"], "high")
        self.assertEqual(harvest_high["flood"]["level"], "high")

    def test_program_core_conclusion_clipped(self) -> None:
        cards = [
            {"key": "growth", "value": "冠层绿度较峰值回落"},
            {"key": "drought", "value": "提示偏高"},
            {"key": "flood", "value": "未检出洪涝"},
        ]
        text = program_core_conclusion(
            status_cards=cards,
            yoy={"peak_date_shift": {"label": "峰值日期提前35天"}},
            harvest={"status": "detected", "confidence": "low"},
        )
        self.assertLessEqual(len(text), 80)
        self.assertNotIn("生物量", text)
        self.assertNotIn("立即收割", text)

    def test_disclaimer_mentions_program_and_caution(self) -> None:
        self.assertIn("程序计算", FOOTER_DISCLAIMER)
        self.assertIn("需进一步田间确认", FOOTER_DISCLAIMER)
        self.assertIn("立即收割", FOOTER_DISCLAIMER)

    def test_facts_for_llm_truncates(self) -> None:
        series = [{"date": f"2026-06-{(i % 28) + 1:02d}", "value": 0.4 + i * 0.001} for i in range(80)]
        facts = {
            "field": {"field_name": "测试"},
            "window": {"start_date": "2026-06-01", "end_date": "2026-09-30"},
            "ndvi": {"series": series, "mean": 0.5},
            "ndmi": {"series": series[:10]},
            "drought": {
                "days": [{"date": f"d{i}", "class": "mild"} for i in range(30)],
                "counts": {"mild": 30},
                "drought_scene_count": 30,
            },
            "flood": {
                "status": "ok",
                "counts": {},
                "scenes": [
                    {"date": f"2026-07-{(i % 28) + 1:02d}", "vv": -12.0, "class": "dry"}
                    for i in range(40)
                ],
            },
            "harvest": {"status": "detected", "harvest_date": "2026-09-05", "confidence": "low"},
            "scenes": {},
            "timeline": [{"month": "2026-06", "s2_count": 2}],
            "methodology": _methodology(),
            "s2_appendix": [
                {"date": f"2026-06-{(i % 28) + 1:02d}", "ndvi": 0.5} for i in range(50)
            ],
            "s1_appendix": [
                {"date": f"2026-07-{(i % 28) + 1:02d}", "vv": -12.0} for i in range(40)
            ],
            "confidence": {"growth": {"level": "high"}},
            "yoy": {"peak_date_shift": {"label": "峰值日期提前35天"}},
            "program_core_conclusion": "冠层绿度回落",
        }
        compact = facts_for_llm(facts, max_series=24)
        self.assertTrue(compact["ndvi"]["series_truncated"])
        self.assertLessEqual(len(compact["ndvi"]["series"]), 24)
        self.assertLessEqual(len(compact["drought"]["days"]), 10)
        self.assertIn("timeline", compact)
        self.assertLessEqual(len(compact["s2_appendix"]), 30)
        self.assertLessEqual(len(compact["s1_appendix"]), 20)
        self.assertTrue(compact.get("s2_appendix_truncated"))
        self.assertTrue(compact.get("s1_appendix_truncated"))
        self.assertIn("confidence", compact)
        self.assertIn("yoy", compact)
        self.assertIn("wording_hint", compact["harvest"])
        self.assertIn("疑似进入成熟后期", compact["harvest"]["wording_hint"])
        self.assertIn("峰值日期", compact["ai_rules"]["yoy_only"])

    def test_july_moisture_matches_usable_drought_days(self) -> None:
        """July moisture must reflect deduped usable classes, not raw+good duplicates."""
        # Same calendar date: official severe + non-official unreliable duplicate
        rows = []
        for d, ndvi, ndmi, official, q in [
            ("2026-07-02", 0.80, 0.05, True, "good"),
            ("2026-07-02", 0.10, 0.00, False, "bad"),  # duplicate noise
            ("2026-07-05", 0.81, 0.04, True, "good"),
            ("2026-07-05", 0.12, 0.01, False, "raw"),
            ("2026-07-09", 0.78, 0.08, True, "good"),
            ("2026-07-12", 0.82, 0.25, True, "good"),  # normal-ish
            ("2026-07-15", 0.79, 0.22, True, "good"),
            ("2026-07-20", 0.77, 0.20, True, "good"),
            ("2026-07-25", 0.76, 0.18, True, "good"),
            ("2026-06-10", 0.45, 0.15, True, "good"),
            ("2026-08-10", 0.70, 0.12, True, "good"),
        ]:
            rows.append(
                {
                    "date": d,
                    "scene_id": f"S2-{d}-{'off' if official else 'raw'}",
                    "ndvi_avg": ndvi,
                    "evi_avg": ndvi - 0.05,
                    "mndwi_avg": -0.1,
                    "ndmi_avg": ndmi,
                    "official": official,
                    "decloud_quality": q,
                    "cloud_cover": 5.0 if official else 80.0,
                    "parcel_cloud_cover_pct": 5.0 if official else 80.0,
                    "cloud_pct": 5.0 if official else 80.0,
                    "clear": official,
                }
            )
        drought = _drought_summary(rows, season_months=(6, 7, 8, 9))
        self.assertIn("usable_scene_classes", drought)
        july_days = [d for d in drought["days"] if str(d["date"]).startswith("2026-07")]
        # At least the three drought dates should survive dedupe if classified drought
        # Force-check aggregation prefers usable over unreliable duplicates:
        from app.reports.season_growth.facts import (
            dedupe_usable_scene_classes,
            count_classes_by_month,
        )
        # Inject known scene_classes with duplicate-date bug pattern
        scene_classes = [
            {"date": "2026-07-02", "class": "severe"},
            {"date": "2026-07-02", "class": "unreliable"},
            {"date": "2026-07-05", "class": "severe"},
            {"date": "2026-07-05", "class": "unreliable"},
            {"date": "2026-07-09", "class": "moderate"},
            {"date": "2026-07-12", "class": "normal"},
            {"date": "2026-07-15", "class": "normal"},
            {"date": "2026-07-18", "class": "normal"},
            {"date": "2026-07-20", "class": "normal"},
            {"date": "2026-07-22", "class": "normal"},
            {"date": "2026-07-25", "class": "normal"},
            {"date": "2026-07-28", "class": "unreliable"},
        ]
        usable = dedupe_usable_scene_classes(scene_classes)
        counts = count_classes_by_month(usable, "2026-07")
        self.assertEqual(counts.get("severe"), 2)
        self.assertEqual(counts.get("moderate"), 1)
        self.assertNotIn("unreliable", {k for k, v in counts.items() if k == "unreliable" and v >= 7})
        # unreliable only if no usable class that day — 07-28 only
        self.assertEqual(counts.get("unreliable"), 1)
        drought_forced = {
            "scene_classes": scene_classes,
            "usable_scene_classes": usable,
            "days": [
                {"date": "2026-07-02", "class": "severe"},
                {"date": "2026-07-05", "class": "severe"},
                {"date": "2026-07-09", "class": "moderate"},
            ],
            "counts": counts,
        }
        tl = _build_timeline(
            start=date(2026, 6, 1),
            end=date(2026, 9, 30),
            s2_rows=rows,
            s1_rows=[],
            drought=drought_forced,
            flood={"scenes": [], "counts": {}},
            ndvi_ts=[{"date": r["date"], "value": r["ndvi_avg"], "official": r["official"]} for r in rows],
            crops=["玉米"],
            peak_month=7,
        )
        july = next(r for r in tl if r["month"] == "2026-07")
        self.assertIn("重度2", july["moisture"])
        self.assertIn("中度1", july["moisture"])
        self.assertEqual(july["drought_days"], 3)
        # Must NOT look like the old buggy "中度1 / 正常6 / 不可靠7"
        self.assertNotRegex(july["moisture"], r"不可靠7")


    def test_next_season_agronomy_not_remote_ops(self) -> None:
        text = program_next_season_actions(
            drought={
                "drought_scene_count": 5,
                "counts": {"severe": 3, "mild": 2},
                "days": [{"date": "2026-09-01", "class": "severe"}],
            },
            flood={"counts": {"watch": 2}, "flood_scene_count": 0},
        )
        self.assertFalse(looks_like_remote_ops_advice(text))
        self.assertNotIn("无人机", text)
        self.assertNotIn("多源卫星", text)
        self.assertTrue(("灌溉" in text) or ("墒情" in text))
        self.assertTrue(looks_like_remote_ops_advice("增加多源卫星或无人机补测频次"))



    def test_growth_grade_shares(self) -> None:
        shares = compute_growth_grade_shares([0.7, 0.6, 0.4, 0.2, 0.1])
        self.assertEqual(shares["n"], 5)
        self.assertEqual(shares["counts"]["较好"], 2)
        self.assertEqual(shares["counts"]["正常"], 1)
        self.assertEqual(shares["counts"]["偏弱"], 2)
        self.assertEqual(classify_growth_grade(0.55), "较好")
        self.assertEqual(classify_growth_grade(0.35), "正常")
        self.assertEqual(classify_growth_grade(0.34), "偏弱")

    def test_pick_rgb_scene_prefers_clear(self) -> None:
        rows = [
            {
                "date": "2026-09-08",
                "rgb_url": "http://x/a.png",
                "official": True,
                "clear": False,
                "cloud_pct": 90.0,
            },
            {
                "date": "2026-09-10",
                "rgb_url": "http://x/b.png",
                "official": True,
                "clear": True,
                "cloud_pct": 5.0,
            },
            {
                "date": "2026-07-07",
                "rgb_url": "http://x/c.png",
                "official": True,
                "clear": True,
                "cloud_pct": 3.0,
            },
        ]
        latest = _pick_rgb_scene(rows, prefer_date="2026-09-10")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["date"], "2026-09-10")
        peak = _pick_rgb_scene(rows, prefer_date="2026-07-07")
        self.assertEqual(peak["date"], "2026-07-07")

    def test_build_spatial_block_urls(self) -> None:
        rows = [
            {
                "date": "2026-09-10",
                "rgb_url": "http://example/rgb.png",
                "large_rgb_url": "http://example/large.png",
                "rgb_oss_key": "s1s2_parcel/img/6597/x.png",
                "official": True,
                "clear": True,
                "cloud_pct": 0.0,
                "pixel_n": 0,
            }
        ]
        spatial = build_spatial_block(
            None,
            land_id=None,
            s2_rows=rows,
            ndvi_peak={"date": "2026-09-10", "value": 0.5},
            ndvi_latest={"date": "2026-09-10", "value": 0.4},
        )
        self.assertEqual(spatial["latest_rgb_url"], "http://example/rgb.png")
        self.assertEqual(spatial["latest_rgb_date"], "2026-09-10")
        self.assertIsNotNone(spatial["rgb_url"])
        self.assertFalse(spatial["has_pixel_stats"])


if __name__ == "__main__":
    unittest.main()
