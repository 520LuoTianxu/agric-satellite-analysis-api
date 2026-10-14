"""项目级全波段 COG：纯函数与构建流程（无网络、无 OSS）。"""

from __future__ import annotations

import json

import numpy as np
import pytest
import rasterio
from rio_cogeo.cogeo import cog_validate

from app.core import project_cogs as pc
from app.tasks import project_cogs as task


def _square(lon: float, lat: float, d: float = 0.002) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]],
    }


PARCELS = [
    {"land_id": "1", "boundary_geojson": _square(114.30, 36.50)},
    {"land_id": "2", "boundary_geojson": json.dumps(_square(114.303, 36.50))},
    {"land_id": "3", "boundary_geojson": _square(114.60, 36.80)},  # 远处，单独成块
]


def test_cog_key_deterministic():
    assert pc.cog_key("2878", "e32650_x1_y2", "2026-10-10") == "cog/s2l2a/v1/group_2878/e32650_x1_y2/2026-10-10.tif"
    assert pc.sidecar_key("a/b.tif") == "a/b.json"


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("PROJECT_SCENE_COGS_ENABLED", raising=False)
    assert pc.project_cogs_enabled() is False
    monkeypatch.setenv("PROJECT_SCENE_COGS_ENABLED", "1")
    assert pc.project_cogs_enabled() is True


def test_build_blocks_clusters_and_is_deterministic():
    a = pc.build_blocks(PARCELS)
    b = pc.build_blocks(list(reversed(PARCELS)))
    assert [x.key for x in a] == [x.key for x in b]
    assert sorted(len(x.land_ids) for x in a) == [1, 2]
    for blk in a:
        assert blk.x0 % pc.BLOCK_SNAP_M == 0
        assert blk.crs.startswith("EPSG:326")


def test_build_blocks_splits_over_cap(monkeypatch):
    monkeypatch.setenv("PROJECT_SCENE_COGS_MAX_BLOCK_PIXELS", "20000")
    blocks = pc.build_blocks(PARCELS[:2] + [{"land_id": "4", "boundary_geojson": _square(114.30, 36.50, 0.03)}])
    assert len(blocks) > 1
    assert all(b.width * b.height <= 20000 * 1.0001 or b.width <= 256 for b in blocks)
    assert len({b.key for b in blocks}) == len(blocks)


def test_to_uint16_reflectance():
    dn = np.array([np.nan, 0.0, -5.0, 1234.4, 70000.0], dtype=np.float32)
    out = pc.to_uint16_reflectance(dn, 1e-4, 0.0)
    assert out.dtype == np.uint16
    assert out.tolist() == [0, 0, 1, 1234, 65535]
    # 旧基线：DN 带 -1000 偏移
    assert pc.to_uint16_reflectance(np.array([2000.0], np.float32), 1e-4, -0.1).tolist() == [1000]


def test_mosaic_fill_only_fills_empty():
    acc = np.zeros((3, 2, 2), np.uint16)
    acc[:, 0, 0] = [5, 6, 4]
    new = np.full((3, 2, 2), 9, np.uint16)
    out = pc.mosaic_fill(acc, new)
    assert out[:, 0, 0].tolist() == [5, 6, 4]
    assert out[:, 1, 1].tolist() == [9, 9, 9]
    first = pc.mosaic_fill(None, new)
    assert first is not new and np.array_equal(first, new)


