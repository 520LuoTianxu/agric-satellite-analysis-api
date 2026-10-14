"""项目收获分析视图层（纯函数）。"""

from __future__ import annotations

from agric_satellite_analysis_common import project_harvest as ph

from app.services import project_harvest_views as v


def _stat(lid, d, pick, high=1.0, med=0.5, crop=10.0, cloud=0.0, noncrop=0.0, block="b1"):
    row = ph.parcel_date_row({"high": high, "medium": med, "low": 0.0, "unharvested": crop - high - med - cloud,
                              "cloud": cloud, "noncrop": noncrop}, crop + noncrop, crop + noncrop)
    return {"land_id": lid, "obs_date": d, "block_key": block, "is_pick": pick, **row}


META = {
    "1": {"land_id": "1", "land_status": "1", "town_name": "A镇", "village_name": "甲村", "land_name": "一号"},
    "2": {"land_id": "2", "land_status": "0", "town_name": "A镇", "village_name": "乙村", "land_name": "二号"},
}
STATS = [
    _stat("1", "2026-10-10", False, high=0.0, med=0.0),
    _stat("1", "2026-10-13", True, high=4.0, med=1.0),
    _stat("2", "2026-10-10", True, high=2.0, med=0.0),
    _stat("2", "2026-10-13", False, high=3.0, med=0.0, cloud=5.0),
]


def test_summary_pick_and_date():
    sums = ph.build_summaries(STATS, META, "2026-10-13")
    s = v.summary_view(sums, None, "2026-10-13", {"2026-10-13": 1, "2026-10-10": 1})
    assert s["date"] == "pick"
    assert s["totals"]["parcel_count"] == 2
    assert abs(s["totals"]["harvested_mu"] - 7.0) < 1e-6
    assert {x["key"] for x in s["by_status"]} == {"0", "1"}
    d = v.summary_view(sums, "2026-10-10", "2026-10-13")
    assert abs(d["totals"]["harvested_mu"] - 2.0) < 1e-6
    assert [p["date"] for p in s["series"]] == ["2026-10-10", "2026-10-13"]


def test_parcel_rows_fallback_and_paging():
    rows = v.parcel_rows(STATS, META, "pick")
    by = {r["land_id"]: r for r in rows}
    assert by["2"]["fallback"] is True and by["1"]["fallback"] is False
    page = v.filter_sort_page(rows, sort="harvested_pct", order="desc", limit=1)
    assert page["total"] == 2 and page["items"][0]["land_id"] == "1"
    assert v.filter_sort_page(rows, q="乙村")["total"] == 1
    assert v.filter_sort_page(rows, status="1")["items"][0]["land_id"] == "1"


def test_geojson_layers_dedup():
    sq = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
    dd = [{"land_id": "1", "kept": True, "clipped_geojson": sq, "clipped_area_mu": 10, "priority_rank": 1},
          {"land_id": "3", "kept": False, "merged_reason": "sliver", "clipped_area_mu": 0.1, "priority_rank": 2}]
    rows = {r["land_id"]: r for r in v.parcel_rows(STATS, META, "pick")}
    g = v.geojson_view(dd, rows)
    assert len(g["features"]) == 1 and g["features"][0]["properties"]["dominant"] == "unharvested"
    lay = v.layers_view([{"block_key": "b1", "obs_date": "2026-10-13", "kind": "tier", "format": "png", "oss_key": "k",
                          "west": 0, "south": 0, "east": 1, "north": 1, "width": 2, "height": 2}],
                        [{"key": "b1"}, {"key": "b2", "bbox4326": [0, 0, 1, 1]}], "2026-10-13", "tier", lambda k: "u/" + k)
    assert lay["items"][0]["coordinates"][0] == [0.0, 1.0] and lay["items"][1]["available"] is False
    dv = v.dedup_view(dd)
    assert dv["kept"] == 1 and dv["merged"] == 1


def test_overview_without_run():
    o = v.overview_view(None)
    assert o["available"] is False and len(o["legend"]) >= 6
