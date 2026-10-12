"""换像元网格时的最近邻重映射（旧经纬度网格 → v5 UTM 网格）。"""

from __future__ import annotations

import copy
import random
import unittest
from unittest.mock import patch

from app.core import harvest_progress as hp
from app.core.harvest_progress import (
    HarvestProgressThresholds,
    compute_harvest_series,
    decode_pixel_states,
)

THR = HarvestProgressThresholds()
GREEN, BARE = 0.85, 0.15
LAT = 40.62
# 约 10m 像元：经度 1.18e-4°、纬度 9e-5°
DLON, DLAT = 1.18e-4, 9e-5


def _grid(nx, ny, *, ox=0.0, oy=0.0, sx=DLON, sy=DLAT):
    return [(111.8 + ox + i * sx, LAT + oy + j * sy) for j in range(ny) for i in range(nx)]


def _scene(day, coords, ndvi, *, clear=None, version=None):
    pixels = []
    for i, (lon, lat) in enumerate(coords):
        v = ndvi(i, lon, lat) if callable(ndvi) else ndvi
        pixels.append(
            {"lon": lon, "lat": lat, "NDVI": v, "clear": 1 if clear is None else clear[i]}
        )
    return {"date": day, "pixels": pixels, "scene_id": f"s_{day}", "algorithm_version": version}


class GridRemapTests(unittest.TestCase):
    def _old_new(self):
        # 旧网格更密（9.3m 步长、偏移 1/4 像元），新网格 10m：旧→新多对一，
        # 修复前会有一批新像元拿不到任何旧网格历史，被当成非作物（255）。
        old = _grid(24, 24, ox=DLON / 4, oy=DLAT / 4, sx=DLON * 0.93, sy=DLAT * 0.93)
        new = _grid(22, 22)
        scenes = [
            _scene("2026-07-01", old, 0.7),
            _scene("2026-08-01", old, GREEN),
            _scene("2026-08-20", old, GREEN),
            _scene("2026-09-10", old, 0.7),
            # 新网格：已收获（裸土），绿度从未≥返青阈值
            _scene("2026-10-01", new, BARE, version="stac-optical-lonlat-v5"),
            _scene("2026-10-06", new, BARE, version="stac-optical-lonlat-v5"),
            _scene("2026-10-11", new, BARE, version="stac-optical-lonlat-v5"),
        ]
        return old, new, scenes

    def test_new_grid_pixels_inherit_old_history(self) -> None:
        _, new, scenes = self._old_new()
        rows = compute_harvest_series(scenes, thresholds=THR, pixel_states=True)
        last = rows[-1]
        self.assertEqual(len(last["pixel_keys"]), len(new))
        states = decode_pixel_states(last["pixel_states"], len(new))
        self.assertEqual(states.count(255), 0)
        self.assertEqual(last["crop_pixel_count"], len(new))
        self.assertEqual(last["harvested_pct"], 100.0)
        t = last["thresholds"]
        self.assertTrue(t["grid_remapped"])
        self.assertEqual(t["grid_count"], 2)
        self.assertEqual(t["grid_remap_match_pct"], 100.0)
        self.assertEqual(t["grid_remap_max_m"], 7.0)

    def test_legacy_alignment_lost_pixels(self) -> None:
        # 证明场景确实复现了 20308 的问题：只用旧对齐逻辑会丢像元。
        _, new, scenes = self._old_new()
        with patch.object(hp, "_remap_grid_changes", return_value=None):
            rows = compute_harvest_series(scenes, thresholds=THR, pixel_states=True)
        states = decode_pixel_states(rows[-1]["pixel_states"], len(new))
        self.assertGreater(states.count(255), 0)

    def test_distance_threshold_configurable(self) -> None:
        _, new, scenes = self._old_new()
        with patch.dict("os.environ", {"HARVEST_GRID_REMAP_MAX_M": "0.5"}):
            rows = compute_harvest_series(scenes, thresholds=THR, pixel_states=True)
        t = rows[-1]["thresholds"]
        self.assertTrue(t["grid_remapped"])
        self.assertLess(t["grid_remap_match_pct"], 20.0)
        self.assertEqual(t["grid_remap_max_m"], 0.5)

    def test_far_pixels_not_mapped(self) -> None:
        # 新网格外扩一圈：超出 7m 的目标像元不硬凑旧值。
        old = _grid(10, 10)
        new = _grid(12, 12, ox=-DLON, oy=-DLAT, sx=DLON, sy=DLAT)
        new = [(lon + 2e-5, lat + 2e-5) for lon, lat in new]  # 偏 ~2.5m，重合率 0
        scenes = [_scene(d, old, GREEN) for d in ("2026-07-01", "2026-08-01", "2026-08-20")]
        scenes += [_scene(d, new, BARE) for d in ("2026-10-01", "2026-10-06")]
        rows = compute_harvest_series(scenes, thresholds=THR, pixel_states=True)
        t = rows[-1]["thresholds"]
        self.assertEqual(t["grid_remap_match_pct"], round(100 * 100 / 144, 1))
        self.assertEqual(rows[-1]["crop_pixel_count"], 100)

    def test_same_grid_identical_to_legacy(self) -> None:
        # 同一网格（仅云掩膜不同）：结果必须与修复前逐位一致，且不带 grid_* 字段。
        rng = random.Random(11)
        coords = _grid(15, 15)
        scenes = []
        for k, day in enumerate(
            ["2026-06-01", "2026-06-20", "2026-07-10", "2026-08-01", "2026-08-20",
             "2026-09-10", "2026-09-20", "2026-09-30", "2026-10-06"]
        ):
            base = [0.3, 0.6, GREEN, GREEN, GREEN, 0.7, 0.4, BARE, BARE][k]
            clear = [1 if rng.random() > 0.15 else 0 for _ in coords]
            scenes.append(
                _scene(day, coords, lambda i, lo, la: max(0.0, min(0.95, base + rng.gauss(0, 0.07))), clear=clear)
            )
        new_rows = compute_harvest_series(copy.deepcopy(scenes), thresholds=THR, pixel_states=True)
        with patch.object(hp, "_remap_grid_changes", return_value=None):
            old_rows = compute_harvest_series(copy.deepcopy(scenes), thresholds=THR, pixel_states=True)
        self.assertEqual(new_rows, old_rows)
        for r in new_rows:
            self.assertNotIn("grid_remapped", r["thresholds"])

    def test_same_grid_no_remap_detected(self) -> None:
        coords = _grid(8, 8)
        days = hp._daily_observations(
            [_scene("2026-08-01", coords, GREEN), _scene("2026-08-10", coords, GREEN,
             clear=[0] * 20 + [1] * 44)],
            THR,
        )
        self.assertIsNone(hp._remap_grid_changes(days))


if __name__ == "__main__":
    unittest.main()
