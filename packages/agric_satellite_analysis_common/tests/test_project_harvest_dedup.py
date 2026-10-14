"""项目收获去重：status=1 优先，再 land_id 大者优先；碎片/异常边界合并。"""

from agric_satellite_analysis_common import project_harvest as ph

D = 0.001  # ~100 m


def sq(x, y, w, h=None):
    h = w if h is None else h
    return {"type": "Polygon", "coordinates": [[[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]]]}


def test_priority_and_merge_reasons():
    lands = [
        {"land_id": 10, "land_status": "0", "boundary_geojson": sq(115.0, 33.0, 3 * D)},
        {"land_id": 5, "land_status": "1", "boundary_geojson": sq(115.0, 33.0, 2 * D)},  # status=1 优先于更大 id
        {"land_id": 7, "land_status": "1", "boundary_geojson": sq(115.0, 33.0, 2 * D)},  # 与 5 重合，7 优先
        {"land_id": 8, "land_status": "0", "boundary_geojson": sq(115.1, 33.0, 0.2)},  # 跨度 > 0.05° 异常
    ]
    out = ph.dedup_parcels(lands)
    rows = {str(r["land_id"]): r for r in out["rows"]}
    assert [str(r["land_id"]) for r in out["rows"]][:2] == ["7", "5"]
    assert rows["7"]["kept"] and not rows["5"]["kept"]
    assert rows["10"]["kept"] and rows["10"]["clipped_area_mu"] < rows["10"]["original_area_mu"]
    assert rows["8"]["merged_reason"] == "invalid_boundary"
    assert out["hash"] == ph.dedup_parcels(list(reversed(lands)))["hash"]
