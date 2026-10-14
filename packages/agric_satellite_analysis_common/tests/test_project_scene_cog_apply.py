"""project_scene_cog 回执：参数映射、分发、缺表容错。"""

from __future__ import annotations

import pytest

from agric_satellite_analysis_common import result_apply as ra

PAYLOAD = {
    "kind": "project_scene_cog",
    "group_id": "2878",
    "block_key": "e32650_x1_y2",
    "obs_date": "2026-10-10",
    "oss_key": "cog/s2l2a/v1/group_2878/e32650_x1_y2/2026-10-10.tif",
    "crs": "EPSG:32650",
    "width": 100,
    "height": 80,
    "transform": [10, 0, 1, 0, -10, 2],
    "bounds": [1, 2, 3, 4],
    "bounds_4326": [114.1, 36.1, 114.2, 36.2],
    "bands": ["B01", "SCL"],
    "acquired_at": "2026-10-10T03:05:01Z",
    "scene_ids": ["S2B_50SLC_20261010_0_L2A"],
    "land_ids": [1, 2],
}


class FakeSession:
    def __init__(self, exc=None):
        self.exc, self.calls, self.committed, self.rolled = exc, [], False, False

    def execute(self, stmt, params):
        self.calls.append(params)
        if self.exc:
            raise self.exc

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled = True

    def close(self):
        pass


def test_params_mapping():
    p = ra.project_scene_cog_params(PAYLOAD)
    assert p["west"] == 114.1 and p["north"] == 36.2
    assert p["land_ids"] == ["1", "2"] and p["product"] == "s2l2a_v1"
    assert p["acquired_at"].startswith("2026-10-10T03:05:01")
    with pytest.raises(ValueError):
        ra.project_scene_cog_params({**PAYLOAD, "oss_key": ""})


def test_dispatch_upserts(monkeypatch):
    s = FakeSession()
    monkeypatch.setattr(ra, "SyncSession", lambda: s)
    out = ra.apply_result_envelope(dict(PAYLOAD))
    assert out["domain"]["project_scene_cog"] == "upserted"
    assert s.committed and s.calls[0]["oss_key"] == PAYLOAD["oss_key"]


def test_missing_table_tolerated(monkeypatch):
    s = FakeSession(Exception('relation "agric_satellite.project_scene_cogs" does not exist'))
    monkeypatch.setattr(ra, "SyncSession", lambda: s)
    assert ra.apply_project_scene_cog(dict(PAYLOAD)) == "table_missing"
    assert s.rolled


def test_other_errors_raise(monkeypatch):
    monkeypatch.setattr(ra, "SyncSession", lambda: FakeSession(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        ra.apply_project_scene_cog(dict(PAYLOAD))