def _fake_reader(value_by_item):
    def reader(item, block):
        nb = len(pc.BAND_NAMES)
        arr = np.zeros((nb, block.height, block.width), np.uint16)
        v = value_by_item[item["id"]]
        if v == "left":
            rows = np.where(pc.parcel_mask(block).any(axis=1))[0]
            half = int(rows[len(rows) // 2])
            arr[:-1, :half] = 1000
            arr[-1, :half] = 4
        else:
            arr[:-1] = v
            arr[-1] = 4 if v else 0
        return arr

    return reader


class MemStorage:
    def __init__(self):
        self.objs: dict[str, bytes] = {}

    def exists(self, key):
        return key in self.objs

    def upload_file(self, key, path, content_type=None):
        with open(path, "rb") as f:
            self.objs[key] = f.read()

    def put_bytes(self, key, data, content_type=None):
        self.objs[key] = data

    def get_bytes(self, key):
        return self.objs[key]

    def download_file(self, key, path):
        with open(path, "wb") as f:
            f.write(self.objs[key])


ITEMS = [
    {"id": "A", "properties": {"datetime": "2026-10-10T03:00:00Z", "grid:code": "MGRS-50SLC", "eo:cloud_cover": 1}},
    {"id": "B", "properties": {"datetime": "2026-10-10T03:00:05Z", "grid:code": "MGRS-50SLB", "eo:cloud_cover": 2}},
]


def test_build_block_date_writes_valid_cog_and_is_idempotent(tmp_path):
    block = pc.build_blocks(PARCELS[:2])[0]
    st = MemStorage()
    p = task.build_block_date(
        group_id="g", block=block, obs_date="2026-10-10", items=ITEMS, storage=st,
        reader=_fake_reader({"A": "left", "B": 2000}),
    )
    assert p["kind"] == "project_scene_cog" and p["scene_ids"] == ["A", "B"]
    assert p["tiles"] == ["MGRS-50SLB", "MGRS-50SLC"]
    assert p["bands"][-1] == "SCL" and len(p["bands"]) == 13
    assert 0 < p["valid_pixel_count"] == p["mask_pixel_count"] < block.width * block.height
    f = tmp_path / "x.tif"
    f.write_bytes(st.objs[p["oss_key"]])
    valid, errors, _ = cog_validate(str(f))
    assert valid, errors
    with rasterio.open(f) as ds:
        assert ds.count == 13 and ds.dtypes[0] == "uint16" and ds.nodata == 0
        assert ds.profile["compress"].lower() == "zstd"
        d = ds.read(1)
        assert set(np.unique(d)) <= {0, 1000, 2000}

    calls = []
    again = task.build_block_date(
        group_id="g", block=block, obs_date="2026-10-10", items=ITEMS, storage=st,
        reader=lambda it, b: calls.append(it) or None,
    )
    assert again["reused"] is True and not calls
    assert again["oss_key"] == p["oss_key"]


def test_build_block_date_empty_returns_none():
    block = pc.build_blocks(PARCELS[:1])[0]
    st = MemStorage()
    assert task.build_block_date(
        group_id="g", block=block, obs_date="2026-10-10", items=ITEMS[:1], storage=st,
        reader=_fake_reader({"A": 0}),
    ) is None
    assert st.objs == {}


def test_run_group_reports(monkeypatch):
    st = MemStorage()
    reported = []
    stats = task.run_group(
        "g", PARCELS, None, None, storage=st, report=reported.append,
        searcher=lambda bbox, d0, d1: ITEMS, reader=_fake_reader({"A": 1500, "B": 1500}),
    )
    assert stats["written"] == 2 and stats["reported"] == 2 and stats["failed"] == 0
    stats2 = task.run_group(
        "g", PARCELS, None, None, storage=st, report=reported.append,
        searcher=lambda bbox, d0, d1: ITEMS, reader=_fake_reader({"A": 1500, "B": 1500}),
    )
    assert stats2["reused"] == 2 and stats2["written"] == 0


def test_dispatch_gated(monkeypatch):
    sent = []
    monkeypatch.setattr(task.build_project_cogs_for_lands, "apply_async", lambda args: sent.append(args))
    monkeypatch.delenv("PROJECT_SCENE_COGS_ENABLED", raising=False)
    assert task.maybe_dispatch_after_batch(["1"], [{"date": "2026-10-10"}], "S2") is False
    monkeypatch.setenv("PROJECT_SCENE_COGS_ENABLED", "true")
    assert task.maybe_dispatch_after_batch(["1"], [{"date": "2026-10-10"}], "S1") is False
    assert task.maybe_dispatch_after_batch(["1"], [], "S2") is False
    assert task.maybe_dispatch_after_batch(["1"], [{"date": "2026-10-10"}, {"date": "2026-10-10"}], "S2") is True
    assert sent == [[["1"], ["2026-10-10"]]]
