"""项目 COG 读取：整像元对齐网格上 nearest 读取须与源像元逐一相等（snap_window）。"""

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from app.tasks.pipeline import _read_band_windowed_profiled


def test_snap_window_keeps_pixels_registered(tmp_path):
    src = tmp_path / "b08.tif"
    data = np.arange(400 * 400, dtype=np.uint16).reshape(400, 400)
    with rasterio.open(src, "w", driver="GTiff", width=400, height=400, count=1, dtype="uint16",
                       crs="EPSG:32650", transform=from_origin(300000, 3800040, 10, 10)) as ds:
        ds.write(data, 1)
    x0, y1, w, h = 300000 + 10 * 57, 3800040 - 10 * 91, 120, 80
    tr = from_origin(x0, y1, 10, 10)
    bounds = transform_bounds("EPSG:32650", "EPSG:4326", x0, y1 - 10 * h, x0 + 10 * w, y1, densify_pts=21)
    out = _read_band_windowed_profiled(str(src), bounds, (h, w), tr, target_crs="EPSG:32650",
                                       resampling=Resampling.nearest, snap_window=True).value
    assert np.array_equal(out, data[91 : 91 + h, 57 : 57 + w].astype(np.float32))
