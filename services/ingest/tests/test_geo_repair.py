"""下载侧边界修复与 API 侧保持一致。"""

from shapely.geometry import Polygon

from app.core.geo import repair_polygon_geometry


def test_bowtie_repaired():
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    repaired = repair_polygon_geometry(bowtie)
    assert repaired is not None and repaired.is_valid and repaired.area > 0


def test_duplicate_points_and_valid_unchanged():
    dup = Polygon([(0, 0), (0, 0), (2, 0), (2, 2), (0, 2), (0, 0)])
    assert abs(repair_polygon_geometry(dup).area - 4) < 1e-9
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    assert repair_polygon_geometry(square) is square
