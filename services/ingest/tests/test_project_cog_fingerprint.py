"""项目 COG 块指纹：一致复用、地块增减/范围变化重建、旧旁车兼容。"""

from __future__ import annotations

import json

import pytest

from app.core import project_cogs as pc
from app.tasks import project_cogs as task
from tests.test_project_cogs import ITEMS, PARCELS, MemStorage, _fake_reader, _square


def _no_read(*a):
    raise AssertionError("should not re-read scenes")


class CountingReader:
    def __init__(self):
        self.calls = 0
        self.inner = _fake_reader({"A": 1500, "B": 1500})

    def __call__(self, item, block):
        self.calls += 1
        return self.inner(item, block)


def _run(st, parcels, reader, reported=None):
    return task.run_group("g", parcels, None, None, storage=st,
                          report=(reported.append if reported is not None else None),
                          searcher=lambda *a: ITEMS, reader=reader)


def _sides(st):
    return {k: json.loads(v) for k, v in st.objs.items() if k.endswith(".json")}


def test_fingerprint_deterministic_and_sensitive():
    a = pc.build_blocks(PARCELS[:2])[0]
    b = pc.build_blocks(list(reversed(PARCELS[:2])))[0]
    assert pc.block_fingerprint(a) == pc.block_fingerprint(b)
    moved = [PARCELS[0], {"land_id": "2", "boundary_geojson": _square(114.3031, 36.50)}]
    c = pc.build_blocks(moved)[0]
    assert c.key == a.key
    assert pc.block_fingerprint(c)["geom_hash"] != pc.block_fingerprint(a)["geom_hash"]


def test_match_reuses():
    st = MemStorage()
    s1 = _run(st, PARCELS, CountingReader())
    assert s1["written"] == 2
    assert all("fingerprint" in p for p in _sides(st).values())
    reported = []
    s2 = _run(st, PARCELS, _no_read, reported)
    assert s2["reused"] == 2 and s2["rebuilt"] == 0 and s2["written"] == 0 and s2["failed"] == 0
    assert len(reported) == 2


def test_added_parcel_rebuilds_only_affected_block():
    st = MemStorage()
    _run(st, PARCELS, CountingReader())
    added = PARCELS + [{"land_id": "9", "boundary_geojson": _square(114.3005, 36.5005, 0.0005)}]  # 落在块内不改范围
    blocks = {b.key: b for b in pc.build_blocks(added)}
    old = {b.key: b for b in pc.build_blocks(PARCELS)}
    assert blocks.keys() == old.keys()
    r = CountingReader()
    s = _run(st, added, r)
    assert s["rebuilt"] == 1 and s["written"] == 1 and s["reused"] == 1 and s["failed"] == 0
    assert r.calls > 0
    side = [p for p in _sides(st).values() if "9" in p["land_ids"]]
    assert len(side) == 1 and side[0]["fingerprint"]["land_ids"] == sorted(side[0]["land_ids"])


def test_removed_parcel_rebuilds():
    st = MemStorage()
    _run(st, PARCELS, CountingReader())
    fewer = [PARCELS[0], PARCELS[2]]
    s = _run(st, fewer, CountingReader())
    assert s["rebuilt"] == 1 and s["reused"] == 1


def test_extent_change_same_origin_rebuilds():
    st = MemStorage()
    base = PARCELS[:1]
    _run(st, base, CountingReader())
    blk = pc.build_blocks(base)[0]
    # 往东北方向扩：原点（左上角 x0/y1）可能不变，但尺寸变大
    grown = base + [{"land_id": "7", "boundary_geojson": _square(114.302, 36.4990, 0.01)}]
    nb = [b for b in pc.build_blocks(grown) if "1" in b.land_ids][0]
    assert (nb.width, nb.height) != (blk.width, blk.height)
    s = _run(st, grown, CountingReader())
    assert s["failed"] == 0 and s["written"] >= 1
    side = [p for p in _sides(st).values() if p["block_key"] == nb.key][0]
    assert (side["width"], side["height"]) == (nb.width, nb.height)


def _make_legacy(st):
    for k, p in _sides(st).items():
        p.pop("fingerprint")
        st.objs[k] = json.dumps(p).encode()


def test_legacy_sidecar_match_reuses_and_backfills():
    st = MemStorage()
    _run(st, PARCELS, CountingReader())
    _make_legacy(st)
    tifs = {k: v for k, v in st.objs.items() if k.endswith(".tif")}
    s = _run(st, PARCELS, _no_read, [])
    assert s["reused"] == 2 and s["fingerprint_backfilled"] == 2 and s["rebuilt"] == 0
    for k, p in _sides(st).items():
        blk = [b for b in pc.build_blocks(PARCELS) if b.key == p["block_key"]][0]
        assert p["fingerprint"] == pc.block_fingerprint(blk)
        assert "reused" not in p and "fingerprint_backfilled" not in p
    assert all(st.objs[k] == v for k, v in tifs.items())  # 未改动影像
    s3 = _run(st, PARCELS, _no_read)
    assert s3["reused"] == 2 and s3["fingerprint_backfilled"] == 0


