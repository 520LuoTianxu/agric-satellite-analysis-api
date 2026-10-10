"""季外高云 S2 景：下载前 SCL 地块晴空预判 > S2_PARCEL_CLEAR_KEEP_PCT 时按地块保留。"""

import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping

from app.core import s2_parcel_clear as pc
from app.core.decloud import filter_scenes_outside_season_high_cloud
from app.tasks import satellite_batch as batch
from tests.test_satellite_batch import make_lands

SEASON = (6, 7, 8, 9)


def _scene(day, cloud, **kw):
    return {"date": day, "cloud_cover": cloud, "id": f"S2_{day}", **kw}


class KeepPctConfigTests(unittest.TestCase):
    def test_env(self):
        cases = {"": 60.0, "75": 75.0, "off": None, "0": None, "-5": None, "x": 60.0, "nan": 60.0, "120": 100.0}
        for raw, want in cases.items():
            with patch.dict(os.environ, {"S2_PARCEL_CLEAR_KEEP_PCT": raw}):
                self.assertEqual(pc.parcel_clear_keep_pct(), want, raw)

    def test_clear_pct_from_scl(self):
        self.assertEqual(pc.clear_pct_from_scl(np.array([4, 5, 6, 7, 8, 9, 10, 3, 0, 0])), 50.0)
        self.assertIsNone(pc.clear_pct_from_scl(np.array([0, 0, 255])))
        self.assertIsNone(pc.clear_pct_from_scl(np.array([])))
        # 1/2/11（饱和/暗像元/雪）不算晴空，但计入分母。
        self.assertEqual(pc.clear_pct_from_scl(np.array([4, 1, 2, 11])), 25.0)


class FilterRuleTests(unittest.TestCase):
    def test_out_of_season_high_cloud_kept_only_when_parcel_clear_above_threshold(self):
        oct4 = _scene(date(2026, 10, 4), 63.2)
        keep = lambda clear: filter_scenes_outside_season_high_cloud(  # noqa: E731
            [oct4], season_months=SEASON, parcel_clear_pct=clear, parcel_clear_keep_pct=60.0
        )[0]
        self.assertEqual(keep(91.1), [oct4])
        self.assertEqual(keep(60.0), [])  # 严格大于
        self.assertEqual(keep(0.0), [])
        self.assertEqual(keep(None), [])  # 预判失败 → 原规则
        kept, skipped = filter_scenes_outside_season_high_cloud(
            [oct4], season_months=SEASON, parcel_clear_pct=99.0, parcel_clear_keep_pct=None
        )
        self.assertEqual((kept, skipped), ([], 1))  # 功能关闭

    def test_in_season_and_low_cloud_unchanged(self):
        sep = _scene(date(2026, 9, 29), 33.9)
        clear_oct = _scene(date(2026, 10, 6), 0.0)
        for clear in (None, 0.0, 100.0):
            kept, _ = filter_scenes_outside_season_high_cloud(
                [sep, clear_oct], season_months=SEASON, parcel_clear_pct=clear, parcel_clear_keep_pct=60.0
            )
            self.assertEqual(kept, [sep, clear_oct])


def _write_scl(path, arr, x0, y0, res=20.0):
    with rasterio.open(
        path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
        dtype="uint8", crs="EPSG:32649", transform=from_origin(x0, y0, res, res), nodata=0,
    ) as ds:
        ds.write(arr, 1)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        from pyproj import Transformer

        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "scl.tif")
        # 200×200 像元 20 m：左半晴（4），右半云（9）。
        arr = np.full((200, 200), 4, dtype=np.uint8)
        arr[:, 100:] = 9
        self.x0, self.y0 = 500000.0, 4500000.0
        _write_scl(self.path, arr, self.x0, self.y0)
        to_ll = Transformer.from_crs("EPSG:32649", "EPSG:4326", always_xy=True).transform

        def parcel(xa, xb, ya, yb):
            (lon0, lat0), (lon1, lat1) = to_ll(xa, ya), to_ll(xb, yb)
            return box(min(lon0, lon1), min(lat0, lat1), max(lon0, lon1), max(lat0, lat1))

        self.clear_parcel = parcel(self.x0 + 200, self.x0 + 1000, self.y0 - 1000, self.y0 - 200)
        self.cloud_parcel = parcel(self.x0 + 3000, self.x0 + 3800, self.y0 - 1000, self.y0 - 200)
        self.half_parcel = parcel(self.x0 + 1600, self.x0 + 2400, self.y0 - 1000, self.y0 - 200)

    def tearDown(self):
        self.tmp.cleanup()

    def test_per_parcel_clear_from_one_window(self):
        out = pc.probe_parcel_clear(
            self.path, {"clear": self.clear_parcel, "cloud": self.cloud_parcel, "half": self.half_parcel}
        )
        self.assertGreater(out["clear"], 95)
        self.assertLess(out["cloud"], 5)
        self.assertTrue(30 < out["half"] < 70, out["half"])

    def test_failures_fall_back_to_empty(self):
        self.assertEqual(pc.probe_parcel_clear(None, {"a": self.clear_parcel}), {})
        self.assertEqual(pc.probe_parcel_clear(self.path, {}), {})
        self.assertEqual(
            pc.probe_parcel_clear(os.path.join(self.tmp.name, "missing.tif"), {"a": self.clear_parcel}), {}
        )
        far = box(10, 10, 10.01, 10.01)  # 不在栅格范围内
        out = pc.probe_parcel_clear(self.path, {"far": far})
        self.assertIsNone(out.get("far"))  # 空窗口：{} 或 None，均退回原规则


