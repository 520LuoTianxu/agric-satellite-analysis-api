"""Regression tests for the biweekly PDF: fixed scales, robust statistics,
stage-specific metrics, missing data and formal wording.

样稿用例来自 app.reports.biweekly.demo_data（示例数据，非实测），全程不联网。
"""

from __future__ import annotations

import inspect
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

# 正文不得出现的口语化或替代性措辞。
BANNED_WORDS = (
    "绿度",
    "叶片营养",
    "读数",
    "更应看",
    "这两周",
    "水分账",
    "变差",
    "好转",
    "失墒",
    "受灾面积",
)

# 原因类词语只允许出现在「不判定」「多种因素」这类限定句里。
CAUSE_WORDS = ("病虫害", "缺肥", "干旱")
CAUSE_QUALIFIERS = ("不判定", "不能判定", "多种因素")

FORMAL_NAMES = {
    "ndvi": "归一化植被指数（NDVI）",
    "evi": "增强型植被指数（EVI）",
    "ndre": "归一化差值红边指数（NDRE）",
    "ndmi": "归一化差值水分指数（NDMI）",
}


def _grid_product(
    day: str,
    values: np.ndarray,
    *,
    metric: str = "NDVI",
    clear: np.ndarray | None = None,
    lon0: float = 115.5,
    lat0: float = 36.5,
) -> dict:
    """A rows×cols parcel product on a 10 m grid; NaN becomes a missing (None) value."""
    rows, cols = values.shape
    lons, lats, column = [], [], []
    for r in range(rows):
        for c in range(cols):
            lons.append(lon0 + c * 0.000112)
            lats.append(lat0 - r * 0.00009)
            value = float(values[r, c])
            column.append(None if np.isnan(value) else value)
    columns = {"lon": lons, "lat": lats, metric: column}
    if clear is not None:
        columns["clear"] = [None if np.isnan(v) else int(v) for v in clear.ravel()]
    return {
        "land_id": "T-1",
        "date": day,
        "sensor": "S2",
        "parcel_cloud_cover_pct": 0.0,
        "cloud_cover": 1.0,
        "stac_item_id": f"TEST_{day}",
        "pixel_data": {"format": "lonlat_columns_v1", "columns": columns},
    }


def _is_green(rgba) -> bool:
    r, g, b = rgba[:3]
    return g > r + 0.02 and g > b


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", text)