def test_legacy_sidecar_mismatch_rebuilds():
    st = MemStorage()
    _run(st, PARCELS, CountingReader())
    _make_legacy(st)
    added = PARCELS + [{"land_id": "9", "boundary_geojson": _square(114.3005, 36.5005, 0.0005)}]
    s = _run(st, added, CountingReader())
    assert s["rebuilt"] == 1 and s["fingerprint_backfilled"] == 1 and s["failed"] == 0


def test_legacy_size_mismatch():
    fp = {"land_ids": ["1"], "width": 10, "height": 20}
    assert pc.fingerprint_status({"land_ids": ["1"], "width": 10, "height": 20}, fp) == "legacy_match"
    assert pc.fingerprint_status({"land_ids": ["1"]}, fp) == "legacy_match"
    assert pc.fingerprint_status({"land_ids": ["1"], "width": 11, "height": 20}, fp) == "mismatch"
    assert pc.fingerprint_status({"land_ids": ["1", "2"]}, fp) == "mismatch"
    assert pc.fingerprint_status({}, fp) == "mismatch"


def test_cog_without_sidecar_wrong_size_rebuilds():
    st = MemStorage()
    base = PARCELS[:1]
    _run(st, base, CountingReader())
    for k in [k for k in st.objs if k.endswith(".json")]:
        del st.objs[k]
    grown = base + [{"land_id": "7", "boundary_geojson": _square(114.302, 36.4990, 0.01)}]
    s = _run(st, grown, CountingReader())
    assert s["failed"] == 0 and s["written"] >= 1


# ---------------------------------------------------------------- 空标记
def test_empty_first_build_writes_marker_then_skips():
    st = MemStorage()
    s1 = _run(st, PARCELS, _fake_reader({"A": 0, "B": 0}))
    assert s1["empty"] == 2 and s1["written"] == 0
    assert not [k for k in st.objs if k.endswith(".tif")]
    assert all(p["empty"] for p in _sides(st).values())
    s2 = _run(st, PARCELS, _no_read, [])
    assert s2["empty"] == 2 and s2["empty_marked_skipped"] == 2 and s2["reused"] == 0 and s2["failed"] == 0


def test_empty_marker_fingerprint_change_retries():
    st = MemStorage()
    _run(st, PARCELS, _fake_reader({"A": 0, "B": 0}))
    added = PARCELS + [{"land_id": "9", "boundary_geojson": _square(114.3005, 36.5005, 0.0005)}]
    r = CountingReader()
    s = _run(st, added, r)
    assert s["written"] == 1 and s["empty_marked_skipped"] == 1 and r.calls > 0
    side = [p for p in _sides(st).values() if "9" in p["land_ids"]][0]
    assert not side.get("empty") and side["oss_key"] in st.objs


def test_mismatch_rebuild_empty_overwrites_stale_cog_and_marks(tmp_path):
    import numpy as np
    import rasterio

    class NoDelete(MemStorage):
        def delete(self, key):
            raise PermissionError("AccessDenied")

    st = NoDelete()
    _run(st, PARCELS, CountingReader())
    added = PARCELS + [{"land_id": "9", "boundary_geojson": _square(114.3005, 36.5005, 0.0005)}]
    s = _run(st, added, _fake_reader({"A": 0, "B": 0}))
    assert s["rebuilt"] == 1 and s["empty"] == 1 and s["reused"] == 1 and s["failed"] == 0
    blk = [b for b in pc.build_blocks(added) if "9" in b.land_ids][0]
    key = pc.cog_key("g", blk.key, "2026-10-10")
    f = tmp_path / "e.tif"
    f.write_bytes(st.objs[key])
    with rasterio.open(f) as ds:
        assert ds.count == 13 and (ds.width, ds.height) == (blk.width, blk.height)
        assert ds.transform == blk.transform and ds.crs.to_string() == blk.crs and ds.nodata == 0
        assert list(ds.descriptions) == list(pc.BAND_NAMES)
        assert ds.profile["compress"].lower() == "zstd"
        assert not np.any(ds.read())
    m = json.loads(st.objs[pc.sidecar_key(key)])
    assert m["empty"] is True and m["fingerprint"] == pc.block_fingerprint(blk)
    s2 = _run(st, added, _no_read)
    assert s2["empty_marked_skipped"] == 1 and s2["reused"] == 1 and s2["failed"] == 0


def test_first_empty_build_writes_no_cog():
    st = MemStorage()
    _run(st, PARCELS, _fake_reader({"A": 0, "B": 0}))
    assert not [k for k in st.objs if k.endswith(".tif")]


def test_read_failure_does_not_mark_empty():
    st = MemStorage()

    def boom(*a):
        raise OSError("network")

    s = _run(st, PARCELS, boom)
    assert s["empty"] == 2
    assert _sides(st) == {}
