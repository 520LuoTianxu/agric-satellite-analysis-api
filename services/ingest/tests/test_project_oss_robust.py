"""OSS 健壮性：重试、回执重试、旁车缺失重建、掩膜复用、收获读取不静默丢日期。"""

from __future__ import annotations

import json
import ssl

import pytest

from app.core import oss_robust as orb
from app.core import project_cogs as pc
from app.tasks import project_cogs as task
from tests.test_project_cogs import ITEMS, PARCELS, MemStorage, _fake_reader


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(orb, "BACKOFF_BASE", 0.0)


class Flaky(MemStorage):
    """前 n 次 upload/put/download 抛 SSL EOF。"""

    def __init__(self, fails=2, *, download_always=False):
        super().__init__()
        self.fails = {"upload": fails, "put": fails, "download": fails}
        self.download_always = download_always
        self.downloads = 0

    def _maybe(self, op):
        if self.fails[op] > 0:
            self.fails[op] -= 1
            raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2427) UNEXPECTED_EOF")

    def upload_file(self, key, path, content_type=None):
        self._maybe("upload")
        return super().upload_file(key, path, content_type)

    def put_bytes(self, key, data, content_type=None):
        self._maybe("put")
        return super().put_bytes(key, data, content_type)

    def download_file(self, key, path):
        self.downloads += 1
        if self.download_always:
            raise ssl.SSLEOFError(8, "UNEXPECTED_EOF")
        self._maybe("download")
        return super().download_file(key, path)


def test_is_transient():
    assert orb.is_transient(ssl.SSLEOFError(8, "x"))
    assert orb.is_transient(ConnectionResetError())
    assert orb.is_transient(RuntimeError("Server disconnected without sending a response."))

    class E(Exception):
        status = 404

    assert not orb.is_transient(E("NoSuchKey"))
    E.status = 503
    assert orb.is_transient(E("busy"))
    assert not orb.is_transient(ValueError("bad"))


def test_retry_call_gives_up_and_non_transient_immediate():
    n = {"c": 0}

    def boom():
        n["c"] += 1
        raise ssl.SSLEOFError(8, "eof")

    with pytest.raises(ssl.SSLError):
        orb.retry_call(boom, what="t", attempts=3, sleep=lambda s: None)
    assert n["c"] == 3
    n["c"] = 0

    def bad():
        n["c"] += 1
        raise ValueError("no")

    with pytest.raises(ValueError):
        orb.retry_call(bad, what="t", attempts=5, sleep=lambda s: None)
    assert n["c"] == 1


def test_run_group_survives_flaky_upload_and_report():
    st = Flaky(fails=2)
    calls = []

    def report(p):
        calls.append(p["oss_key"])
        if len(calls) <= 2:
            raise RuntimeError("Server disconnected without sending a response.")

    stats = task.run_group("g", PARCELS, None, None, storage=st, report=report,
                           searcher=lambda *a: ITEMS, reader=_fake_reader({"A": 1500, "B": 1500}))
    assert stats["written"] == 2 and stats["reported"] == 2 and stats["failed"] == 0
    assert stats["report_failed"] == 0


def test_sidecar_missing_is_rebuilt_and_reported():
    st = MemStorage()
    task.run_group("g", PARCELS, None, None, storage=st, report=None,
                   searcher=lambda *a: ITEMS, reader=_fake_reader({"A": 1500, "B": 1500}))
    sides = [k for k in st.objs if k.endswith(".json")]
    orig = {k: json.loads(st.objs[k]) for k in sides}
    for k in sides:
        del st.objs[k]
    reported = []

    def no_read(*a):
        raise AssertionError("should not re-read scenes")

    stats = task.run_group("g", PARCELS, None, None, storage=st, report=reported.append,
                           searcher=lambda *a: ITEMS, reader=no_read)
    assert stats["reused"] == 2 and stats["reported"] == 2 and stats["failed"] == 0
    for p in reported:
        assert p["sidecar_rebuilt"] is True
        o = orig[pc.sidecar_key(p["oss_key"])]
        for f in ("valid_pixel_count", "mask_pixel_count", "scene_ids", "size_bytes", "acquired_at", "block_key"):
            assert p[f] == o[f], f
    assert all(k in st.objs for k in sides)


def test_mask_computed_once_per_block(monkeypatch):
    n = {"c": 0}
    real = task.parcel_mask

    def counting(block, *a, **k):
        n["c"] += 1
        return real(block, *a, **k)

    monkeypatch.setattr(task, "parcel_mask", counting)
    items = ITEMS + [
        {"id": "C", "properties": {"datetime": "2026-10-11T03:00:00Z", "eo:cloud_cover": 1}},
        {"id": "D", "properties": {"datetime": "2026-10-12T03:00:00Z", "eo:cloud_cover": 1}},
    ]
    stats = task.run_group("g", PARCELS, None, None, storage=MemStorage(), report=None, searcher=lambda *a: items,
                           reader=_fake_reader({"A": 1500, "B": 1500, "C": 1500, "D": 1500}))
    assert stats["written"] == 6  # 2 块 × 3 天
    assert n["c"] == 2


