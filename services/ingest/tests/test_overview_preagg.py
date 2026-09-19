from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_overview_accumulator_aggregates_without_retaining_raw_batches() -> None:
    from app.core.overview_preagg import OverviewAccumulator

    accumulator = OverviewAccumulator(
        window_from="2026-07-20",
        window_to="2026-09-18",
        crop=None,
    )
    accumulator.add_batch(
        [
            {
                "land_id": "L1",
                "area_mu": 12,
                "province_code": "11",
                "province_name": "北京",
                "city_code": "1101",
                "city_name": "北京市",
                "county_code": "110101",
                "county_name": "东城区",
                "s2": {
                    "date": "2026-07-21",
                    "ndvi_avg": 0.2,
                    "ndmi_avg": -0.3,
                    "pixel_data": None,
                },
                "s1": [],
                "weak": True,
            }
        ]
    )

    results = accumulator.results()
    country = next(item for item in results if item["region"]["level"] == "country")
    province = next(item for item in results if item["region"]["level"] == "province")

    assert accumulator.land_count == 1
    assert country["totals"] == {"parcel_count": 1, "area_mu": 12.0}
    assert country["weak_growth"] == {"parcel_count": 1, "area_mu": 12.0}
    assert province["totals"] == {"parcel_count": 1, "area_mu": 12.0}
    assert province["children"][0]["level"] == "city"


def test_overview_accumulator_merge_matches_sequential_batches() -> None:
    from app.core.overview_preagg import OverviewAccumulator

    land = {
        "land_id": "L1",
        "area_mu": 12,
        "province_code": "11",
        "province_name": "北京",
        "city_code": "1101",
        "city_name": "北京市",
        "county_code": "110101",
        "county_name": "东城区",
        "s2": None,
        "s1": [],
        "weak": False,
    }
    batch = [land]

    sequential = OverviewAccumulator(
        window_from="2026-07-20", window_to="2026-09-18", crop=None
    )
    sequential.add_batch(batch)
    sequential.add_batch(batch)

    left = OverviewAccumulator(
        window_from="2026-07-20", window_to="2026-09-18", crop=None
    )
    right = OverviewAccumulator(
        window_from="2026-07-20", window_to="2026-09-18", crop=None
    )
    left.add_batch(batch)
    right.add_batch(batch)
    left.merge(right)

    assert left.land_count == sequential.land_count == 2
    assert left.results() == sequential.results()