class FixedScaleTests(unittest.TestCase):
    def test_ndvi_scale_is_fixed_zero_to_one(self):
        from app.reports.biweekly.scales import absolute_scale

        scale = absolute_scale("ndvi")
        self.assertEqual((scale["norm"].vmin, scale["norm"].vmax), (0.0, 1.0))
        self.assertEqual(scale["ticks"][0], 0.0)
        self.assertEqual(scale["ticks"][-1], 1.0)
        # 色标不接收数据：任何田块、任何日期得到的都是同一把尺。
        self.assertEqual(list(inspect.signature(absolute_scale).parameters), ["metric"])

    def test_ndvi_at_or_below_low_reference_is_never_green(self):
        from app.reports.biweekly.scales import color_for

        for value in (0.0, 0.05, 0.1, 0.2, 0.25, 0.29, 0.30):
            self.assertFalse(_is_green(color_for("ndvi", value)), value)
        for value in (0.6, 0.8, 1.0):
            self.assertTrue(_is_green(color_for("ndvi", value)), value)
        # 用户截图情形：整块田最高 0.32，不得与高值同色。
        low_max = np.array(color_for("ndvi", 0.32)[:3])
        high = np.array(color_for("ndvi", 0.9)[:3])
        self.assertGreater(float(np.abs(low_max - high).sum()), 0.8)
        # 深绿只留给接近上限的值：0.475 的亮度明显高于 1.0。
        self.assertGreater(sum(color_for("ndvi", 0.475)[:3]), sum(color_for("ndvi", 1.0)[:3]) + 0.8)

    def test_negative_and_missing_have_their_own_colours(self):
        from matplotlib.colors import to_rgba

        from app.reports.biweekly.scales import BELOW_ZERO_COLOR, MISSING_COLOR, color_for

        self.assertEqual(color_for("ndvi", -0.1), to_rgba(BELOW_ZERO_COLOR))
        self.assertEqual(color_for("ndvi", None), to_rgba(MISSING_COLOR))
        self.assertEqual(color_for("ndvi", float("nan")), to_rgba(MISSING_COLOR))
        self.assertNotEqual(color_for("ndvi", 0.0), to_rgba(MISSING_COLOR))

    def test_each_metric_uses_its_own_range(self):
        from matplotlib.colors import TwoSlopeNorm

        from app.reports.biweekly.scales import absolute_scale

        expected = {"ndvi": (0.0, 1.0), "evi": (0.0, 1.2), "ndre": (0.0, 0.8), "ndmi": (-0.5, 0.5)}
        for metric, (low, high) in expected.items():
            norm = absolute_scale(metric)["norm"]
            self.assertEqual((norm.vmin, norm.vmax), (low, high), metric)
        ndmi = absolute_scale("ndmi")["norm"]
        self.assertIsInstance(ndmi, TwoSlopeNorm)
        self.assertEqual(ndmi.vcenter, 0.0)

    def test_low_reference_comes_from_platform_index_registry(self):
        from agric_satellite_analysis_common.biweekly.metric_specs import METRIC_SPECS, REPORT_METRIC_ORDER
        from app.tasks.indices import INDEX_REGISTRY

        for metric in REPORT_METRIC_ORDER:
            spec = METRIC_SPECS[metric]
            self.assertEqual(spec.low_reference, INDEX_REGISTRY[metric].alerts.threshold, metric)
            low, high = spec.valid_range
            self.assertLessEqual(low, spec.display_range[0])
            self.assertGreaterEqual(high, spec.display_range[1])
        self.assertEqual(METRIC_SPECS["ndvi"].valid_range, (-1.0, 1.0))
        self.assertEqual(METRIC_SPECS["evi"].valid_range, (-1.0, 2.0))

    def test_change_and_relative_scales_are_symmetric_about_zero(self):
        from app.reports.biweekly.scales import change_scale, relative_scale

        rng = np.random.default_rng(7)
        values = rng.normal(0.06, 0.02, 500)
        for build in (change_scale, relative_scale):
            scale = build(values)
            self.assertEqual(scale["norm"].vmin, -scale["norm"].vmax)
            self.assertIn(0.0, scale["ticks"])
            centre = scale["cmap"](scale["norm"](0.0))
            self.assertTrue(all(c > 0.9 for c in centre[:3]))

    def test_symmetric_range_ignores_single_pixel_extremes(self):
        from app.reports.biweekly.scales import symmetric_limit

        values = np.concatenate([np.linspace(-0.03, 0.03, 600), [0.9, -0.8]])
        self.assertEqual(symmetric_limit(values), 0.05)
        self.assertEqual(symmetric_limit(np.array([np.nan, np.nan])), 0.05)

    def test_maps_use_the_same_scale_on_every_date_and_field(self):
        from app.reports.biweekly import infield_maps

        seen: list[tuple[float, float]] = []
        original = infield_maps._draw_pixels

        def _spy(ax, xs, ys, values, *, half, cmap, norm, invalid=None):
            seen.append((float(norm.vmin), float(norm.vmax)))
            return original(ax, xs, ys, values, half=half, cmap=cmap, norm=norm, invalid=invalid)

        rng = np.random.default_rng(3)
        low_early = _grid_product("2026-03-01", 0.30 + 0.175 * rng.random((12, 12)))
        low_late = _grid_product("2026-03-11", 0.20 + 0.12 * rng.random((12, 12)))
        high_field = _grid_product("2026-05-01", 0.75 + 0.15 * rng.random((12, 12)), lon0=116.2)
        figures = Path(tempfile.mkdtemp())
        with patch.object(infield_maps, "_draw_pixels", side_effect=_spy):
            first = infield_maps.build_infield_metric(
                metric="ndvi", latest=low_late, baseline=low_early, dest=figures / "a.png"
            )
            second = infield_maps.build_infield_metric(
                metric="ndvi", latest=high_field, baseline=None, dest=figures / "b.png"
            )
        self.assertEqual(first["panels"], ["baseline", "latest", "change", "relative"])
        self.assertEqual(second["panels"], ["latest", "relative"])
        # 绝对值面板：两个日期、两块田都是 0 至 1。
        self.assertEqual(seen[0], (0.0, 1.0))
        self.assertEqual(seen[1], (0.0, 1.0))
        self.assertEqual(seen[4], (0.0, 1.0))
        # 变化图与相对差异图以 0 居中。
        for index in (2, 3, 5):
            self.assertEqual(seen[index][0], -seen[index][1])
        self.assertTrue(Path(first["image"]).is_file())
        # 截图情形（0.20 至 0.32）：统计如实给出，大部分像元在参考线 0.3 以下。
        self.assertGreater(first["stats"]["below_low_reference_fraction"], 0.7)
        self.assertLess(first["stats"]["median"], 0.3)


