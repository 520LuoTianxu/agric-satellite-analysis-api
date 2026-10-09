"""收获逐像元状态：编码、单调、无数据规则；坐标集合存储；harvest-pixels 接口。"""

from __future__ import annotations

import asyncio
import json
import random
import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.core.harvest_progress import (
    PIXEL_STATE_HARVESTED,
    PIXEL_STATE_NODATA,
    PIXEL_STATE_SUSPECTED,
    PIXEL_STATE_UNHARVESTED,
    compute_harvest_series,
    decode_pixel_states,
    pixel_state_counts,
)
from app.services.harvest_progress import pixel_set_payload
from tests.test_harvest_progress import (
    CANOPY,
    FROSTED,
    RESIDUE,
    SENESCENT,
    SOIL,
    THR,
    _mixed,
    _residue_season,
)

U, S, H, N = (
    PIXEL_STATE_UNHARVESTED,
    PIXEL_STATE_SUSPECTED,
    PIXEL_STATE_HARVESTED,
    PIXEL_STATE_NODATA,
)


def _series(scenes, **kw):
    return compute_harvest_series(
        scenes, thresholds=THR, adaptive=False, pixel_states=True, **kw
    )


def _states(row):
    return decode_pixel_states(row["pixel_states"])


class PixelStateTests(unittest.TestCase):
    def test_states_follow_tiers_and_match_percentages(self) -> None:
        tail = [
            ("2026-10-01", _mixed(4, 3, 3)),
            ("2026-10-06", _mixed(4, 3, 3)),
            ("2026-10-20", [SOIL] * 10),
            ("2026-10-25", [SOIL] * 10),
        ]
        rows = {r["date"]: r for r in _series(_residue_season(tail))}
        keys = rows["2026-10-06"]["pixel_keys"]
        self.assertEqual(len(keys), 10)
        # 同纬度按经度升序，与 _v5_pixel 的像元序号一致。
        self.assertEqual([k[0] for k in keys], sorted(k[0] for k in keys))
        self.assertEqual(_states(rows["2026-10-06"]), [S] * 4 + [U] * 6)
        self.assertEqual(_states(rows["2026-10-25"]), [H] * 10)
        for r in rows.values():
            st = _states(r)
            self.assertEqual(len(st), 10)
            n = r["crop_pixel_count"]
            if not n:
                self.assertEqual(set(st), {N})
                continue
            c = pixel_state_counts(st)
            self.assertAlmostEqual(100 * c["harvested"] / n, r["harvested_pct"], 1)
            self.assertAlmostEqual(
                100 * c["suspected"] / n, r["suspected_harvest_pct"], delta=0.11
            )

    def test_cloudy_pixel_is_nodata_unless_already_counted(self) -> None:
        scenes = _residue_season(
            [
                ("2026-10-01", _mixed(4, 0, 6)),
                ("2026-10-06", _mixed(4, 0, 6)),
                ("2026-10-11", _mixed(4, 0, 6)),
            ]
        )
        cloudy = next(s for s in scenes if s["date"] == "2026-10-06")
        cloudy["pixels"][0]["clear"] = 0  # 已疑似 → 仍为 1
        cloudy["pixels"][9]["clear"] = 0  # 未收获 → 无数据
        rows = {r["date"]: r for r in _series(scenes)}
        st = _states(rows["2026-10-06"])
        self.assertEqual(st[0], S)
        self.assertEqual(st[9], N)
        self.assertEqual(st[5], U)

    def test_off_season_rows_are_all_nodata(self) -> None:
        rows = _series(_residue_season([("2026-10-01", [SENESCENT] * 10)]))
        first = rows[0]
        self.assertEqual(first["status"], "off_season")
        self.assertEqual(set(_states(first)), {N})

    def test_per_pixel_state_monotonic_within_season(self) -> None:
        rnd = random.Random(11)
        palette = (SENESCENT, FROSTED, RESIDUE, SOIL, CANOPY)
        tail = [
            (
                (date(2026, 9, 20) + timedelta(days=5 * i)).isoformat(),
                [rnd.choice(palette) for _ in range(40)],
            )
            for i in range(12)
        ]
        scenes = _residue_season(tail, n=40)
        for sc in scenes[6:]:
            for p in sc["pixels"]:
                if rnd.random() < 0.15:
                    p["clear"] = 0
        prev: dict[int, int] = {}
        season = None
        for r in _series(scenes):
            if r["season_start"] != season:
                prev, season = {}, r["season_start"]
            for i, v in enumerate(_states(r)):
                if v == N:
                    continue
                self.assertGreaterEqual(v, prev.get(i, U), (r["date"], i))
                prev[i] = v

    def test_without_flag_rows_have_no_pixel_fields(self) -> None:
        rows = compute_harvest_series(
            _residue_season([("2026-10-01", [SENESCENT] * 10)]),
            thresholds=THR,
            adaptive=False,
        )
        self.assertTrue(all("pixel_states" not in r for r in rows))

    def test_decode_and_set_payload(self) -> None:
        self.assertEqual(decode_pixel_states("012."), [U, S, H, N])
        self.assertEqual(decode_pixel_states(None, 3), [N, N, N])
        keys = [(115.0001234567, 38.9), (115.0002, 38.9)]
        h1, payload = pixel_set_payload(keys)
        h2, _ = pixel_set_payload(list(keys))
        self.assertEqual(h1, h2)
        self.assertEqual(json.loads(payload)["lon"], [115.000123, 115.0002])
        self.assertNotEqual(h1, pixel_set_payload(keys[:1])[0])


