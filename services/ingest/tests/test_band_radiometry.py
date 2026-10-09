"""S2 波段辐射定标：Earth Search 已扣 BOA 偏移的条目不得重复扣除。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from app.tasks.agri_lonlat import agri_optical_index_defs
from app.tasks.indices import get_index
from app.tasks.pipeline import _boa_offset_already_applied, _resolve_band_radiometry

_RB = [{"scale": 0.0001, "offset": -0.1, "nodata": 0}]


def _item(properties: dict, raster_bands=_RB):
    defs = agri_optical_index_defs()
    assets = {}
    for d in defs:
        for band in d.bands:
            for name in d.stac_asset_map.get(band, (band,)):
                extra = {"raster:bands": raster_bands} if raster_bands else {}
                assets[name] = SimpleNamespace(
                    href=f"s3://x/{name}.tif", extra_fields=extra
                )
    return SimpleNamespace(properties=properties, assets=assets), defs


class BoaOffsetTests(unittest.TestCase):
    def test_earth_search_l2a_offset_not_applied_twice(self) -> None:
        item, defs = _item(
            {"s2:processing_baseline": "05.12", "earthsearch:boa_offset_applied": True}
        )
        radiometry, source, sources, reason = _resolve_band_radiometry(item, defs)
        self.assertIsNone(reason)
        self.assertTrue(radiometry)
        for band, cal in radiometry.items():
            self.assertEqual(cal["offset"], 0.0, band)
            self.assertEqual(cal["scale"], 0.0001, band)
            self.assertTrue(sources[band].endswith("+boa_offset_already_applied"))

    def test_unharmonized_items_keep_offset(self) -> None:
        # PC / Earth Search c1：DN 仍含 +1000，必须扣 0.1。
        for props in (
            {"s2:processing_baseline": "05.12"},
            {
                "s2:processing_baseline": "05.12",
                "earthsearch:boa_offset_applied": False,
            },
        ):
            item, defs = _item(props)
            radiometry, _, sources, reason = _resolve_band_radiometry(item, defs)
            self.assertIsNone(reason)
            self.assertTrue(all(c["offset"] == -0.1 for c in radiometry.values()))
            self.assertFalse(any("already_applied" in s for s in sources.values()))

    def test_baseline_fallback_also_respects_flag(self) -> None:
        item, defs = _item(
            {
                "s2:processing_baseline": "04.00",
                "earthsearch:boa_offset_applied": "true",
            },
            raster_bands=None,
        )
        radiometry, _, sources, reason = _resolve_band_radiometry(item, defs)
        self.assertIsNone(reason)
        self.assertTrue(all(c["offset"] == 0.0 for c in radiometry.values()))

    def test_flag_parsing(self) -> None:
        f = _boa_offset_already_applied
        self.assertTrue(
            f(SimpleNamespace(properties={"earthsearch:boa_offset_applied": True}))
        )
        self.assertTrue(
            f(SimpleNamespace(properties={"earthsearch:boa_offset_applied": "TRUE"}))
        )
        self.assertFalse(
            f(SimpleNamespace(properties={"earthsearch:boa_offset_applied": "no"}))
        )
        self.assertFalse(f(SimpleNamespace(properties={})))
        self.assertFalse(f(SimpleNamespace()))

    def test_real_dn_example_gives_physical_ndvi(self) -> None:
        # 2026-07-15 地块 61251 中心像元：Earth Search l2a DN（已扣偏移）红 260、近红外 4175；
        # 同一景未协调 DN（PC / c1）为 1260 / 5175。两条路径应得到同一 NDVI≈0.88。
        ndvi = get_index("ndvi").formula
        es_item, defs = _item(
            {"s2:processing_baseline": "05.12", "earthsearch:boa_offset_applied": True}
        )
        pc_item, _ = _item({"s2:processing_baseline": "05.12"}, raster_bands=None)
        es_rad = _resolve_band_radiometry(es_item, defs)[0]
        pc_rad = _resolve_band_radiometry(pc_item, defs)[0]
        es = ndvi(
            {"B04": np.array([260.0]), "B08": np.array([4175.0])},
            band_radiometry=es_rad,
        )
        pc = ndvi(
            {"B04": np.array([1260.0]), "B08": np.array([5175.0])},
            band_radiometry=pc_rad,
        )
        self.assertAlmostEqual(float(es[0]), 0.8828, places=3)
        self.assertAlmostEqual(float(es[0]), float(pc[0]), places=6)


if __name__ == "__main__":
    unittest.main()
