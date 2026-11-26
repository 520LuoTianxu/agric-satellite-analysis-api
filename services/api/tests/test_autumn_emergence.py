"""秋季出苗：本底与持续抬升、冻结后跳过、不写旧 NDVI 字段。"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import date

import numpy as np

from app.core.autumn_emergence import (
    METHOD_VERSION,
    NDVI_CONTEXT_MARK,
    STATUS_EMERGED,
    STATUS_EMERGING,
    STATUS_NOT_EMERGED,
    STATUS_UNCERTAIN,
    DateStack,
    analyze,
    autumn_season_key,
    is_winter_wheat,
    pixels_lonlat_for_date,
)
from agric_satellite_analysis_common.biweekly.metric_specs import METRIC_SPECS

from app.schemas.autumn_emergence import (
    AutumnEmergenceOut,
    AutumnEmergencePixelsOut,
    class_labels,
)
from app.services import autumn_emergence as svc


def _stack(
    day: str, values: list[float], *, ndre: list[float] | None = None
) -> DateStack:
    n = len(values)
    return DateStack(
        day=date.fromisoformat(day),
        lon=np.array([115.0 + i * 1e-4 for i in range(n)], dtype=float),
        lat=np.full(n, 34.82, dtype=float),
        ndvi=np.array(values, dtype=float),
        ndre=None if ndre is None else np.array(ndre, dtype=float),
    )


def _soil(i: int, wobble: float) -> float:
    return 0.11 + (i % 5) * 0.004 + wobble


def _plateau_series(n: int = 40) -> list[DateStack]:
    """三期低平台，然后三期明显抬升。抬升后多数像元仍低于 0.30。"""
    wobble = (0.0, 0.003, -0.002)
    days = (
        "2026-10-28",
        "2026-10-30",
        "2026-11-02",
        "2026-11-09",
        "2026-11-14",
        "2026-11-24",
    )
    levels = (0.0, 0.0, 0.0, 0.045, 0.09, 0.12)
    stacks = []
    for day, level, delta in zip(days, levels, wobble + (0.0, 0.0, 0.0), strict=True):
        values = [_soil(i, delta) + level for i in range(n)]
        stacks.append(_stack(day, values))
    # 平台首日两个尖峰，不应把噪声抬到植被水平。
    stacks[0].ndvi[0] = 0.62
    stacks[0].ndvi[1] = 0.58
    return stacks


class _Row:
    def __init__(self, data: dict | None) -> None:
        self._mapping = data or {}


class _Result:
    def __init__(self, one=None, many=None) -> None:
        self._one = one
        self._many = [] if many is None else many

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many


class FakeDB:
    def __init__(self, crop: str = "冬小麦", scenes: list[dict] | None = None) -> None:
        self.crop = crop
        self.scenes = scenes or []
        self.row: dict | None = None
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        bound = dict(params or {})
        self.calls.append((sql, bound))
        if "FROM agric_satellite.land_parcels" in sql:
            return _Result(
                one=_Row({"land_id": bound["land_id"], "crop_type": self.crop})
            )
        if (
            "FROM agric_satellite.parcel_autumn_emergence" in sql
            and "INSERT" not in sql
        ):
            return _Result(one=None if self.row is None else _Row(self.row))
        if "parcel_scene_products" in sql:
            return _Result(many=[_Row(row) for row in self.scenes])
        if sql.lstrip().startswith(
            "INSERT INTO agric_satellite.parcel_autumn_emergence"
        ):
            if self.row and self.row.get("frozen"):
                return _Result(one=None)
            self.row = dict(bound)
            return _Result(one=_Row({"land_id": bound["land_id"]}))
        return _Result()

    def sql_text(self) -> str:
        return "\n".join(sql for sql, _params in self.calls)


class MemStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type=None) -> str:
        del content_type
        self.objects[key] = data
        return key

    def get_bytes(self, key: str, max_bytes: int | None = None) -> bytes:
        del max_bytes
        return self.objects[key]


def _scene_row(day: str, values: list[float]) -> dict:
    pixels = [
        {
            "lon": 115.0 + i * 1e-4,
            "lat": 34.82,
            "NDVI": value,
            "clear": 1,
        }
        for i, value in enumerate(values)
    ]
    return {
        "date": date.fromisoformat(day),
        "scene_id": f"S2_{day}",
        "pixel_data": {"format": "lonlat_v1", "pixels": pixels},
        "pixel_count": len(pixels),
        "source": "planetary",
        "decloud_quality": None,
        "parcel_cloud_cover_pct": 0.0,
        "cloud_cover": 1.0,
        "cloud_cover_over_30": False,
        "parcel_cloud_source": "lonlat_clear",
    }


def _rows_from_stacks(stacks: list[DateStack]) -> list[dict]:
    return [_scene_row(stack.day.isoformat(), stack.ndvi.tolist()) for stack in stacks]


class ClassificationTests(unittest.TestCase):
    def test_low_plateau_then_sustained_rise_is_emerged_below_0_30(self) -> None:
        result = analyze(_plateau_series())
        self.assertEqual(result.status, STATUS_EMERGED)
        self.assertEqual(result.status_zh, "已出苗")
        self.assertTrue(result.emerged_flag)
        self.assertEqual(
            [d.isoformat() for d in result.baseline_dates],
            ["2026-10-28", "2026-10-30", "2026-11-02"],
        )
        self.assertIsNotNone(result.noise_floor)
        assert result.noise_floor is not None
        self.assertLess(result.noise_floor, 0.03)
        self.assertNotEqual(result.noise_floor, 0.035)
        self.assertNotEqual(result.noise_floor, NDVI_CONTEXT_MARK)
        self.assertGreater(result.noise_floor, 0.0)
        self.assertEqual(result.summary["method_version"], METHOD_VERSION)
        self.assertEqual(result.summary["noise_floor"], result.noise_floor)
        self.assertEqual(result.share_ndvi_ge_0_30, 0.0)
        self.assertGreaterEqual(result.share_sustained or 0.0, 0.80)
        self.assertIsNotNone(result.first_emerged_date)
        assert result.first_emerged_date is not None
        self.assertGreaterEqual(result.first_emerged_date, date(2026, 11, 9))
        self.assertNotIn(result.first_emerged_date, result.baseline_dates)
        self.assertIn(
            "不是精确出苗日", result.to_payload("33052", "2026-autumn")["disclaimer_zh"]
        )
        # 像元类别用的是算出来的噪声，不是 0.30。
        assert result.classes is not None and result.ndvi is not None
        assert result.baseline_ndvi is not None
        increment = result.ndvi[:, -1] - result.baseline_ndvi
        sustained = result.classes == 2
        self.assertTrue(np.all(increment[sustained] > result.noise_floor))
        self.assertTrue(np.all(result.ndvi[sustained, -1] < NDVI_CONTEXT_MARK))

    def test_flat_high_ndvi_is_not_emerged(self) -> None:
        days = ("2026-10-28", "2026-10-30", "2026-11-02", "2026-11-09", "2026-11-14")
        stacks = []
        for index, day in enumerate(days):
            values = [
                0.46 + (i % 4) * 0.004 + (0.002 if index == 1 else 0.0)
                for i in range(40)
            ]
            stacks.append(_stack(day, values))
        result = analyze(stacks)
        self.assertEqual(result.status, STATUS_NOT_EMERGED)
        self.assertFalse(result.emerged_flag)
        self.assertEqual(result.share_ndvi_ge_0_30, 1.0)
        self.assertIsNone(result.first_emerged_date)

    def test_flat_soil_is_not_emerged(self) -> None:
        days = ("2026-10-28", "2026-10-30", "2026-11-02", "2026-11-09")
        stacks = [
            _stack(day, [_soil(i, 0.001 * (index % 2)) for i in range(40)])
            for index, day in enumerate(days)
        ]
        result = analyze(stacks)
        self.assertEqual(result.status, STATUS_NOT_EMERGED)
        self.assertFalse(result.emerged_flag)

    def test_partial_rise_stays_emerging_and_unfrozen(self) -> None:
        stacks = _plateau_series()
        for stack in stacks[3:]:
            stack.ndvi[12:] = np.array([_soil(i, 0.0) for i in range(12, 40)])
        result = analyze(stacks)
        self.assertEqual(result.status, STATUS_EMERGING)
        self.assertFalse(result.emerged_flag)
        self.assertGreaterEqual(result.share_above_noise or 0.0, 0.25)
        self.assertLess(result.share_sustained or 1.0, 0.80)

    def test_rise_then_collapse_is_uncertain(self) -> None:
        stacks = _plateau_series()
        stacks[-1].ndvi[:] = np.array([_soil(i, 0.0) for i in range(40)])
        result = analyze(stacks)
        self.assertEqual(result.status, STATUS_UNCERTAIN)
        self.assertEqual(result.reason, "uncertain_signal_reversed")
        self.assertFalse(result.emerged_flag)

    def test_single_date_is_uncertain(self) -> None:
        result = analyze([_stack("2026-10-28", [_soil(i, 0.0) for i in range(40)])])
        self.assertEqual(result.status, STATUS_UNCERTAIN)
        self.assertFalse(result.emerged_flag)

    def test_spring_greenup_is_not_emergence(self) -> None:
        days = ("2027-03-01", "2027-03-06", "2027-03-11", "2027-03-20", "2027-03-28")
        levels = (0.12, 0.12, 0.20, 0.35, 0.55)
        stacks = [
            _stack(day, [level + (i % 3) * 0.002 for i in range(40)])
            for day, level in zip(days, levels, strict=True)
        ]
        result = analyze(stacks)
        self.assertEqual(result.status, STATUS_UNCERTAIN)
        self.assertEqual(result.reason, "uncertain_not_autumn")
        self.assertFalse(result.emerged_flag)
        self.assertIsNone(result.season_key)

    def test_ndre_does_not_override_flat_ndvi(self) -> None:
        days = ("2026-10-28", "2026-10-30", "2026-11-02", "2026-11-14", "2026-11-24")
        stacks = []
        for index, day in enumerate(days):
            ndvi = [_soil(i, 0.001 * (index % 2)) for i in range(40)]
            ndre = [0.05 + (0.08 if index >= 3 else 0.0) for _i in range(40)]
            stacks.append(_stack(day, ndvi, ndre=ndre))
        result = analyze(stacks)
        # NDRE 抬升不能把仍在 NDVI 噪声里的地块改成出苗。
        self.assertEqual(result.status, STATUS_NOT_EMERGED)
        self.assertFalse(result.emerged_flag)

    def test_pixel_payload_uses_increment_not_s2_ndvi_key(self) -> None:
        result = analyze(_plateau_series())
        payload = result.to_payload("33052", "2026-autumn")
        assert payload is not None
        points = pixels_lonlat_for_date(payload, "2026-11-24")
        self.assertGreater(points["pixel_count"], 0)
        sample = points["pixels_lonlat"][0]
        self.assertNotIn("NDVI", sample)
        self.assertIn("increment", sample)
        self.assertIn("class", sample)
        self.assertEqual(payload["kind"], "autumn_emergence")
        self.assertNotIn("s1s2_parcel", json.dumps(payload))

    def test_product_ndvi_reference_is_unchanged(self) -> None:
        self.assertEqual(METRIC_SPECS["ndvi"].low_reference, 0.3)


class SeasonAndCropTests(unittest.TestCase):
    def test_autumn_window_excludes_spring(self) -> None:
        self.assertEqual(autumn_season_key(date(2026, 10, 28)), "2026-autumn")
        self.assertEqual(autumn_season_key(date(2027, 1, 20)), "2026-autumn")
        self.assertIsNone(autumn_season_key(date(2027, 2, 1)))
        self.assertIsNone(autumn_season_key(date(2027, 3, 15)))

    def test_winter_wheat_only(self) -> None:
        self.assertTrue(is_winter_wheat("冬小麦"))
        self.assertTrue(is_winter_wheat("wheat"))
        self.assertTrue(is_winter_wheat("小麦"))
        self.assertFalse(is_winter_wheat("春小麦"))
        self.assertFalse(is_winter_wheat("spring wheat"))
        self.assertFalse(is_winter_wheat("玉米"))
        self.assertFalse(is_winter_wheat(None))


class PersistAndSkipTests(unittest.TestCase):
    def _run(self, db: FakeDB, storage: MemStorage, scene_date: date):
        return asyncio.run(svc.recompute_land(db, "33052", scene_date, storage=storage))

    def test_writes_new_record_and_oss_prefix_not_ndvi_columns(self) -> None:
        stacks = _plateau_series()
        db = FakeDB(scenes=_rows_from_stacks(stacks))
        storage = MemStorage()
        outcome = self._run(db, storage, date(2026, 11, 24))
        self.assertEqual(outcome["action"], "updated")
        self.assertEqual(outcome["status"], STATUS_EMERGED)
        self.assertTrue(outcome["frozen"])
        self.assertEqual(outcome["method_version"], METHOD_VERSION)
        blob = db.sql_text().lower()
        self.assertNotIn("ndvi_avg", blob)
        self.assertNotIn("ndvi_min", blob)
        self.assertNotIn("ndvi_max", blob)
        self.assertNotIn("field_stats", blob)
        self.assertNotIn("raster_layers", blob)
        self.assertNotIn("update agric_satellite.parcel_scene_products", blob)
        self.assertIn("insert into agric_satellite.parcel_autumn_emergence", blob)
        self.assertEqual(len(storage.objects), 1)
        key = next(iter(storage.objects))
        self.assertTrue(key.startswith("emergence/autumn/33052/2026-autumn/frozen-"))
        self.assertNotIn("s1s2_parcel", key)
        body = json.loads(storage.objects[key])
        self.assertEqual(body["kind"], "autumn_emergence")
        self.assertEqual(body["noise_floor"], outcome["noise_floor"])
        self.assertNotEqual(body["noise_floor"], 0.30)
        upserts = [
            p
            for sql, p in db.calls
            if "INSERT INTO agric_satellite.parcel_autumn_emergence" in sql
        ]
        self.assertEqual(len(upserts), 1)
        self.assertEqual(upserts[0]["noise_floor"], outcome["noise_floor"])
        self.assertTrue(upserts[0]["emerged_flag"])
        self.assertTrue(upserts[0]["frozen"])
        self.assertEqual(upserts[0]["method_version"], METHOD_VERSION)
        self.assertNotIn("ndvi_avg", upserts[0])
        summary = asyncio.run(
            svc.get_summary(db, "33052", "2026-autumn", enqueue=False)
        )
        parsed = AutumnEmergenceOut.model_validate(summary)
        self.assertEqual(parsed.status, "emerged")
        self.assertEqual(parsed.status_zh, "已出苗")
        self.assertTrue(parsed.frozen)
        self.assertTrue(parsed.pixel_oss_key.startswith("emergence/autumn/"))
        pixels = asyncio.run(
            svc.get_pixels(db, "33052", "2026-autumn", storage=storage)
        )
        layer = AutumnEmergencePixelsOut.model_validate(
            {**pixels, "class_labels": class_labels()}
        )
        self.assertGreater(layer.pixel_count, 0)
        self.assertEqual(layer.value, "increment")
        self.assertNotIn("NDVI", layer.pixels_lonlat[0].model_dump(by_alias=True))

    def test_skip_when_frozen_does_not_recompute_or_upload(self) -> None:
        stacks = _plateau_series()
        db = FakeDB(scenes=_rows_from_stacks(stacks))
        storage = MemStorage()
        first = self._run(db, storage, date(2026, 11, 24))
        self.assertEqual(first["action"], "updated")
        calls_after_first = len(db.calls)
        objects_after_first = dict(storage.objects)
        second = self._run(db, storage, date(2026, 11, 30))
        self.assertEqual(second["action"], "skipped_frozen")
        self.assertEqual(storage.objects, objects_after_first)
        new_sql = "\n".join(sql for sql, _p in db.calls[calls_after_first:])
        self.assertNotIn("parcel_scene_products", new_sql)
        self.assertNotIn("INSERT INTO agric_satellite.parcel_autumn_emergence", new_sql)
        self.assertNotIn("ndvi_avg", new_sql.lower())

    def test_other_crops_do_not_write(self) -> None:
        db = FakeDB(crop="玉米", scenes=_rows_from_stacks(_plateau_series()))
        storage = MemStorage()
        outcome = self._run(db, storage, date(2026, 11, 24))
        self.assertEqual(outcome["action"], "skipped_ineligible")
        self.assertEqual(storage.objects, {})
        self.assertNotIn("parcel_scene_products", db.sql_text())
        self.assertNotIn("ndvi_avg", db.sql_text().lower())

    def test_march_scene_is_not_an_emergence_target(self) -> None:
        target = svc.emergence_target_from_scene_result(
            {"land_id": "33052", "sensor": "S2", "date": "2027-03-20"},
            {"scene_upserts": 1},
        )
        self.assertEqual(target[0], "33052")
        self.assertIsNone(autumn_season_key(target[1]))
        skipped = asyncio.run(
            svc.enqueue_from_scene_result(
                {"land_id": "33052", "sensor": "S2", "date": "2027-03-20"},
                {"scene_upserts": 1},
            )
        )
        self.assertFalse(skipped)
        s1 = svc.emergence_target_from_scene_result(
            {"land_id": "33052", "sensor": "S1", "date": "2026-10-28"},
            {"scene_upserts": 1},
        )
        self.assertIsNone(s1)


if __name__ == "__main__":
    unittest.main()
