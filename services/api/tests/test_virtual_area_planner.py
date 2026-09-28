"""VPA10 动态窗口、完整包含和重叠最小化测试。"""

from types import SimpleNamespace
import heapq

from pyproj import CRS, Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform

from app.services import virtual_area_planner
from app.services.virtual_area_planner import (
    ExistingArea,
    PlannerParcel,
    _select_anchor_ids,
    find_containing_area,
    plan_virtual_areas,
)


def local_land(land_id: str, x: float = 0, y: float = 0, size: float = 100):
    local = CRS.from_proj4("+proj=aeqd +lat_0=35 +lon_0=110 +datum=WGS84 +units=m")
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


def test_anchor_priority_is_stable_when_many_equal_scarcity_lands_leave_the_heap():
    lands = [
        local_land(f"land-{index:02d}", x=20_000 * index, size=100)
        for index in range(16)
    ]

    forward = plan_virtual_areas(lands)
    reverse = plan_virtual_areas(list(reversed(lands)))

    forward_signature = [
        (plan.anchor_land_id, plan.land_ids, plan.aggregation_bbox) for plan in forward
    ]
    reverse_signature = [
        (plan.anchor_land_id, plan.land_ids, plan.aggregation_bbox) for plan in reverse
    ]
    assert forward_signature == reverse_signature
    assert sorted(land_id for plan in forward for land_id in plan.land_ids) == sorted(
        land.land_id for land in lands
    )


def test_anchor_heap_discards_stale_entries_and_keeps_land_id_tie_break():
    remaining = {
        land_id: PlannerParcel(land_id, box(0, 0, 1, 1))
        for land_id in ("land-a", "land-b")
    }
    versions = {"land-a": 0, "land-b": 1, "removed": 0}
    # removed优先级最高但已不在集合；land-b旧版本也比当前版本优先级更高。
    anchor_heap = [
        (-5.0, "removed", 0),
        (-3.0, "land-b", 0),
        (-1.0, "land-a", 0),
        (-1.0, "land-b", 1),
    ]
    heapq.heapify(anchor_heap)

    first = _select_anchor_ids(remaining, anchor_heap, versions, anchor_limit=2)
    second = _select_anchor_ids(remaining, anchor_heap, versions, anchor_limit=2)

    assert first == ["land-a", "land-b"]
    assert second == first


def test_candidates_are_reused_when_an_anchor_local_snapshot_is_unchanged(monkeypatch):
    lands = [
        local_land(f"land-{index:02d}", x=30_000 * index, size=100)
        for index in range(12)
    ]
    original = virtual_area_planner._candidate_for_anchor
    calls = 0

    def counting_candidate(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        virtual_area_planner, "_candidate_for_anchor", counting_candidate
    )
    plans = plan_virtual_areas(lands)

    assert len(plans) == len(lands)
    assert calls == len(lands)
