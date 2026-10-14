"""项目收获分析路由：DB 读取打桩，校验各接口形状（含栅格预签名 URL）。"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from agric_satellite_analysis_common import project_harvest as ph
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import database
from app.routers import project_harvest as R
from app.services import project_harvest as svc

RUN_ID = "00000000-0000-4000-8000-000000000001"
SQ = {"type": "Polygon", "coordinates": [[[115, 33], [115, 33.001], [115.001, 33.001], [115.001, 33], [115, 33]]]}


def _stat(lid, d, pick, high):
    row = ph.parcel_date_row({"high": high, "medium": 0.0, "low": 0.0, "unharvested": 10 - high, "cloud": 0.0,
                              "noncrop": 0.0}, 10.0, 10.0)
    return {"land_id": lid, "obs_date": date.fromisoformat(d), "block_key": "b1", "is_pick": pick, **row}


STATS = [_stat("1", "2026-10-10", False, 1.0), _stat("1", "2026-10-13", True, 4.0)]
META = {"1": {"land_id": "1", "land_status": "1", "town_name": "A", "village_name": "B"}}
SUMS = ph.build_summaries([dict(s, obs_date=s["obs_date"].isoformat()) for s in STATS], META, "2026-10-13")
RUN = {"run_id": uuid.UUID(RUN_ID), "group_id": "g1", "method_version": ph.METHOD_VERSION, "dedup_hash": "h",
       "season_from": date(2026, 8, 10), "season_to": date(2026, 10, 13), "status": "succeeded", "is_current": True,
       "params": {}, "thresholds": {}, "blocks": [{"key": "b1", "bbox4326": [115, 33, 115.001, 33.001],
                                                   "dates": ["2026-10-10", "2026-10-13"], "parcel_count": 1}],
       "valid_dates": [date(2026, 10, 10), date(2026, 10, 13)], "parcel_count": 1, "merged_count": 0,
       "dedup_area_mu": 10.0, "extras": {"pick_dates": {"2026-10-13": 1}}}
DEDUP = [{"land_id": "1", "kept": True, "land_status": "1", "clipped_geojson": SQ, "clipped_area_mu": 10.0,
          "priority_rank": 1, "town_name": "A", "village_name": "B"}]
RASTERS = [{"block_key": "b1", "obs_date": date(2026, 10, 13), "kind": "tier", "format": "png", "oss_key": "k.png",
            "west": 115.0, "south": 33.0, "east": 115.001, "north": 33.001, "width": 2, "height": 2}]


@pytest.fixture()
def client(monkeypatch):
    async def cur(db, g):
        return RUN if g == "g1" else None

    async def none(*a, **k):
        return None

    async def sums(db, r):
        return SUMS

    async def stats(db, r, d=None):
        return STATS

    async def ras(db, r, d):
        return [x for x in RASTERS if x["obs_date"].isoformat() == d]

    async def dd(db, g, h, with_geom=True):
        return DEDUP

    for name, fn in (("current_run", cur), ("active_run", none), ("summaries", sums), ("stats", stats),
                     ("rasters", ras), ("dedup_rows", dd)):
        monkeypatch.setattr(svc, name, fn)
    monkeypatch.setattr(svc, "trigger_enabled", lambda: False)

    class St:
        def presigned_get(self, key, expires=None):
            assert isinstance(expires, timedelta)
            return "https://oss/" + key

    import app.core.storage as st

    monkeypatch.setattr(st, "get_storage", lambda: St())
    app = FastAPI()
    app.include_router(R.router, prefix="/v1")

    async def db():
        yield None

    app.dependency_overrides[database.get_db] = db
    app.dependency_overrides[R._reader] = lambda: None
    app.dependency_overrides[R._admin] = lambda: None
    return TestClient(app)


def test_endpoints(client):
    base = "/v1/projects/g1/harvest-analysis"
    o = client.get(base).json()
    assert o["available"] and o["valid_dates"] == ["2026-10-10", "2026-10-13"] and o["can_trigger"] is False
    s = client.get(base + "/summary").json()
    assert s["date"] == "pick" and abs(s["totals"]["harvested_mu"] - 4.0) < 1e-6
    p = client.get(base + "/parcels", params={"date": "2026-10-10"}).json()
    assert p["total"] == 1 and abs(p["items"][0]["harvested_mu"] - 1.0) < 1e-6
    g = client.get(base + "/parcels.geojson").json()
    assert g["features"][0]["properties"]["land_id"] == "1"
    lay = client.get(base + "/layers", params={"date": "2026-10-13"}).json()
    assert lay["items"][0]["url"] == "https://oss/k.png"
    assert client.get(base + "/dedup").json()["kept"] == 1
    assert client.get("/v1/projects/other/harvest-analysis/summary").status_code == 404