class RobustStatisticsTests(unittest.TestCase):
    def test_cloud_masked_and_missing_pixels_are_excluded_not_zero(self):
        from app.reports.biweekly.metric_stats import field_stats, pixel_table

        values = np.full((10, 10), 0.6)
        values[0, :] = np.nan
        clear = np.ones((10, 10))
        clear[1, :] = 0
        # 云影下的数值即使看似正常也不参与统计。
        values[1, :] = 0.05
        clear[2, :5] = np.nan
        product = _grid_product("2026-04-09", values, clear=clear)
        stats = field_stats(product, "ndvi")
        self.assertEqual(stats["status"], "ok")
        self.assertEqual(stats["n_total"], 100)
        self.assertEqual(stats["n_valid"], 80)
        self.assertEqual(stats["n_cloud"], 10)
        self.assertAlmostEqual(stats["valid_fraction"], 0.8)
        self.assertAlmostEqual(stats["median"], 0.6)
        self.assertAlmostEqual(stats["mean"], 0.6)
        table = pixel_table(product, "ndvi")
        self.assertTrue(np.isnan(table["value"][:20]).all())
        self.assertFalse((table["value"] == 0).any())

    def test_out_of_range_values_are_invalid(self):
        from app.reports.biweekly.metric_stats import field_stats

        values = np.full((6, 6), 0.5)
        values[0, 0] = 3.2
        values[0, 1] = -1.4
        stats = field_stats(_grid_product("2026-04-09", values), "ndvi")
        self.assertEqual(stats["n_valid"], 34)
        self.assertAlmostEqual(stats["median"], 0.5)

    def test_all_invalid_date_reports_missing(self):
        from app.reports.biweekly.infield_maps import analyse_metric
        from app.reports.biweekly.metric_stats import field_stats

        values = np.full((8, 8), 0.4)
        product = _grid_product("2026-04-09", values, clear=np.zeros((8, 8)))
        stats = field_stats(product, "ndvi")
        self.assertEqual(stats["status"], "insufficient_valid_pixels")
        self.assertEqual(stats["n_valid"], 0)
        self.assertNotIn("median", stats)
        self.assertIsNone(analyse_metric("ndvi", product, None))

        empty = _grid_product("2026-04-09", np.full((8, 8), np.nan))
        self.assertEqual(field_stats(empty, "ndvi")["status"], "insufficient_valid_pixels")
        self.assertEqual(field_stats(empty, "ndre")["status"], "missing_metric")

    def test_constant_field_flags_nothing(self):
        from app.reports.biweekly.infield_maps import analyse_metric
        from app.reports.biweekly.metric_stats import tukey_low_mask

        values = np.full((10, 10), 0.42)
        self.assertFalse(tukey_low_mask(values.ravel()).any())
        analysis = analyse_metric("ndvi", _grid_product("2026-04-09", values), None)
        self.assertEqual(analysis["relative"]["flagged_pixels"], 0)
        self.assertEqual(analysis["relative"]["clustered_pixels"], 0)
        self.assertEqual(analysis["relative"]["regions"], [])
        self.assertAlmostEqual(analysis["stats"]["iqr"], 0.0)

    def test_single_pixel_extremes_do_not_drive_statistics_or_location(self):
        from app.reports.biweekly.infield_maps import analyse_metric

        rng = np.random.default_rng(11)
        values = 0.60 + rng.normal(0, 0.01, (20, 20))
        values[3, 4] = -0.9
        values[15, 17] = 1.0
        analysis = analyse_metric("ndvi", _grid_product("2026-04-09", values), None)
        stats = analysis["stats"]
        self.assertAlmostEqual(stats["median"], 0.60, delta=0.005)
        self.assertGreater(stats["p10"], 0.57)
        self.assertLess(stats["p90"], 0.63)
        relative = analysis["relative"]
        self.assertGreaterEqual(relative["flagged_pixels"], 1)
        # 孤立像元不成片：不给位置，不进核查建议。
        self.assertEqual(relative["clustered_pixels"], 0)
        self.assertEqual(relative["regions"], [])
        self.assertEqual(relative["isolated_pixels"], relative["flagged_pixels"])

    def test_clustered_low_patch_is_located_and_separated_from_absolute_low(self):
        from app.reports.biweekly.infield_maps import analyse_metric

        rng = np.random.default_rng(5)
        values = 0.70 + rng.normal(0, 0.008, (20, 20))
        values[1:4, 16:19] = 0.55
        analysis = analyse_metric("ndvi", _grid_product("2026-04-09", values), None)
        relative = analysis["relative"]
        self.assertEqual(relative["clustered_pixels"], 9)
        self.assertEqual(relative["regions"][0]["name"], "东北部")
        # 相对本田偏低，但绝对值 0.55 高于低值参考线 0.30。
        self.assertEqual(relative["clustered_below_low_reference"], 0)
        self.assertEqual(analysis["stats"]["below_low_reference_fraction"], 0.0)

    def test_change_only_counts_pixels_valid_on_both_dates(self):
        from app.reports.biweekly.infield_maps import analyse_metric

        earlier = np.full((10, 10), 0.50)
        later = np.full((10, 10), 0.56)
        clear = np.ones((10, 10))
        clear[:2, :] = 0
        analysis = analyse_metric(
            "ndvi",
            _grid_product("2026-04-09", later, clear=clear),
            _grid_product("2026-04-04", earlier),
        )
        self.assertEqual(analysis["change"]["n_paired"], 80)
        self.assertAlmostEqual(analysis["change"]["median"], 0.06)
        self.assertEqual(analysis["change"]["increase_fraction"], 1.0)