def _harvest_setup(st):
    task.run_group("g", PARCELS[:2], None, None, storage=st, report=None,
                   searcher=lambda *a: ITEMS, reader=_fake_reader({"A": 1500, "B": 1500}))


def test_harvest_read_retries_then_fails_run(monkeypatch):
    from agric_satellite_analysis_common import project_harvest as ph

    from app.tasks import project_harvest as phtask

    st = Flaky(fails=0, download_always=True)
    _harvest_setup(st)
    block = pc.build_blocks(PARCELS[:2])[0]
    parcels = [{"land_id": p["land_id"], "clipped_geojson": pc.parcel_geometry(p["boundary_geojson"]).__geo_interface__,
                "clipped_area_mu": 1.0} for p in PARCELS[:2]]
    with pytest.raises(phtask.CogReadError) as ei:
        phtask.run_block(group_id="g", run_id="r", block=block, parcels=parcels, season_from=__import__("datetime").date(2026, 8, 1),
                         season_to=__import__("datetime").date(2026, 10, 13), params=ph.merge_params(None),
                         storage=orb.robust(st), report=None, searcher=lambda *a: ITEMS)
    assert "2026-10-10" in str(ei.value) and "SSLEOFError" in str(ei.value)
    assert st.downloads == 2 * orb.ATTEMPTS  # 存储层 × 读取层均重试满


def test_harvest_read_transient_recovers(monkeypatch):
    from agric_satellite_analysis_common import project_harvest as ph

    from app.tasks import project_harvest as phtask

    st = Flaky(fails=0)
    _harvest_setup(st)
    st.fails["download"] = 2
    block = pc.build_blocks(PARCELS[:2])[0]
    parcels = [{"land_id": p["land_id"], "clipped_geojson": pc.parcel_geometry(p["boundary_geojson"]).__geo_interface__,
                "clipped_area_mu": 1.0} for p in PARCELS[:2]]
    r = phtask.run_block(group_id="g", run_id="r", block=block, parcels=parcels,
                         season_from=__import__("datetime").date(2026, 8, 1), season_to=__import__("datetime").date(2026, 10, 13),
                         params=ph.merge_params(None), storage=orb.robust(st), report=None, searcher=lambda *a: ITEMS)
    rec = r["scenes"][0]
    assert rec.get("note") != "read_failed" and "clear_frac" in rec


def test_harvest_tolerated_missing_recorded(monkeypatch):
    from agric_satellite_analysis_common import project_harvest as ph

    from app.tasks import project_harvest as phtask

    st = Flaky(fails=0, download_always=True)
    _harvest_setup(st)
    block = pc.build_blocks(PARCELS[:2])[0]
    parcels = [{"land_id": p["land_id"], "clipped_geojson": pc.parcel_geometry(p["boundary_geojson"]).__geo_interface__,
                "clipped_area_mu": 1.0} for p in PARCELS[:2]]
    params = {**ph.merge_params(None), "max_missing_date_frac": 1.0}
    r = phtask.run_block(group_id="g", run_id="r", block=block, parcels=parcels,
                         season_from=__import__("datetime").date(2026, 8, 1), season_to=__import__("datetime").date(2026, 10, 13),
                         params=params, storage=orb.robust(st), report=None, searcher=lambda *a: ITEMS)
    assert r["block"]["missing_dates"] == ["2026-10-10"]


def test_harvest_skips_empty_marked_date_cleanly():
    from agric_satellite_analysis_common import project_harvest as ph

    from app.tasks import project_harvest as phtask

    st = MemStorage()
    task.run_group("g", PARCELS[:2], None, None, storage=st, report=None,
                   searcher=lambda *a: ITEMS, reader=_fake_reader({"A": 0, "B": 0}))
    block = pc.build_blocks(PARCELS[:2])[0]
    key = pc.cog_key("g", block.key, "2026-10-10")
    assert key not in st.objs and json.loads(st.objs[pc.sidecar_key(key)])["empty"] is True
    parcels = [{"land_id": p["land_id"], "clipped_geojson": pc.parcel_geometry(p["boundary_geojson"]).__geo_interface__,
                "clipped_area_mu": 1.0} for p in PARCELS[:2]]

    def no_read(*a):
        raise AssertionError("should not re-read scenes")

    r = phtask.run_block(group_id="g", run_id="r", block=block, parcels=parcels,
                         season_from=__import__("datetime").date(2026, 8, 1), season_to=__import__("datetime").date(2026, 10, 13),
                         params=ph.merge_params(None), storage=orb.robust(st), report=None, searcher=lambda *a: ITEMS,
                         reader=no_read)
    rec = r["scenes"][0]
    assert rec["note"] == "no_data" and rec["valid"] is False
    assert "missing_dates" not in r["block"]


def test_robust_delete():
    st = MemStorage()
    st.objs["k"] = b"x"
    orb.robust(st).delete("k")
    assert "k" not in st.objs