class HarvestPixelsEndpointTests(unittest.TestCase):
    DATA = {
        "date": "2026-10-07",
        "status": "harvesting",
        "scene_id": "s1",
        "harvested_pct": 25.0,
        "suspected_harvest_pct": 25.0,
        "harvested_or_suspected_pct": 50.0,
        "crop_pixel_count": 4,
        "valid_pct": 80.0,
        "season_start": "2026-05-25",
        "pixel_keys": [
            (115.0, 38.9),
            (115.0001, 38.9),
            (115.0002, 38.9),
            (115.0003, 38.9),
            (115.0004, 38.9),
        ],
        "pixel_states": "012..",
        "source": "stored",
    }

    def _call(self, *, stored=None, stored_exc=None, live=None, exists=True):
        from app.routers import agri

        db = MagicMock()
        db.rollback = AsyncMock()
        load = AsyncMock(return_value=stored, side_effect=stored_exc)
        live_mock = AsyncMock(return_value=live)
        with (
            patch.object(agri, "_agri_ready", AsyncMock()),
            patch(
                "app.services.harvest_progress.load_land_area_mu",
                AsyncMock(return_value=(exists, 30.0)),
            ),
            patch("app.services.harvest_progress.load_stored_pixel_states", load),
            patch("app.services.harvest_progress.compute_live_pixel_states", live_mock),
        ):
            out = asyncio.run(
                agri.get_harvest_pixels(
                    "L1", MagicMock(), db, on_date=date(2026, 10, 9)
                )
            )
        return out, load, live_mock, db

    def test_stored_states_returned_as_lonlat_pixels(self) -> None:
        out, _, live, _ = self._call(stored=dict(self.DATA))
        live.assert_not_awaited()
        self.assertEqual(out.source, "stored")
        self.assertEqual(str(out.date), "2026-10-07")
        self.assertEqual(str(out.requested_date), "2026-10-09")
        self.assertEqual(out.format, "lonlat_v1")
        self.assertEqual(out.nodata, 255)
        self.assertEqual([p.state for p in out.pixels_lonlat], [0, 1, 2, 255, 255])
        self.assertEqual(out.pixels_lonlat[1].lon, 115.0001)
        self.assertEqual(out.counts.harvested, 1)
        self.assertEqual(out.counts.nodata, 2)
        self.assertEqual(out.crop_pct.harvested, 25.0)
        self.assertEqual(out.crop_pct.suspected, 25.0)
        self.assertEqual(out.crop_pct.unharvested, 25.0)
        self.assertEqual(out.crop_pct.nodata, 25.0)
        self.assertEqual(out.state_labels["1"], "疑似收获")

    def test_live_fallback_when_not_stored_or_migration_missing(self) -> None:
        live = {**self.DATA, "source": "live"}
        out, _, live_mock, _ = self._call(stored=None, live=live)
        self.assertEqual(out.source, "live")
        live_mock.assert_awaited_once()
        out, _, _, db = self._call(stored_exc=RuntimeError("no column"), live=live)
        self.assertEqual(out.source, "live")
        db.rollback.assert_awaited()

    def test_not_found(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._call(stored=None, live=None)
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException) as ctx:
            self._call(exists=False)
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
