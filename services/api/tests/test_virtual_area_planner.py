"""VPA10 动态窗口、完整包含和重叠最小化测试。"""

from types import SimpleNamespace

from pyproj import CRS, Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform

from app.services.virtual_area_planner import (
    ExistingArea,
    find_containing_area,
    plan_virtual_areas,
)


def local_land(land_id: str, x: float = 0, y: float = 0, size: float = 100):
    local = CRS.from_proj4(
        "+proj=aeqd +lat_0=35 +lon_0=110 +datum=WGS84 +units=m"
    )
    to_wgs = Transformer.from_crs(local, 4326, always_xy=True)
    geometry = transform(
        to_wgs.transform,
        box(x - size / 2, y - size / 2, x + size / 2, y + size / 2),
    )
    return SimpleNamespace(
        land_id=land_id,
        boundary_geojson=mapping(geometry),
        boundary_srid=4326,
    )


def test_dynamic_center_groups_parcels_that_anchor_centroid_window_misses():
    anchor = local_land("anchor")
    distant = local_land("distant", x=8_500)

    plans = plan_virtual_areas([anchor, distant], anchor_limit=2)

    assert len(plans) == 1
    assert plans[0].land_ids == ("anchor", "distant")
    assert plans[0].window_side_m == 10_000


def test_every_normal_member_is_fully_contained_by_planned_boundary():
    lands = [
        local_land("A"),
        local_land("B", x=3_000, y=2_000),
        local_land("C", x=-2_000, y=-1_500),
    ]

    plans = plan_virtual_areas(lands, anchor_limit=3)

    for plan in plans:
        boundary = plan.boundary_geojson
        for land in lands:
            if land.land_id in plan.land_ids:
                assert boundary["type"] in {"Polygon", "MultiPolygon"}
                assert find_containing_area(
                    land, [ExistingArea("planned", shape(boundary))]
                )


def test_new_window_prefers_minimum_overlap_when_member_count_is_equal():
    existing = local_land("existing", x=0, size=10_000)
    incoming = local_land("incoming", x=9_000)

    plans = plan_virtual_areas(
        [incoming],
        existing_areas=[ExistingArea("old", existing.boundary_geojson)],
        anchor_limit=1,
    )

    assert len(plans) == 1
    assert plans[0].overlap_ratio < 0.02


def test_oversized_land_is_kept_as_single_oversized_plan():
    oversized = local_land("large", size=12_000)

    plans = plan_virtual_areas([oversized])

    assert len(plans) == 1
    assert plans[0].land_ids == ("large",)
    assert plans[0].oversized is True
