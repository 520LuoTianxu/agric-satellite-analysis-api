"""地块边界自动修复：自相交、重复点、有效边界保持不变。"""

from types import SimpleNamespace

from shapely.geometry import MultiPolygon, Polygon, mapping

from app.core.geo import repair_polygon_geometry
from app.services.satellite_batch import satellite_land_geometry

BOWTIE = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])


def test_bowtie_repaired_to_two_triangles():
    assert not BOWTIE.is_valid
    repaired = repair_polygon_geometry(BOWTIE)
    assert repaired is not None and repaired.is_valid
    assert isinstance(repaired, MultiPolygon)
    assert abs(repaired.area - 0.5) < 1e-9


def test_duplicate_consecutive_points_removed():
    poly = Polygon([(0, 0), (0, 0), (2, 0), (2, 2), (2, 2), (0, 2), (0, 0)])
    repaired = repair_polygon_geometry(poly)
    assert repaired is not None and repaired.is_valid
    assert abs(repaired.area - 4) < 1e-9


def test_valid_polygon_unchanged():
    poly = Polygon([(0, 0), (3, 0), (3, 3), (0, 3), (0, 0)])
    assert repair_polygon_geometry(poly) is poly


def test_zero_area_rejected():
    line_like = Polygon([(0, 0), (1, 1), (2, 2), (0, 0)])
    assert repair_polygon_geometry(line_like) is None


def test_satellite_land_geometry_accepts_bowtie():
    land = SimpleNamespace(land_id="x", boundary_geojson=mapping(BOWTIE), boundary_srid=4326)
    geom = satellite_land_geometry(land)
    assert geom.is_valid and geom.area > 0
