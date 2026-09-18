"""验证地块5×5公里窗口和完整边界合并规则。"""

from __future__ import annotations

from pyproj import Transformer
import pytest
from shapely.geometry import box
from shapely.ops import transform

from app.core.processing_window import build_processing_window


def test_processing_window_is_five_kilometres_in_local_projection():
    land = box(110.0, 35.0, 110.001, 35.001)
    window = build_processing_window(land, 5)
    center = land.centroid
    to_local = Transformer.from_crs(
        "EPSG:4326",
        f"+proj=aeqd +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m",
        always_xy=True,
    ).transform
    local = transform(to_local, window)

    assert local.bounds[2] - local.bounds[0] == pytest.approx(5000)
    assert local.bounds[3] - local.bounds[1] == pytest.approx(5000)


def test_partial_parcel_is_not_merged_into_shared_window():
    anchor = box(110.0, 35.0, 110.001, 35.001)
    window = build_processing_window(anchor, 5)
    from_local = Transformer.from_crs(
        f"+proj=aeqd +lat_0={anchor.centroid.y} +lon_0={anchor.centroid.x} "
        "+datum=WGS84 +units=m",
        "EPSG:4326",
        always_xy=True,
    ).transform
    partial = transform(from_local, box(2400, -200, 2800, 200))

    assert window.covers(anchor)
    assert not window.covers(partial)