class BatchSelectionTests(unittest.TestCase):
    """多地块批次：每个地块独立判定，只有晴空地块获得这一景。"""

    def _scene(self, day, cloud):
        return {
            "id": f"S2_{day}",
            "date": day,
            "cloud_cover": cloud,
            "geometry": mapping(box(109.9, 34.9, 110.1, 35.1)),
            "band_hrefs": {"SCL": "s3://scl.tif"},
        }

    def _lands(self):
        lands = make_lands()
        for land in lands:
            land["season_months"] = SEASON
        return lands

    def test_out_of_season_cloudy_scene_kept_per_parcel(self):
        lands = self._lands()
        scene = self._scene(date(2026, 10, 4), 63.2)
        with patch.object(batch, "probe_parcel_clear", return_value={"A": 91.1, "B": 12.0}) as probe:
            batch._prefetch_parcel_clear(scene, lands, "S2")
            batch._prefetch_parcel_clear(scene, lands, "S2")  # 已缓存，不重复读取
        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[0], "s3://scl.tif")
        self.assertEqual(sorted(probe.call_args.args[1]), ["A", "B"])
        selected = batch._scene_lands(scene, lands, "S2")
        self.assertEqual([land["meta"]["land_id"] for land in selected], ["A"])

    def test_probe_failure_keeps_original_drop(self):
        lands = self._lands()
        scene = self._scene(date(2026, 10, 9), 82.9)
        with patch.object(batch, "probe_parcel_clear", return_value={}):
            batch._prefetch_parcel_clear(scene, lands, "S2")
        self.assertEqual(batch._scene_lands(scene, lands, "S2"), [])

    def test_no_probe_for_in_season_low_cloud_s1_or_disabled(self):
        lands = self._lands()
        cases = [
            (self._scene(date(2026, 9, 29), 33.9), "S2", {}),  # 季内
            (self._scene(date(2026, 10, 6), 0.0), "S2", {}),  # 季外低云
            (self._scene(date(2026, 10, 4), 63.2), "S1", {}),
            (self._scene(date(2026, 10, 4), 63.2), "S2", {"S2_PARCEL_CLEAR_KEEP_PCT": "off"}),
        ]
        for scene, sensor, env in cases:
            with patch.dict(os.environ, env), patch.object(batch, "probe_parcel_clear") as probe:
                batch._prefetch_parcel_clear(scene, lands, sensor)
            probe.assert_not_called()
        # 季内/季外低云仍全部保留；关闭时季外高云仍丢弃。
        self.assertEqual(len(batch._scene_lands(cases[0][0], lands, "S2")), 2)
        self.assertEqual(len(batch._scene_lands(cases[1][0], lands, "S2")), 2)
        with patch.dict(os.environ, {"S2_PARCEL_CLEAR_KEEP_PCT": "off"}):
            self.assertEqual(batch._scene_lands(cases[3][0], lands, "S2"), [])

    def test_only_parcels_that_would_be_dropped_are_probed(self):
        lands = self._lands()
        lands[1]["season_months"] = (6, 7, 8, 9, 10)  # B 的轮作季含 10 月：原规则已保留
        lands[1]["existing"] = set()
        scene = self._scene(date(2026, 10, 4), 63.2)
        with patch.object(batch, "probe_parcel_clear", return_value={"A": 10.0}) as probe:
            batch._prefetch_parcel_clear(scene, lands, "S2")
        self.assertEqual(sorted(probe.call_args.args[1]), ["A"])
        self.assertEqual([x["meta"]["land_id"] for x in batch._scene_lands(scene, lands, "S2")], ["B"])


if __name__ == "__main__":
    unittest.main()