class StageMetricTests(unittest.TestCase):
    def test_each_growth_phase_leads_with_different_metrics(self):
        from app.reports.biweekly.enrichment import report_metric_plan

        plans = {label: report_metric_plan({"display_phase": label}) for label in ("S1", "S2", "S3")}
        self.assertEqual(plans["S1"]["primary"], ["ndvi"])
        self.assertEqual(plans["S2"]["primary"], ["ndre", "evi"])
        self.assertEqual(plans["S3"]["primary"], ["ndre", "ndmi"])
        self.assertEqual(plans["S1"]["supporting"], ["evi", "ndmi"])
        self.assertEqual(plans["S2"]["supporting"], ["ndvi", "ndmi"])
        self.assertEqual(plans["S3"]["supporting"], ["ndvi", "evi"])
        for plan in plans.values():
            self.assertEqual(plan["basis"], "phase")
            self.assertTrue(plan["rationale"])
            # 阶段名称是生长阶段，不是卫星名。
            self.assertNotIn("哨兵", plan["formal_name"])
            self.assertIn("生长", plan["formal_name"])

    def test_split_period_follows_latest_observation_phase(self):
        from app.reports.biweekly.enrichment import report_metric_plan

        phase = {
            "display_phase": None,
            "primary_metrics": [],
            "observation_stages": [
                {"date": "2026-03-20", "stage_code": "返青期", "phase_label": "S1"},
                {"date": "2026-03-28", "stage_code": "拔节孕穗期", "phase_label": "S2"},
            ],
        }
        plan = report_metric_plan(phase, "2026-03-28")
        self.assertEqual(plan["basis"], "latest_observation_phase")
        self.assertEqual(plan["phase_label"], "S2")
        self.assertEqual(plan["primary"], ["ndre", "evi"])

    def test_no_plan_lists_all_metrics_without_stage_claims(self):
        from app.reports.biweekly.enrichment import report_metric_plan

        plan = report_metric_plan({})
        self.assertEqual(plan["basis"], "default")
        self.assertEqual(plan["primary"] + plan["supporting"], ["ndvi", "evi", "ndre", "ndmi"])
        self.assertIsNone(plan["rationale"])

    def test_missing_metric_stays_missing_in_series_and_comparison(self):
        from app.reports.biweekly.enrichment import attach_report_inputs
        from app.reports.biweekly.report_text import MISSING, evidence_table

        facts = {
            "land_id": "T-1",
            "period": {"start": "2026-04-02", "end_inclusive": "2026-04-15", "end_exclusive": "2026-04-16"},
            "phase": {"display_phase": "S2"},
        }
        products = [
            _grid_product("2026-04-04", np.full((6, 6), 0.30), metric="NDRE"),
            _grid_product("2026-04-09", np.full((6, 6), 0.34), metric="NDRE"),
        ]
        attach_report_inputs(facts, products)
        comparison = facts["comparison"]
        self.assertEqual(comparison["metrics"]["ndre"]["delta"], 0.04)
        for metric in ("evi", "ndvi", "ndmi"):
            self.assertIsNone(comparison["metrics"][metric]["latest"], metric)
            self.assertIsNone(comparison["metrics"][metric]["delta"], metric)
        rows = {row[0]: row for row in evidence_table(facts)[1:]}
        self.assertEqual(rows["EVI（主要）"][1], MISSING)
        self.assertNotIn("0.00", rows["EVI（主要）"][1:4])

    def test_printed_change_equals_difference_of_printed_values(self):
        from app.reports.biweekly.enrichment import build_comparison

        def _row(day, in_period, value):
            return {
                "date": day,
                "in_period": in_period,
                "metrics": {"ndvi": {"status": "ok", "median": value, "field_mean": value}},
            }

        series = [_row("2026-04-04", True, 0.2449), _row("2026-04-09", True, 0.2551)]
        with patch("app.reports.biweekly.enrichment.series_statistic", return_value="median"):
            item = build_comparison(series, ["ndvi"])["metrics"]["ndvi"]
        # 0.26 − 0.24，而不是 0.0102 取整后的 0.01。
        self.assertEqual(item["delta"], 0.02)
        self.assertEqual(item["direction"], "up")

    def test_evidence_table_shows_latest_percentiles_even_if_series_uses_mean(self):
        """An early mean-only date forces series means, but must not hide real latest p10/p90."""
        from app.reports.biweekly.report_text import evidence_caption, evidence_table

        facts = {
            "report_metrics": {"primary": ["ndre", "evi"], "supporting": ["ndvi", "ndmi"]},
            "metric_statistics": {"ndre": "mean", "evi": "mean", "ndvi": "mean", "ndmi": "mean"},
            "comparison": {
                "latest_date": "2026-03-31",
                "baseline_date": "2026-03-19",
                "metrics": {
                    "ndre": {"statistic": "mean", "latest": 0.565933, "baseline": 0.395735, "delta": 0.17},
                    "evi": {"statistic": "mean", "latest": 0.71017, "baseline": 0.453088, "delta": 0.26},
                    "ndvi": {"statistic": "mean", "latest": 0.708331, "baseline": 0.574236, "delta": 0.14},
                    "ndmi": {"statistic": "mean", "latest": 0.411788, "baseline": 0.196497, "delta": 0.21},
                },
            },
            "metric_series": [
                {
                    "date": "2026-02-22",
                    "in_period": False,
                    "source": "field_mean",
                    "metrics": {
                        "ndre": {"status": "mean_only", "field_mean": 0.197459, "p10": None, "p90": None},
                        "evi": {"status": "mean_only", "field_mean": 0.229067, "p10": None, "p90": None},
                        "ndvi": {"status": "mean_only", "field_mean": 0.275168, "p10": None, "p90": None},
                        "ndmi": {"status": "mean_only", "field_mean": 0.029255, "p10": None, "p90": None},
                    },
                },
                {
                    "date": "2026-03-19",
                    "in_period": False,
                    "source": "pixels",
                    "metrics": {
                        "ndre": {"status": "ok", "median": 0.4023715, "p10": 0.3415024, "p90": 0.4441943, "field_mean": 0.395735},
                        "evi": {"status": "ok", "median": 0.4537015, "p10": 0.3728518, "p90": 0.5382044, "field_mean": 0.453088},
                        "ndvi": {"status": "ok", "median": 0.5822055, "p10": 0.4923318, "p90": 0.647431, "field_mean": 0.574236},
                        "ndmi": {"status": "ok", "median": 0.203001, "p10": 0.1329657, "p90": 0.2518124, "field_mean": 0.196497},
                    },
                },
                {
                    "date": "2026-03-31",
                    "in_period": True,
                    "source": "pixels",
                    "metrics": {
                        "ndre": {"status": "ok", "median": 0.579301, "p10": 0.5063956, "p90": 0.6044996, "field_mean": 0.565933},
                        "evi": {"status": "ok", "median": 0.721904, "p10": 0.6419339, "p90": 0.766859, "field_mean": 0.71017},
                        "ndvi": {"status": "ok", "median": 0.722792, "p10": 0.6587616, "p90": 0.7432993, "field_mean": 0.708331},
                        "ndmi": {"status": "ok", "median": 0.4249015, "p10": 0.3420948, "p90": 0.4610775, "field_mean": 0.411788},
                    },
                },
            ],
        }
        rows = {row[0]: row for row in evidence_table(facts)[1:]}
        self.assertEqual(rows["NDRE（主要）"][4], "0.51 至 0.60")
        self.assertEqual(rows["EVI（主要）"][4], "0.64 至 0.77")
        caption = evidence_caption(facts)
        self.assertIn("百分位来自最近一次观测的有效像元", caption)
        self.assertNotIn("无像元级数据", caption)
        self.assertNotIn("未提供百分位", caption)


