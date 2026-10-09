"""收获占比：像元级启发式、回执解析与接口过滤。"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.harvest_progress import (
    HarvestProgressThresholds,
    compute_harvest_series,
    valid_ndvi_values,
)

THR = HarvestProgressThresholds(
    ndvi_max=0.30, peak_drop=0.5, grow_min=0.35, lookback_days=150, min_valid_pct=50
)


def _scene(day: str, ndvis: list[float], *, clear: list[int] | None = None, **kw):
    pixels = []
    for i, v in enumerate(ndvis):
        p = {"lon": 120 + i * 1e-4, "lat": 40.0, "NDVI": v}
        if clear is not None:
            p["clear"] = clear[i]
        pixels.append(p)
    return {"date": day, "pixels": pixels, "scene_id": f"s_{day}", **kw}


class HarvestSeriesTests(unittest.TestCase):
    def test_progressive_harvest_after_peak(self) -> None:
        scenes = [
            _scene("2026-07-01", [0.5] * 10),
            _scene("2026-08-01", [0.8] * 10),  # 季节峰值
            _scene("2026-09-20", [0.8] * 6 + [0.15] * 4),
            _scene("2026-10-01", [0.7] * 2 + [0.12] * 8),
        ]
        rows = compute_harvest_series(scenes, parcel_area_mu=100, thresholds=THR)
        by_day = {r["date"]: r for r in rows}
        self.assertEqual(by_day["2026-07-01"]["status"], "growing")
        self.assertEqual(by_day["2026-08-01"]["harvested_pct"], 0.0)
        self.assertEqual(by_day["2026-09-20"]["harvested_pct"], 40.0)
        self.assertEqual(by_day["2026-09-20"]["newly_harvested_pct"], 40.0)
        self.assertEqual(by_day["2026-09-20"]["harvested_area_mu"], 40.0)
        self.assertEqual(by_day["2026-10-01"]["harvested_pct"], 80.0)
        self.assertEqual(by_day["2026-10-01"]["newly_harvested_pct"], 40.0)
        self.assertEqual(by_day["2026-10-01"]["status"], "harvesting")
        self.assertEqual(by_day["2026-10-01"]["peak_date"], "2026-08-01")

    def test_low_ndvi_before_peak_is_not_harvest(self) -> None:
        # 出苗期低 NDVI 不能被当作收获。
        scenes = [_scene("2026-05-20", [0.1] * 10), _scene("2026-06-30", [0.6] * 10)]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertTrue(all(r["harvested_pct"] == 0 for r in rows))
        self.assertEqual(rows[0]["status"], "no_growth")
        self.assertEqual(rows[1]["status"], "growing")

    def test_never_grown_field_is_zero(self) -> None:
        scenes = [_scene("2026-08-01", [0.2] * 5), _scene("2026-09-01", [0.1] * 5)]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertEqual([r["status"] for r in rows], ["no_growth", "no_growth"])
        self.assertEqual([r["harvested_pct"] for r in rows], [0.0, 0.0])

    def test_cloudy_day_skipped_and_clear_only(self) -> None:
        scenes = [
            _scene("2026-08-01", [0.8] * 4, clear=[1, 1, 1, 1]),
            # 只有 1/4 有效像元 → 观测不足，整天不计
            _scene("2026-09-01", [0.1] * 4, clear=[1, 0, 0, 0]),
            # 3/4 有效；云像元 NDVI 不参与
            _scene("2026-09-10", [0.1, 0.1, 0.8, 0.05], clear=[1, 1, 1, 0]),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertEqual([r["date"] for r in rows], ["2026-08-01", "2026-09-10"])
        self.assertAlmostEqual(rows[-1]["harvested_pct"], 66.7)
        self.assertEqual(rows[-1]["valid_pct"], 75.0)

    def test_regrowth_delta_not_negative(self) -> None:
        scenes = [
            _scene("2026-08-01", [0.8] * 4),
            _scene("2026-09-01", [0.1] * 4),
            _scene("2026-09-15", [0.1, 0.1, 0.5, 0.5]),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertEqual(rows[1]["harvested_pct"], 100.0)
        self.assertEqual(rows[1]["status"], "harvested")
        self.assertEqual(rows[2]["newly_harvested_pct"], 0.0)

    def test_one_scene_per_day_prefers_official(self) -> None:
        scenes = [
            _scene("2026-08-01", [0.8] * 4),
            _scene("2026-09-01", [0.1] * 4, official=False),
            _scene("2026-09-01", [0.8] * 2 + [0.1] * 2, official=True),
        ]
        rows = compute_harvest_series(scenes, thresholds=THR)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["harvested_pct"], 50.0)
        self.assertTrue(rows[1]["official"])

    def test_valid_values_rejects_garbage(self) -> None:
        values, total = valid_ndvi_values(
            [{"NDVI": "x"}, {"NDVI": float("nan")}, {"ndvi": 0.4}, "bad", {"NDVI": 5}]
        )
        self.assertEqual(values, [0.4])
        self.assertEqual(total, 4)

    def test_thresholds_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {"HARVEST_PROGRESS_NDVI_MAX": "0.25", "HARVEST_PROGRESS_LOOKBACK_DAYS": "5"},
        ):
            thr = HarvestProgressThresholds.from_env()
        self.assertEqual(thr.ndvi_max, 0.25)
        self.assertEqual(thr.lookback_days, 30)  # 下限保护
        self.assertIn("0.25", thr.rule_zh())


class SceneResultTargetTests(unittest.TestCase):
    def test_extracts_s2_land_and_date(self) -> None:
        from app.services.harvest_progress import harvest_target_from_scene_result

        env = {"extras": {"land_id": "L1", "date": "2026-09-30", "sensor": "S2"}}
        self.assertEqual(
            harvest_target_from_scene_result(env, {"scene_upserts": 1}),
            ("L1", date(2026, 9, 30)),
        )
        self.assertIsNone(
            harvest_target_from_scene_result(
                {"extras": {"land_id": "L1", "sensor": "S1"}}, {"scene_upserts": 1}
            )
        )
        self.assertIsNone(harvest_target_from_scene_result(env, {"scene_upserts": 0}))
        self.assertEqual(
            harvest_target_from_scene_result(
                {"apply": {"result": {"land_id": "L2"}}},
                {"apply": {"oss": {"scene_upserts": 2}}},
            ),
            ("L2", None),
        )


class HarvestProgressEndpointTests(unittest.TestCase):
    def _call(self, rows, *, include_zero: bool, stored: bool = True):
        from app.routers import agri

        db = MagicMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        with (
            patch.object(agri, "_agri_ready", AsyncMock()),
            patch(
                "app.services.harvest_progress.load_land_area_mu",
                AsyncMock(return_value=(True, 30.0)),
            ),
            patch(
                "app.services.harvest_progress.list_stored",
                AsyncMock(return_value=rows if stored else []),
            ),
            patch(
                "app.services.harvest_progress.compute_land_series",
                AsyncMock(return_value=rows),
            ),
            patch("app.services.harvest_progress.enqueue_lands", AsyncMock()) as enq,
        ):
            out = asyncio.run(
                agri.get_harvest_progress(
                    "L1",
                    MagicMock(),
                    db,
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 10, 9),
                    include_zero=include_zero,
                )
            )
        return out, enq

    ROWS = [
        {"date": "2026-08-01", "harvested_pct": 0.0, "newly_harvested_pct": 0.0,
         "harvested_area_mu": 0.0, "status": "growing", "valid_pct": 100.0},
        {"date": "2026-09-20", "harvested_pct": 40.0, "newly_harvested_pct": 40.0,
         "harvested_area_mu": 12.0, "status": "harvesting", "valid_pct": 90.0},
    ]

    def test_excludes_zero_by_default(self) -> None:
        out, _ = self._call(self.ROWS, include_zero=False)
        self.assertEqual([str(i.date) for i in out.items], ["2026-09-20"])
        self.assertEqual(out.source, "stored")
        self.assertTrue(out.heuristic)
        self.assertEqual(out.parcel_area_mu, 30.0)

    def test_include_zero_and_live_fallback_enqueues(self) -> None:
        out, enq = self._call(self.ROWS, include_zero=True, stored=False)
        self.assertEqual(len(out.items), 2)
        self.assertEqual(out.source, "live")
        enq.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
