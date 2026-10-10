"""收获占比落库门槛（HARVEST_PROGRESS_MIN_SAVE_PCT）与已计算区间标记。"""

from __future__ import annotations

import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import harvest_progress as hp
from tests.test_harvest_pixels import HarvestPixelsEndpointTests, _series
from tests.test_harvest_progress import (
    RESIDUE,
    SENESCENT,
    SOIL,
    _mixed,
    _residue_season,
)


def _row(d: str, combined: float | None, harvested: float = 0.0, keys=None) -> dict:
    return {
        "date": d,
        "status": "harvesting",
        "harvested_pct": harvested,
        "newly_harvested_pct": 0.0,
        "harvested_or_suspected_pct": combined,
        "suspected_harvest_pct": None if combined is None else combined - harvested,
        "valid_pct": 100.0,
        "valid_pixel_count": 10,
        "harvested_pixel_count": 0,
        "total_pixel_count": 10,
        "pixel_keys": keys,
        "pixel_states": "0" * len(keys) if keys else None,
    }


class FakeDB:
    """记录 execute 的 SQL 与参数；支持 begin_nested。"""

    def __init__(self, *, coverage_fails: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.coverage_fails = coverage_fails

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        if self.coverage_fails and "parcel_harvest_progress_coverage" in sql:
            raise RuntimeError(
                'relation "parcel_harvest_progress_coverage" does not exist'
            )
        self.calls.append((sql, dict(params or {})))
        return MagicMock()

    @asynccontextmanager
    async def _nested(self):
        yield

    def begin_nested(self):
        return self._nested()

    def sql(self, needle: str) -> list[dict]:
        return [p for s, p in self.calls if needle in s]


class MinSavePctConfigTests(unittest.TestCase):
    def test_env_parsing(self) -> None:
        cases = {
            "": 3.0,
            "5": 5.0,
            "0": 0.0,
            "abc": 3.0,
            "-1": 3.0,
            "nan": 3.0,
            "150": 100.0,
        }
        for raw, want in cases.items():
            with patch.dict(os.environ, {"HARVEST_PROGRESS_MIN_SAVE_PCT": raw}):
                self.assertEqual(hp.min_save_pct(), want, raw)

    def test_gate_uses_combined_and_is_strict(self) -> None:
        self.assertFalse(hp.should_persist_row(_row("d", 3.0), 3.0))
        self.assertTrue(hp.should_persist_row(_row("d", 3.01), 3.0))
        # 合计优先：已收获 1% + 疑似 4% → 合计 5% 落库。
        self.assertTrue(hp.should_persist_row(_row("d", 5.0, harvested=1.0), 3.0))
        # 旧结果无合计字段：退回已收获占比。
        self.assertTrue(hp.should_persist_row(_row("d", None, harvested=4.0), 3.0))
        self.assertFalse(hp.should_persist_row(_row("d", None, harvested=2.0), 3.0))
        # 门槛 0：合计为 0 的行仍不落库。
        self.assertFalse(hp.should_persist_row(_row("d", 0.0), 0.0))
        self.assertTrue(hp.should_persist_row(_row("d", 0.1), 0.0))


class RecomputePersistenceTests(unittest.TestCase):
    KEYS = [(115.0, 38.9), (115.0001, 38.9)]

    def _run(self, rows, *, env="3", db=None):
        db = db or FakeDB()
        with (
            patch.dict(os.environ, {"HARVEST_PROGRESS_MIN_SAVE_PCT": env}),
            patch.object(
                hp, "load_land_info", AsyncMock(return_value={"area_mu": 10.0})
            ),
            patch.object(
                hp, "compute_land_series", AsyncMock(return_value=rows)
            ) as comp,
        ):
            n = asyncio.run(
                hp.recompute_land(db, "L1", date(2026, 9, 1), date(2026, 10, 10))
            )
        return n, db, comp

    def test_only_rows_above_threshold_are_written(self) -> None:
        rows = [
            _row("2026-09-20", 0.0, keys=self.KEYS),
            _row("2026-09-25", 2.9, keys=self.KEYS),
            _row("2026-09-30", 3.0, keys=self.KEYS),
            _row("2026-10-02", 6.7, harvested=0.1, keys=self.KEYS),
            _row("2026-10-07", 44.8, harvested=9.9, keys=self.KEYS),
        ]
        n, db, comp = self._run(rows)
        self.assertEqual(n, 2)
        written = db.sql("INSERT INTO agric_satellite.parcel_harvest_progress (")
        self.assertEqual(
            [p["obs_date"].isoformat() for p in written], ["2026-10-02", "2026-10-07"]
        )
        self.assertTrue(all(p["pixel_states"] for p in written))
        # 区间先整体删除（清掉以前存下的低门槛行），坐标集合只写一次。
        self.assertEqual(
            len(db.sql("obs_date >= :date_from AND obs_date <= :date_to")), 1
        )
        self.assertEqual(len(db.sql("parcel_harvest_pixel_sets\n        (land_id")), 1)
        cov = db.sql("INSERT INTO agric_satellite.parcel_harvest_progress_coverage")
        self.assertEqual(len(cov), 1)
        self.assertEqual(cov[0]["computed_rows"], 5)
        self.assertEqual(cov[0]["saved_rows"], 2)
        self.assertEqual(cov[0]["min_save_pct"], 3.0)
        self.assertEqual(cov[0]["date_to"], date(2026, 10, 10))
        self.assertLess(cov[0]["date_from"], date(2026, 9, 1))  # 含 REVISION_DAYS 回看
        # 计算本身不受门槛影响：仍按完整区间计算一次。
        comp.assert_awaited_once()

    def test_all_below_threshold_writes_no_rows_or_pixel_sets_but_marks_range(
        self,
    ) -> None:
        rows = [
            _row("2026-09-20", 0.0, keys=self.KEYS),
            _row("2026-09-25", 1.5, keys=self.KEYS),
        ]
        n, db, _ = self._run(rows)
        self.assertEqual(n, 0)
        self.assertEqual(
            db.sql("INSERT INTO agric_satellite.parcel_harvest_progress ("), []
        )
        self.assertEqual(db.sql("parcel_harvest_pixel_sets\n        (land_id"), [])
        # 孤立坐标集合仍会被清理。
        self.assertEqual(
            len(db.sql("DELETE FROM agric_satellite.parcel_harvest_pixel_sets")), 1
        )
        cov = db.sql("parcel_harvest_progress_coverage")
        self.assertEqual(cov[0]["saved_rows"], 0)

    def test_threshold_zero_keeps_all_nonzero_rows(self) -> None:
        rows = [
            _row("2026-09-20", 0.0),
            _row("2026-09-25", 1.5),
            _row("2026-09-30", 2.0),
        ]
        n, _, _ = self._run(rows, env="0")
        self.assertEqual(n, 2)

    def test_missing_coverage_table_does_not_break_recompute(self) -> None:
        rows = [_row("2026-10-07", 44.8, keys=self.KEYS)]
        n, db, _ = self._run(rows, db=FakeDB(coverage_fails=True))
        self.assertEqual(n, 1)
        self.assertEqual(
            len(db.sql("INSERT INTO agric_satellite.parcel_harvest_progress (")), 1
        )

    def test_persisted_rows_are_unchanged_suffix_of_monotonic_series(self) -> None:
        """门槛只过滤落库：存下的行与完整序列中同日的值完全一致，且为季内单调尾段。"""
        tail = [
            ("2026-09-26", [SENESCENT] * 10),
            ("2026-10-01", _mixed(1, 0, 9)),
            ("2026-10-06", _mixed(4, 3, 3)),
            ("2026-10-11", [RESIDUE] * 5 + [SOIL] * 5),
            ("2026-10-20", [SOIL] * 10),
        ]
        full = _series(_residue_season(tail))
        n, db, _ = self._run(full)
        written = {
            p["obs_date"].isoformat(): p
            for p in db.sql("INSERT INTO agric_satellite.parcel_harvest_progress (")
        }
        expected = [r for r in full if hp.row_save_pct(r) > 3.0]
        self.assertTrue(expected)
        self.assertEqual(sorted(written), [r["date"] for r in expected])
        for r in expected:
            p = written[r["date"]]
            self.assertEqual(p["harvested_pct"], r["harvested_pct"])
            self.assertEqual(
                p["harvested_or_suspected_pct"], r["harvested_or_suspected_pct"]
            )
            self.assertEqual(p["pixel_states"], r["pixel_states"])
        # 季内合计单调：一旦超过门槛，此后同季的观测日都会落库（不出现空洞）。
        by_season: dict = {}
        for r in full:
            by_season.setdefault(r["season_start"], []).append(r)
        for season_rows in by_season.values():
            flags = [hp.row_save_pct(r) > 3.0 for r in season_rows]
            if True in flags:
                first = flags.index(True)
                self.assertTrue(all(flags[first:]), flags)


class RangeComputedTests(unittest.TestCase):
    def _run(self, coverage, latest=None, *, frm=date(2026, 1, 1), to=None):
        to = to or date.today()
        with (
            patch.object(hp, "load_coverage", AsyncMock(return_value=coverage)),
            patch.object(hp, "latest_obs_date", AsyncMock(return_value=latest)) as lat,
        ):
            return asyncio.run(hp.range_computed(MagicMock(), "L1", frm, to)), lat

    def test_cases(self) -> None:
        today = date.today()
        cov = {
            "computed_from": date(2025, 12, 1),
            "computed_to": today,
            "min_save_pct": 3.0,
        }
        self.assertTrue(self._run(cov)[0])
        self.assertFalse(self._run(None)[0])
        late = {**cov, "computed_from": date(2026, 3, 1)}
        self.assertFalse(self._run(late)[0])
        stale = {**cov, "computed_to": today - timedelta(days=3)}
        # 标记之后没有新观测 → 仍视为已覆盖；有新观测 → 需要重算。
        self.assertTrue(self._run(stale, latest=today - timedelta(days=5))[0])
        self.assertTrue(self._run(stale, latest=None)[0])
        self.assertFalse(self._run(stale, latest=today - timedelta(days=1))[0])
        # 请求区间晚于今天的部分不要求覆盖。
        ok, lat = self._run(cov, to=today + timedelta(days=30))
        self.assertTrue(ok)
        lat.assert_not_awaited()

    def test_coverage_unavailable_is_not_computed(self) -> None:
        db = MagicMock()
        db.begin_nested = MagicMock(side_effect=RuntimeError("no table"))
        self.assertIsNone(asyncio.run(hp.load_coverage(db, "L1")))


class HarvestProgressEndpointGateTests(unittest.TestCase):
    def _call(self, *, stored_rows, computed: bool, live_rows):
        from app.routers import agri

        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        with (
            patch.object(agri, "_agri_ready", AsyncMock()),
            patch.object(hp, "load_land_area_mu", AsyncMock(return_value=(True, 30.0))),
            patch.object(hp, "list_stored", AsyncMock(return_value=stored_rows)),
            patch.object(hp, "range_computed", AsyncMock(return_value=computed)) as rc,
            patch.object(
                hp, "compute_land_series", AsyncMock(return_value=live_rows)
            ) as comp,
            patch.object(hp, "enqueue_lands", AsyncMock()) as enq,
            patch.object(hp, "harvest_progress_enabled", return_value=True),
        ):
            out = asyncio.run(
                agri.get_harvest_progress(
                    "L1",
                    MagicMock(),
                    db,
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 10, 9),
                    include_zero=False,
                    interpolate="none",
                )
            )
        return out, comp, enq, rc

    LIVE = [{**_row("2026-09-25", 1.5), "season_start": "2026-05-25"}]

    def test_computed_but_all_below_threshold_returns_empty_stored(self) -> None:
        out, comp, enq, _ = self._call(
            stored_rows=[], computed=True, live_rows=self.LIVE
        )
        self.assertEqual(out.source, "stored")
        self.assertEqual(out.items, [])
        comp.assert_not_awaited()
        enq.assert_not_awaited()

    def test_never_computed_falls_back_to_live_and_enqueues(self) -> None:
        out, comp, enq, _ = self._call(
            stored_rows=[], computed=False, live_rows=self.LIVE
        )
        self.assertEqual(out.source, "live")
        comp.assert_awaited_once()
        enq.assert_awaited_once()
        # 现场结果保持原 include_zero 语义（>0 即返回），不套用落库门槛。
        self.assertEqual([str(i.date) for i in out.items], ["2026-09-25"])

    def test_stored_rows_skip_coverage_lookup(self) -> None:
        stored = [{**_row("2026-10-07", 44.8, harvested=9.9), "sensor": "S2"}]
        out, comp, _, rc = self._call(stored_rows=stored, computed=False, live_rows=[])
        self.assertEqual(out.source, "stored")
        rc.assert_not_awaited()
        comp.assert_not_awaited()