class DemoReportTests(unittest.TestCase):
    """Render the three demo cases through the official generate entry once."""

    @classmethod
    def setUpClass(cls):
        from pypdf import PdfReader

        from app.reports.biweekly import demo_data

        cls.out = Path(tempfile.mkdtemp(prefix="biweekly-quality-"))
        cls.built = {}
        cls.pages = {}
        cls.text = {}
        for name in demo_data.CASES:
            built = demo_data.render_case(name, cls.out)
            reader = PdfReader(str(built["pdf"]))
            pages = [_flat(page.extract_text() or "") for page in reader.pages]
            cls.built[name] = built
            cls.pages[name] = pages
            cls.text[name] = "".join(pages)

    def _enriched(self, name: str) -> dict:
        import json

        return json.loads((self.built[name]["out_dir"] / "facts-enriched.json").read_text(encoding="utf-8"))

    def test_demo_cases_cover_three_growth_phases_with_different_metrics(self):
        primary = {name: self._enriched(name)["report_metrics"]["primary"] for name in self.built}
        self.assertEqual(primary["s1-tillering"], ["ndvi"])
        self.assertEqual(primary["s2-jointing"], ["ndre", "evi"])
        self.assertEqual(primary["s3-grainfill"], ["ndre", "ndmi"])
        labels = {name: self._enriched(name)["phase"]["display_phase"] for name in self.built}
        self.assertEqual(labels, {"s1-tillering": "S1", "s2-jointing": "S2", "s3-grainfill": "S3"})

    def test_primary_infield_maps_share_one_page(self):
        # 改版前 s1 6、s2 8、s3 7。合页并去掉整页数值表后：s1 仍是 6（只有一个主要指标，减不了分布页），s2 7、s3 6。
        expected = {"s1-tillering": 6, "s2-jointing": 7, "s3-grainfill": 6}
        for name, pages in self.pages.items():
            self.assertEqual(len(pages), expected[name], (name, len(pages)))
            enriched = self._enriched(name)
            spatial = [page for page in pages if "田内空间分布" in page]
            self.assertEqual(len(spatial), 1, name)
            self.assertIn("二、田内空间分布", spatial[0])
            self.assertIn("比整块地偏弱", spatial[0])
            joined_case = "".join(pages)
            if enriched.get("surrounding_comparison_image"):
                self.assertIn("三、和周围的农田比", joined_case)
                self.assertIn("四、指标变化", joined_case)
            else:
                self.assertNotIn("三、和周围的农田比", joined_case)
                self.assertIn("三、指标变化", joined_case)
            self.assertEqual(
                [item["metric"] for item in enriched["infield"]], enriched["report_metrics"]["primary"]
            )
            self.assertNotIn("观测与数据质量", "".join(pages))
            self.assertIn("历史可比数据不足，暂不评价", "".join(pages))
            self.assertIn("持续偏弱", "".join(pages))
            self.assertIn("均匀度", "".join(pages))
            self.assertIn("不判定干旱或洪涝", "".join(pages))

    def test_formal_metric_names_and_no_colloquial_wording(self):
        for name, text in self.text.items():
            enriched = self._enriched(name)
            plan = enriched["report_metrics"]
            for metric in plan["primary"] + plan["supporting"]:
                self.assertIn(_flat(FORMAL_NAMES[metric]), text, (name, metric))
            for word in BANNED_WORDS:
                self.assertNotIn(word, text, (name, word))
            for sentence in text.split("。"):
                if any(word in sentence for word in CAUSE_WORDS):
                    self.assertTrue(any(q in sentence for q in CAUSE_QUALIFIERS), (name, sentence))
            # 阶段代号只在内部使用，正文写阶段名称。
            self.assertNotRegex(text, r"S[123]阶段")
            self.assertIn("生长阶段", text)

    def test_demo_reports_are_marked_on_every_page(self):
        for name, pages in self.pages.items():
            for index, page in enumerate(pages):
                self.assertIn("非实测", page, (name, index))
            self.assertIn("不用于农事决策", pages[0])
            self.assertEqual(self._enriched(name)["data_kind"], "demo")

    def test_low_ndvi_field_is_reported_as_low_not_healthy(self):
        text = self.text["s1-tillering"]
        self.assertNotIn("低值参考线", text)
        self.assertIn("中位数", text)
        self.assertIn("色标范围固定", text)
        infield = self._enriched("s1-tillering")["infield"][0]
        self.assertLess(infield["stats"]["p90"], 0.30)
        self.assertGreater(infield["stats"]["below_low_reference_fraction"], 0.9)
        # 好坏不由代码写成结论，正文也不标明这段文字的来源。
        self.assertNotIn("本次没有生成", text)
        self.assertNotIn("由模型", text)
        self.assertIn("不判定病虫害", text)

    def test_rejected_pass_and_cloud_shadow_are_disclosed(self):
        s1 = self._enriched("s1-tillering")
        rejected = s1["data_quality"]["rejected"]
        self.assertEqual([row["date"] for row in rejected], ["2025-11-03"])
        self.assertEqual(rejected[0]["reason"], "cloud_over_limit")
        self.assertIn("未采用：云量超过限值", self.text["s1-tillering"])

        s2 = self._enriched("s2-jointing")
        self.assertLess(s2["data_quality"]["valid_fraction_min"], 1.0)
        self.assertIn("有效像元占比", self.text["s2-jointing"])
        self.assertIn("云影", self.text["s2-jointing"])

    def test_same_dates_in_text_table_and_maps(self):
        for name in self.built:
            enriched = self._enriched(name)
            comparison = enriched["comparison"]
            for item in enriched["infield"]:
                self.assertEqual(item["date"], comparison["latest_date"], name)
                self.assertEqual(item["baseline_date"], comparison["baseline_date"], name)

    def test_weather_gaps_are_disclosed_not_summed_as_zero(self):
        enriched = self._enriched("s3-grainfill")
        period = enriched["weather_period"]
        self.assertEqual(period["days"], 14)
        self.assertEqual(period["precipitation_days"], 12)
        self.assertEqual(period["et0_days"], 11)
        missing = [row for row in enriched["weather_daily"] if row.get("precipitation_mm") is None]
        self.assertTrue(missing)
        self.assertNotIn("合计只含12天", self.text["s3-grainfill"])
        self.assertNotIn("合计只含11天", self.text["s3-grainfill"])

    def test_isolated_pixels_do_not_become_field_check_locations(self):
        enriched = self._enriched("s3-grainfill")
        for item in enriched["infield"]:
            self.assertEqual(item["relative"]["clustered_pixels"], 0)
        self.assertIn("零星像元不作为核查位置", self.text["s3-grainfill"])
        self.assertNotIn("本期未发现需要优先核查的位置", self.text["s3-grainfill"])
        regions = []
        for item in self._enriched("s1-tillering")["infield"]:
            regions.extend((item.get("relative") or {}).get("regions") or [])
        self.assertTrue(any(region.get("name") == "东北部" for region in regions))
        self.assertNotIn("田块东北部", self.text["s1-tillering"])

    def test_trigger_facts_carry_prior_and_rejected_observations(self):
        facts = self.built["s1-tillering"]["facts"]
        self.assertEqual([row["date"] for row in facts["prior_observations"]], ["2025-10-21"])
        rejected = facts["rejected_optical"]
        self.assertEqual(rejected[0]["date"], "2025-11-03")
        self.assertEqual(rejected[0]["reason"], "cloud_over_limit")
        self.assertGreater(rejected[0]["parcel_cloud_cover_pct"], 30)
        for row in facts["observations"]:
            self.assertIn("parcel_cloud_cover_pct", row)


