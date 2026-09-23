"""项目区压缩像素资产的上传/恢复回环测试。"""

from datetime import date
from unittest import TestCase
from unittest.mock import patch

import numpy as np
from rasterio.transform import Affine

from app.core import virtual_area_cache as cache


class _MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        self.objects[key] = data
        return key

    def get_bytes(self, key: str) -> bytes:
        return self.objects[key]


class VirtualAreaCacheTests(TestCase):
    def test_s1_pixel_json_round_trip_keeps_grid_and_scene_metadata(self) -> None:
        storage = _MemoryStorage()
        uploaded_assets: list[dict] = []
        grid = (
            Affine(0.1, 0, 110, 0, -0.1, 35),
            (2, 2),
            None,
            (110.0, 34.8, 110.2, 35.0),
        )
        scene = {
            "id": "S1-demo",
            "date": date(2026, 1, 2),
            "cloud_cover": 12.5,
            "relative_orbit": 42,
            "geometry": {"type": "Polygon", "coordinates": []},
            "band_hrefs": {
                "vv": "https://example/vv.tif",
                "vh": "https://example/vh.tif",
            },
        }

        def remember_asset(tile_id: str, **kwargs):
            uploaded_assets.append({"tile_id": tile_id, **kwargs})

        with (
            patch.object(cache, "get_storage", return_value=storage),
            patch.object(
                cache, "upsert_virtual_area_asset", side_effect=remember_asset
            ),
        ):
            result = cache.upload_virtual_area_scene(
                tile_id="vpa10_demo",
                sensor="S1",
                scene=scene,
                grid=grid,
                bands={
                    "vv": np.asarray([[1, 2], [3, 4]], dtype=np.float32),
                    "vh": np.asarray([[5, 6], [7, 8]], dtype=np.float32),
                },
            )
            restored_scene, arrays, restored_grid = cache.load_virtual_area_scene(
                {"oss_key": result["pixel_oss_key"], "scene_date": "2026-01-02"}
            )

        self.assertGreaterEqual(len(uploaded_assets), 1)
        self.assertEqual(restored_scene["cloud_cover"], 12.5)
        self.assertEqual(restored_scene["relative_orbit"], 42)
        self.assertEqual(restored_scene["band_hrefs"]["vv"], scene["band_hrefs"]["vv"])
        self.assertEqual(restored_grid[1], (2, 2))
        np.testing.assert_array_equal(
            arrays["vv"], np.asarray([[1, 2], [3, 4]], dtype=np.float32)
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