class HarvestPixelsGateTests(unittest.TestCase):
    DATA = HarvestPixelsEndpointTests.DATA  # 存储行 2026-10-07

    def _call(self, latest, *, latest_exc=None):
        from app.routers import agri

        db = MagicMock()
        db.rollback = AsyncMock()
        live = {**self.DATA, "date": "2026-10-09", "source": "live"}
        live_mock = AsyncMock(return_value=live)
        with (
            patch.object(agri, "_agri_ready", AsyncMock()),
            patch.object(hp, "load_land_area_mu", AsyncMock(return_value=(True, 30.0))),
            patch.object(
                hp, "load_stored_pixel_states", AsyncMock(return_value=dict(self.DATA))
            ),
            patch.object(hp, "compute_live_pixel_states", live_mock),
            patch.object(
                hp,
                "latest_obs_date",
                AsyncMock(return_value=latest, side_effect=latest_exc),
            ),
        ):
            out = asyncio.run(
                agri.get_harvest_pixels(
                    "L1", MagicMock(), db, on_date=date(2026, 10, 9)
                )
            )
        return out, live_mock

    def test_newer_unstored_observation_is_computed_live(self) -> None:
        out, live = self._call(date(2026, 10, 9))
        self.assertEqual(out.source, "live")
        live.assert_awaited_once()

    def test_stored_row_is_latest_observation(self) -> None:
        out, live = self._call(date(2026, 10, 7))
        self.assertEqual(out.source, "stored")
        live.assert_not_awaited()
        out, _ = self._call(None, latest_exc=RuntimeError("boom"))
        self.assertEqual(out.source, "stored")


if __name__ == "__main__":
    unittest.main()
