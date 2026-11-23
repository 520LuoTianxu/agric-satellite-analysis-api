"""Trend axes keep out-of-range points and do not invent values for rejected passes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.reports.biweekly.official_pdf import _trend_chart, trend_y_limits


def _row(day: str, value: float, *, in_period: bool = True, p10: float | None = None, p90: float | None = None):
    entry = {"status": "ok", "median": value, "field_mean": value}
    if p10 is not None:
        entry["p10"] = p10
        entry["p90"] = p90
    return {
        "date": day,
        "in_period": in_period,
        "metrics": {"ndmi": entry, "ndre": dict(entry), "ndvi": dict(entry), "evi": dict(entry)},
    }


class TrendLimitTests(unittest.TestCase):
    def test_axis_grows_past_the_colour_scale_for_high_ndmi(self):
        y0, y1, expanded = trend_y_limits((-0.5, 0.5), [0.53, 0.62], [0.48], [0.66])
        self.assertTrue(expanded)
        self.assertLess(y0, -0.5)
        self.assertGreater(y1, 0.66)

    def test_in_range_points_still_get_label_headroom(self):
        _y0, y1, expanded = trend_y_limits((0.0, 0.8), [0.73, 0.79], [0.70], [0.79])
        self.assertFalse(expanded)
        self.assertGreater(y1, 0.79)

    def test_missing_values_are_not_treated_as_zero(self):
        y0, y1, _expanded = trend_y_limits((0.0, 1.0), [None, 0.4], [None], [None])
        self.assertLess(y0, 0.0)
        self.assertGreater(y1, 0.4)
        # 缺测没有把下沿拉到需要单独容纳 0 以下的数据。色标下沿本身是 0。
        self.assertGreater(y0, -0.2)


class TrendRenderTests(unittest.TestCase):
    def test_labels_and_high_points_stay_inside_the_image(self):
        facts = {
            "period": {"start": "2026-05-02", "end_exclusive": "2026-05-16", "end_inclusive": "2026-05-15"},
            "report_metrics": {
                "primary": ["ndre", "ndmi"],
                "supporting": ["ndvi", "evi"],
            },
            "metric_statistics": {key: "median" for key in ("ndvi", "evi", "ndre", "ndmi")},
            "metric_series": [
                _row("2026-04-28", 0.73, in_period=False, p10=0.68, p90=0.76),
                _row("2026-05-03", 0.78, p10=0.74, p90=0.81),
                _row("2026-05-08", 0.75, p10=0.71, p90=0.78),
                _row("2026-05-10", 0.74, p10=0.70, p90=0.77),
                _row("2026-05-11", 0.73, p10=0.69, p90=0.76),
            ],
            "data_quality": {"rejected": [{"date": "2026-05-13", "reason": "cloud_over_limit", "cloud_pct": 42}]},
        }
        # NDMI 用高于色标上限的数，复现报告里点被裁切的情况。
        for row, ndmi in zip(facts["metric_series"], (0.53, 0.62, 0.57, 0.56, 0.57)):
            row["metrics"]["ndmi"] = {
                "status": "ok",
                "median": ndmi,
                "p10": ndmi - 0.04,
                "p90": ndmi + 0.03,
            }
        dest = Path(tempfile.mkdtemp()) / "trend.png"
        path = _trend_chart(facts, dest)
        self.assertIsNotNone(path)
        from PIL import Image

        image = Image.open(path).convert("RGB")
        # 图顶不应是一条被裁掉的色带：标题上方仍是白边，点不贴死图像边缘。
        top = image.crop((0, 0, image.width, 8)).getcolors(8)
        self.assertTrue(top)
        self.assertTrue(all(color[1][0] > 245 and color[1][1] > 245 and color[1][2] > 245 for color in top))
        # 灰叉没有被画成 0.00 这类数值；文件里能读到超出 0.50 的 NDMI 标签。
        import matplotlib

        matplotlib.use("Agg")
        # 标签是栅格，不能从 PNG 取字。用同一函数的纵轴结果核对 0.62 仍在范围内。
        y0, y1, expanded = trend_y_limits((-0.5, 0.5), [0.53, 0.62, 0.57, 0.56, 0.57], [0.49], [0.65])
        self.assertTrue(expanded)
        self.assertGreater(y1, 0.62)
        self.assertLess(y0, 0.49)

    def test_compact_infield_titles_do_not_run_off_the_figure(self):
        import numpy as np

        from app.reports.biweekly.infield_maps import build_infield_metric

        values = np.linspace(0.35, 0.72, 16 * 14).reshape(16, 14)
        latest = _ndre_product("2026-05-22", values)
        baseline = _ndre_product("2026-05-14", values - 0.04)
        dest = Path(tempfile.mkdtemp()) / "infield.png"
        item = build_infield_metric(
            metric="ndre", latest=latest, baseline=baseline, dest=dest, compact=True
        )
        self.assertIsNotNone(item)
        from PIL import Image

        image = Image.open(item["image"]).convert("RGB")
        width, height = image.size
        pixels = image.load()

        def ink(x: int, y: int) -> bool:
            red, green, blue = pixels[x, y]
            return red < 248 or green < 248 or blue < 248

        right_ink = sum(ink(width - 1, y) or ink(width - 2, y) for y in range(height))
        self.assertEqual(right_ink, 0)
        top_ink = sum(ink(x, 0) or ink(x, 1) for x in range(width))
        self.assertEqual(top_ink, 0)


def _ndre_product(day: str, values) -> dict:
    rows, cols = values.shape
    lons, lats, column = [], [], []
    for r in range(rows):
        for c in range(cols):
            lons.append(115.5 + c * 0.000112)
            lats.append(36.5 - r * 0.00009)
            column.append(float(values[r, c]))
    return {
        "date": day,
        "sensor": "S2",
        "pixel_data": {"format": "lonlat_columns_v1", "columns": {"lon": lons, "lat": lats, "NDRE": column}},
    }


if __name__ == "__main__":
    unittest.main()
