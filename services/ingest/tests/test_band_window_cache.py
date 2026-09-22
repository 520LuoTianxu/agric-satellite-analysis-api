"""共享卫星窗口缓存 v2 的键校验、原子读写与 LRU 测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.core.band_window_cache import (
    prune_window_cache,
    read_scene_window,
    write_scene_window,
)


class BandWindowCacheTests(unittest.TestCase):
    transform = (0.0001, 0.0, 110.0, 0.0, -0.0001, 35.0, 0.0, 0.0, 1.0)
    shape = (4, 5)

    def _env(self, root: str) -> dict[str, str]:
        return {
            "BAND_WINDOW_CACHE": "1",
            "BAND_WINDOW_CACHE_DIR": root,
            "BAND_WINDOW_CACHE_MAX_GB": "1",
            "BAND_WINDOW_CACHE_MIN_FREE_GB": "0",
            "BAND_WINDOW_CACHE_PRUNE_INTERVAL_SEC": "0",
        }

    def test_round_trip_ignores_rotating_query_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, self._env(tmp)
        ):
            arrays = {"B04": np.arange(20, dtype=np.float32).reshape(self.shape)}
            path = write_scene_window(
                scene_id="scene",
                sensor="S2",
                target_shape=self.shape,
                target_transform=self.transform,
                band_hrefs={"B04": "https://example.test/red.tif?sig=first"},
                arrays=arrays,
            )
            self.assertIsNotNone(path)
            loaded = read_scene_window(
                scene_id="scene",
                sensor="S2",
                target_shape=self.shape,
                target_transform=self.transform,
                band_hrefs={"B04": "https://example.test/red.tif?sig=second"},
            )
        self.assertIsNotNone(loaded)
        np.testing.assert_array_equal(loaded["B04"], arrays["B04"])

    def test_changed_grid_or_source_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, self._env(tmp)
        ):
            arrays = {"vv": np.ones(self.shape, dtype=np.float32)}
            write_scene_window(
                scene_id="scene",
                sensor="S1",
                target_shape=self.shape,
                target_transform=self.transform,
                band_hrefs={"vv": "https://example.test/vv.tif"},
                arrays=arrays,
            )
            changed = read_scene_window(
                scene_id="scene",
                sensor="S1",
                target_shape=self.shape,
                target_transform=self.transform,
                band_hrefs={"vv": "https://example.test/vv-new.tif"},
            )
            resized = read_scene_window(
                scene_id="scene",
                sensor="S1",
                target_shape=(2, 10),
                target_transform=self.transform,
                band_hrefs={"vv": "https://example.test/vv.tif"},
            )
        self.assertIsNone(changed)
        self.assertIsNone(resized)

    def test_lru_removes_oldest_file_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, self._env(tmp)
        ):
            first = write_scene_window(
                scene_id="old",
                sensor="S2",
                target_shape=self.shape,
                target_transform=self.transform,
                band_hrefs={"B04": "https://example.test/old.tif"},
                arrays={"B04": np.ones(self.shape, dtype=np.float32)},
            )
            second = write_scene_window(
                scene_id="new",
                sensor="S2",
                target_shape=self.shape,
                target_transform=self.transform,
                band_hrefs={"B04": "https://example.test/new.tif"},
                arrays={"B04": np.full(self.shape, 2, dtype=np.float32)},
            )
            assert first is not None and second is not None
            os.utime(first, (1, 1))
            os.utime(second, (2, 2))
            largest = max(first.stat().st_size, second.stat().st_size)
            os.environ["BAND_WINDOW_CACHE_MAX_GB"] = str(
                (largest * 1.2) / (1024**3)
            )
            result = prune_window_cache(force=True)

            self.assertEqual(result["removed"], 1)
            self.assertFalse(Path(first).exists())
            self.assertTrue(Path(second).exists())


if __name__ == "__main__":
    unittest.main()