class WeatherSummaryTests(unittest.TestCase):
    def test_missing_days_are_counted_separately(self):
        from app.reports.biweekly.weather_fetch import summarize_weather_rows

        rows = [
            {"date": "2026-05-11", "precipitation_mm": 2.0, "min_c": 12, "max_c": 24, "et0_mm": 4.0},
            {"date": "2026-05-12", "precipitation_mm": None, "min_c": 13, "max_c": 25, "et0_mm": None},
            {"date": "2026-05-13", "precipitation_mm": 0.0, "min_c": 14, "max_c": 26, "et0_mm": 4.5},
        ]
        summary = summarize_weather_rows(rows)
        self.assertEqual(summary["days"], 3)
        self.assertEqual(summary["precipitation_days"], 2)
        self.assertEqual(summary["et0_days"], 2)
        self.assertEqual(summary["precipitation_mm"], 2.0)

    def test_report_without_weather_omits_the_section(self):
        from app.reports.biweekly.report_text import has_weather, omitted_items

        facts = {"weather_daily": [], "weather_period": {"days": 0, "precipitation_mm": None, "et0_mm": None}}
        self.assertFalse(has_weather(facts))
        self.assertTrue(any("气象" in item for item in omitted_items(facts)))


class LayoutFixtureTests(unittest.TestCase):
    """Screenshot-like and evidence-gap PDFs. Not production land 32696."""

    @classmethod
    def setUpClass(cls):
        from pypdf import PdfReader

        from app.reports.biweekly.demo_data import render_layout_fixture, render_water_insufficient

        cls.out = Path(tempfile.mkdtemp(prefix="biweekly-layout-"))
        cls.dense = _pages(PdfReader(str(render_layout_fixture(cls.out)["pdf"])))
        cls.thin = _pages(PdfReader(str(render_water_insufficient(cls.out)["pdf"])))

    def test_dense_report_keeps_both_maps_and_the_archive_on_existing_pages(self):
        self.assertEqual(len(self.dense), 6)
        spatial = [page for page in self.dense if "田内空间分布" in page]
        self.assertEqual(len(spatial), 1)
        self.assertIn("叶绿素", spatial[0])
        self.assertIn("叶子里的水", spatial[0])
        joined = "".join(self.dense)
        self.assertIn("云量超过限值（42%）", joined)
        self.assertIn("0.61", joined)
        self.assertIn("相差", joined)
        self.assertIn("不是产量", joined)
        self.assertIn("方向不一致", joined)
        self.assertIn("不判定干旱或洪涝", joined)
        self.assertIn("不是生产地块32696的结论", joined)
        self.assertNotIn("观测与数据质量", joined)
        self.assertIn("二、田内空间分布", joined)
        self.assertIn("三、指标变化", joined)
        self.assertIn("四、气象条件", joined)
        self.assertIn("和周围的农田比", joined)

    def test_missing_ndmi_and_weather_is_one_line_not_a_page(self):
        self.assertEqual(len(self.thin), 5)
        joined = "".join(self.thin)
        self.assertIn("二、田内空间分布", joined)
        self.assertIn("三、指标变化", joined)
        self.assertNotIn("四、指标变化", joined)
        self.assertNotIn("三、气象条件", joined)
        self.assertNotIn("四、气象条件", joined)
        self.assertNotIn("五、气象条件", joined)
        self.assertIn("历史可比数据不足，暂不评价", joined)
        self.assertIn("证据不足", joined)
        self.assertIn("不能写成无风险", joined)
        self.assertIn("不判定干旱或洪涝", joined)
        self.assertIn("不是生产地块32696的结论", joined)


def _pages(reader) -> list[str]:
    return [_flat(page.extract_text() or "") for page in reader.pages]


if __name__ == "__main__":
    unittest.main()
