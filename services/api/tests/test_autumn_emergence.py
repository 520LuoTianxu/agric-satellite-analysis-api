"""秋季出苗：本底与持续抬升、冻结后跳过、不写旧 NDVI 字段。"""

from __future__ import annotations

import asyncio
import inspect
import json
import unittest
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

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
    AutumnEmergenceScheduleOut,
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
    def __init__(
        self, crop: str | None = "冬小麦", scenes: list[dict] | None = None
    ) -> None:
        self.crop = crop
        self.scenes = scenes or []
        self.row: dict | None = None
        self.request: dict | None = None
        self.enqueued: list[dict] = []
        self.outbox_status: str | None = None
        self.open_jobs: list[dict] = []
        self.job_progress: list[dict] = []
        self.calls: list[tuple[str, dict]] = []

    async def get(self, model, key):
        del model
        return type("Land", (), {"land_id": str(key), "deleted_at": None})()

    async def commit(self):
        self.committed = True

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        bound = dict(params or {})
        self.calls.append((sql, bound))
        if "parcel_autumn_emergence_requests" in sql:
            if "INSERT" in sql:
                self.request = {
                    "land_id": bound.get("land_id"),
                    "season_key": bound.get("season_key"),
                    "sowing_date": bound.get("sowing_date"),
                    "status": bound.get("status"),
                    "dispatch": bound.get("dispatch"),
                    "date_from": bound.get("date_from"),
                    "date_to": bound.get("date_to"),
                    "job_ids": bound.get("job_ids"),
                }
                return _Result()
            if "UPDATE" in sql:
                if self.request is not None:
                    self.request["status"] = bound.get("status", "ready")
                return _Result()
            return _Result(one=None if self.request is None else _Row(self.request))
        if "parcel_autumn_emergence_outbox" in sql and "INSERT" in sql:
            self.enqueued.append(bound)
            self.outbox_status = "pending"
            return _Result()
        if "parcel_autumn_emergence_outbox" in sql and "SELECT" in sql:
            if self.outbox_status is None:
                return _Result(one=None)
            return _Result(one=_Row({"status": self.outbox_status}))
        if "work_items" in sql:
            return _Result(many=[_Row(row) for row in self.job_progress])
        if "FROM agric_satellite.jobs" in sql:
            return _Result(many=[_Row(row) for row in self.open_jobs])
        if "FROM agric_satellite.land_parcels" in sql:
            return _Result(
                one=_Row({"land_id": bound["land_id"], "crop_type": self.crop})
            )
        if (
            "FROM agric_satellite.parcel_autumn_emergence" in sql
            and "requests" not in sql
            and "INSERT" not in sql
        ):
            return _Result(one=None if self.row is None else _Row(self.row))
        if "parcel_scene_products" in sql:
            start = bound.get("date_from")
            end = bound.get("date_to")
            rows = self.scenes
            if start is not None or end is not None:
                rows = [
                    row
                    for row in rows
                    if (start is None or row["date"] >= start)
                    and (end is None or row["date"] <= end)
                ]
            return _Result(many=[_Row(row) for row in rows])
        if (
            "INSERT INTO agric_satellite.parcel_autumn_emergence" in sql
            and "requests" not in sql
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


class _Session:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    async def __aenter__(self) -> FakeDB:
        return self.db

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False


def _forbid_download(*_args, **_kwargs):
    raise AssertionError("API host must not download imagery")


class ManualTriggerTests(unittest.TestCase):
    def test_manual_pull_always_creates_claimable_s2_job(self) -> None:
        db = FakeDB(crop="", scenes=[])
        created: dict = {}

        async def create(_db, lands, **kwargs):
            created["lands"] = lands
            created["kwargs"] = kwargs
            return [], [SimpleNamespace(id="job-s2-1")], {}

        with (
            patch(
                "app.services.satellite_batch.create_satellite_batch_jobs",
                new=create,
            ),
            patch("httpx.Client", _forbid_download),
            patch("httpx.AsyncClient", _forbid_download),
            patch("urllib.request.urlopen", _forbid_download),
        ):
            payload = asyncio.run(
                svc.schedule_manual_emergence(
                    db,
                    "33052",
                    date(2026, 10, 15),
                    "2026-autumn",
                    today=date(2026, 11, 26),
                )
            )
        schedule = AutumnEmergenceScheduleOut.model_validate(payload)
        self.assertEqual(schedule.status, "dispatched")
        self.assertEqual(schedule.claim_type, "satellite_batch")
        self.assertEqual(schedule.display_name, "出苗")
        self.assertEqual(schedule.purpose, "autumn_emergence")
        self.assertEqual(schedule.job_status, "pending")
        self.assertEqual(schedule.sensors, ["S2"])
        self.assertEqual(schedule.job_ids, ["job-s2-1"])
        self.assertEqual(schedule.sowing_date, date(2026, 10, 15))
        self.assertEqual(schedule.date_from, date(2026, 10, 15))
        self.assertEqual(schedule.date_to, date(2026, 11, 26))
        self.assertNotIn("pending 为 false", schedule.message)
        kwargs = created["kwargs"]
        self.assertEqual(tuple(kwargs["sensors"]), ("S2",))
        self.assertNotIn("S1", kwargs["sensors"])
        self.assertEqual(kwargs["date_from"], date(2026, 10, 15))
        self.assertEqual(kwargs["date_to"], date(2026, 11, 26))
        self.assertFalse(kwargs["force"])
        self.assertEqual(kwargs["dispatch_extras"]["display_name"], "出苗")
        self.assertEqual(kwargs["dispatch_extras"]["purpose"], "autumn_emergence")
        extra = kwargs["extra_params"]
        self.assertTrue(extra["autumn_emergence_manual"])
        self.assertEqual(extra["display_name"], "出苗")
        self.assertTrue(extra["bypass_schedule_land_filter"])
        self.assertNotIn("is_backfill", extra)
        self.assertEqual(db.enqueued, [])
        self.assertEqual(db.crop, "")
        self.assertNotIn("parcel_scene_products", db.sql_text())
        self.assertNotIn("update agric_satellite.land_parcels", db.sql_text().lower())
        for fn in (svc.schedule_manual_emergence, svc.dispatch_s2_download):
            source = inspect.getsource(fn)
            self.assertNotIn("httpx", source)
            self.assertNotIn("urlopen", source)
            self.assertNotIn("rasterio", source)
            self.assertNotIn("app.tasks", source)
        db.open_jobs = [{"id": "job-s2-1", "status": "pending"}]
        waiting = asyncio.run(
            svc.recompute_land(db, "33052", date(2026, 11, 26), storage=MemStorage())
        )
        self.assertEqual(waiting["action"], "waiting_for_scenes")
        self.assertIsNone(db.row)
        db.open_jobs = []
        finished = asyncio.run(
            svc.recompute_land(db, "33052", date(2026, 11, 26), storage=MemStorage())
        )
        self.assertEqual(finished["action"], "updated")
        self.assertEqual(finished["status"], "uncertain")

    def test_open_s2_job_is_not_dispatched_twice(self) -> None:
        db = FakeDB(crop=None, scenes=[])
        calls = {"n": 0}

        async def create(_db, _lands, **_kwargs):
            calls["n"] += 1
            return [], [SimpleNamespace(id="job-s2-1")], {}

        with patch(
            "app.services.satellite_batch.create_satellite_batch_jobs",
            new=create,
        ):
            first = asyncio.run(
                svc.schedule_manual_emergence(
                    db,
                    "33052",
                    date(2026, 10, 15),
                    today=date(2026, 11, 26),
                )
            )
            db.open_jobs = [{"id": "job-s2-1", "status": "pending"}]
            second = asyncio.run(
                svc.schedule_manual_emergence(
                    db,
                    "33052",
                    date(2026, 10, 15),
                    today=date(2026, 11, 26),
                )
            )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first["season_key"], "2026-autumn")
        self.assertEqual(first["status"], "dispatched")
        self.assertEqual(second["status"], "already_running")
        self.assertEqual(second["job_ids"], ["job-s2-1"])
        self.assertIn("未重复派发", second["message"])
        self.assertEqual(db.crop, None)

    def test_ingested_clear_s2_still_creates_job_then_callback_computes(self) -> None:
        sowing = date(2026, 10, 15)
        scenes = _rows_from_stacks(_plateau_series())
        scenes.append(_scene_row("2026-09-20", [0.55] * 40))
        db = FakeDB(crop="", scenes=scenes)
        created: dict = {}

        async def create(_db, _lands, **kwargs):
            created["kwargs"] = kwargs
            return [], [SimpleNamespace(id="job-s2-existing")], {}

        with (
            patch(
                "app.services.satellite_batch.create_satellite_batch_jobs",
                new=create,
            ),
            patch("httpx.Client", _forbid_download),
            patch("urllib.request.urlopen", _forbid_download),
        ):
            payload = asyncio.run(
                svc.schedule_manual_emergence(
                    db,
                    "33052",
                    sowing,
                    "2026-autumn",
                    today=date(2026, 11, 26),
                )
            )
        schedule = AutumnEmergenceScheduleOut.model_validate(payload)
        self.assertEqual(schedule.status, "dispatched")
        self.assertEqual(schedule.job_ids, ["job-s2-existing"])
        self.assertEqual(schedule.sensors, ["S2"])
        self.assertFalse(created["kwargs"]["force"])
        self.assertEqual(db.enqueued, [])
        self.assertNotIn("parcel_scene_products", db.sql_text())
        self.assertEqual(db.request["dispatch"], "s2_download")
        self.assertEqual(db.request["sowing_date"], sowing)
        with patch(
            "app.core.database.async_session",
            return_value=_Session(db),
        ):
            queued = asyncio.run(
                svc.enqueue_after_satellite_job(
                    "33052",
                    {
                        "autumn_emergence_manual": True,
                        "purpose": "autumn_emergence",
                        "sowing_date": sowing.isoformat(),
                        "date_to": "2026-11-26",
                        "sensor": "S2",
                    },
                    "completed",
                )
            )
        self.assertTrue(queued)
        self.assertEqual(len(db.enqueued), 1)
        db.open_jobs = []
        storage = MemStorage()
        outcome = asyncio.run(
            svc.recompute_land(db, "33052", date(2026, 11, 26), storage=storage)
        )
        self.assertEqual(outcome["action"], "updated")
        self.assertEqual(outcome["sowing_date"], sowing.isoformat())
        self.assertEqual(db.crop, "")
        summary = json.loads(
            next(
                params["summary"]
                for sql, params in reversed(db.calls)
                if "INSERT INTO agric_satellite.parcel_autumn_emergence" in sql
                and "requests" not in sql
            )
        )
        self.assertNotIn("2026-09-20", summary["baseline_dates"])
        self.assertTrue(
            all(day >= sowing.isoformat() for day in summary["baseline_dates"])
        )

    def test_callback_computes_empty_crop_from_sowing_date(self) -> None:
        sowing = date(2026, 10, 15)
        scenes = _rows_from_stacks(_plateau_series())
        scenes.insert(0, _scene_row("2026-09-20", [0.55] * 40))
        db = FakeDB(crop="", scenes=scenes)
        db.request = {
            "land_id": "33052",
            "season_key": "2026-autumn",
            "sowing_date": sowing,
            "status": "scheduled",
            "dispatch": "s2_download",
            "date_from": sowing,
            "date_to": date(2026, 11, 26),
            "job_ids": "[]",
        }
        storage = MemStorage()
        with patch(
            "app.core.database.async_session",
            return_value=_Session(db),
        ):
            queued = asyncio.run(
                svc.enqueue_from_scene_result(
                    {"land_id": "33052", "sensor": "S2", "date": "2026-11-24"},
                    {"scene_upserts": 1},
                )
            )
            ignored = asyncio.run(
                svc.enqueue_from_scene_result(
                    {"land_id": "33052", "sensor": "S1", "date": "2026-11-24"},
                    {"scene_upserts": 1},
                )
            )
        self.assertTrue(queued)
        self.assertFalse(ignored)
        self.assertEqual(len(db.enqueued), 1)
        self.assertEqual(db.enqueued[0]["scene_date"], date(2026, 11, 24))
        outcome = asyncio.run(
            svc.recompute_land(db, "33052", date(2026, 11, 24), storage=storage)
        )
        self.assertEqual(outcome["action"], "updated")
        self.assertEqual(outcome["sowing_date"], sowing.isoformat())
        self.assertEqual(db.crop, "")
        self.assertNotIn("update agric_satellite.land_parcels", db.sql_text().lower())
        upserts = [
            params
            for sql, params in db.calls
            if "INSERT INTO agric_satellite.parcel_autumn_emergence" in sql
            and "requests" not in sql
        ]
        self.assertEqual(upserts[-1]["sowing_date"], sowing)
        summary = json.loads(upserts[-1]["summary"])
        self.assertEqual(summary["sowing_date"], sowing.isoformat())
        self.assertTrue(
            all(day >= sowing.isoformat() for day in summary["baseline_dates"])
        )
        self.assertNotIn("2026-09-20", summary["baseline_dates"])
        scene_windows = [
            params for sql, params in db.calls if "parcel_scene_products" in sql
        ]
        self.assertTrue(scene_windows)
        self.assertTrue(all(params["date_from"] == sowing for params in scene_windows))
        self.assertEqual(db.request["status"], "ready")
        view = asyncio.run(svc.get_summary(db, "33052", "2026-autumn", enqueue=False))
        parsed = AutumnEmergenceOut.model_validate(view)
        self.assertTrue(parsed.eligible)
        self.assertFalse(parsed.pending)
        self.assertEqual(parsed.sowing_date, sowing)
        self.assertEqual(parsed.method_version, METHOD_VERSION)
        self.assertIsNotNone(parsed.status)

    def test_automatic_hook_still_skips_empty_crop(self) -> None:
        for crop in ("", None):
            db = FakeDB(crop=crop, scenes=_rows_from_stacks(_plateau_series()))
            outcome = asyncio.run(
                svc.recompute_land(
                    db, "33052", date(2026, 11, 24), storage=MemStorage()
                )
            )
            self.assertEqual(outcome["action"], "skipped_ineligible")
            self.assertIsNone(db.row)
            self.assertEqual(db.crop, crop)
            self.assertNotIn(
                "update agric_satellite.land_parcels", db.sql_text().lower()
            )
            self.assertNotIn("parcel_scene_products", db.sql_text())
            view = asyncio.run(
                svc.get_summary(
                    db, "33052", "2026-autumn", today=date(2026, 11, 26), enqueue=False
                )
            )
            self.assertFalse(view["eligible"])
            self.assertFalse(view["pending"])

    def test_sowing_date_outside_autumn_window_is_rejected(self) -> None:
        db = FakeDB(crop="")
        with self.assertRaises(svc.EmergenceScheduleError) as caught:
            asyncio.run(
                svc.schedule_manual_emergence(
                    db,
                    "33052",
                    date(2026, 8, 20),
                    "2026-autumn",
                    today=date(2026, 11, 26),
                )
            )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(db.enqueued, [])
        self.assertIsNone(db.request)

    def test_frozen_season_does_not_download_or_rewrite_crop(self) -> None:
        db = FakeDB(crop="")
        db.row = {
            "frozen": True,
            "emerged_flag": True,
            "status": "emerged",
            "sowing_date": date(2026, 10, 1),
        }

        async def create(*_args, **_kwargs):
            raise AssertionError("frozen season must not dispatch")

        with patch(
            "app.services.satellite_batch.create_satellite_batch_jobs",
            new=create,
        ):
            payload = asyncio.run(
                svc.schedule_manual_emergence(
                    db,
                    "33052",
                    date(2026, 10, 20),
                    "2026-autumn",
                    today=date(2026, 11, 26),
                )
            )
        self.assertEqual(payload["status"], "frozen")
        self.assertEqual(payload["job_ids"], [])
        self.assertIsNone(payload["claim_type"])
        self.assertEqual(payload["sowing_date"], date(2026, 10, 1))
        self.assertEqual(db.enqueued, [])
        self.assertEqual(db.crop, "")
        self.assertNotIn("update agric_satellite.land_parcels", db.sql_text().lower())

    def test_plain_backfill_completion_does_not_enqueue_emergence(self) -> None:
        db = FakeDB(crop="冬小麦")
        with patch(
            "app.core.database.async_session",
            return_value=_Session(db),
        ):
            queued = asyncio.run(
                svc.enqueue_after_satellite_job(
                    "33052",
                    {"is_backfill": True, "sensor": "S2", "date_to": "2026-11-26"},
                    "completed",
                )
            )
            failed = asyncio.run(
                svc.enqueue_after_satellite_job(
                    "33052",
                    {"autumn_emergence_manual": True, "purpose": "autumn_emergence"},
                    "failed",
                )
            )
        self.assertFalse(queued)
        self.assertFalse(failed)
        self.assertEqual(db.enqueued, [])

    def test_get_exposes_job_progress_and_failure(self) -> None:
        db = FakeDB(crop="")
        db.request = {
            "land_id": "33052",
            "season_key": "2026-autumn",
            "sowing_date": date(2026, 10, 20),
            "status": "scheduled",
            "dispatch": "s2_download",
            "date_from": date(2026, 10, 20),
            "date_to": date(2026, 11, 26),
            "job_ids": '["job-running", "job-failed"]',
        }
        db.job_progress = [
            {
                "id": "job-running",
                "status": "running",
                "error": None,
                "progress_json": {"scenes_total": 4, "scenes_done": 1},
                "work_status": "leased",
                "lease_owner": "download-host-a",
                "last_claimed_by": "download-host-a",
                "work_error": None,
            },
            {
                "id": "job-failed",
                "status": "pending",
                "error": None,
                "progress_json": {"scenes_total": 2, "scenes_done": 0},
                "work_status": "pending",
                "lease_owner": None,
                "last_claimed_by": None,
                "work_error": None,
            },
        ]
        view = asyncio.run(svc.get_summary(db, "33052", "2026-autumn", enqueue=False))
        parsed = AutumnEmergenceOut.model_validate(view)
        self.assertTrue(parsed.eligible)
        self.assertTrue(parsed.pending)
        self.assertIsNone(parsed.status)
        self.assertEqual(parsed.display_name, "出苗")
        self.assertEqual(parsed.claim_type, "satellite_batch")
        self.assertEqual(parsed.job_status, "running")
        self.assertEqual(parsed.claimed_by, "download-host-a")
        self.assertEqual(parsed.job_ids, ["job-running", "job-failed"])
        self.assertEqual(parsed.running_jobs, 1)
        self.assertEqual(parsed.pending_jobs, 1)
        self.assertEqual(parsed.scenes_total, 6)
        self.assertEqual(parsed.scenes_done, 1)
        self.assertEqual(parsed.scenes_remaining, 5)
        self.assertEqual(parsed.date_from, date(2026, 10, 20))
        self.assertEqual(parsed.phase, "stac")
        self.assertIsNone(parsed.failure_reason)

        db.job_progress = [
            {
                "id": "job-failed",
                "status": "failed",
                "error": "STAC search failed",
                "progress_json": {"scenes_total": 2, "scenes_done": 0},
                "work_status": "completed",
                "lease_owner": None,
                "last_claimed_by": "download-host-b",
                "work_error": None,
            }
        ]
        db.request["job_ids"] = '["job-failed"]'
        failed_view = asyncio.run(
            svc.get_summary(db, "33052", "2026-autumn", enqueue=False)
        )
        failed = AutumnEmergenceOut.model_validate(failed_view)
        self.assertFalse(failed.pending)
        self.assertIsNone(failed.status)
        self.assertEqual(failed.job_status, "failed")
        self.assertEqual(failed.failure_reason, "STAC search failed")
        self.assertEqual(failed.claimed_by, "download-host-b")
        self.assertEqual(failed.phase, "failed")
        self.assertEqual(failed.failed_jobs, 1)

    def test_post_route_is_registered_beside_get(self) -> None:
        from app.routers.autumn_emergence import router

        path = "/agri/lands/{land_id}/autumn-emergence"
        matched = [
            route for route in router.routes if getattr(route, "path", None) == path
        ]
        methods = {method for route in matched for method in (route.methods or set())}
        self.assertIn("POST", methods)
        self.assertIn("GET", methods)


class _OrderDB:
    """记录 SQL 与 COMMIT 的先后，证明 outbox 写在任务提交之前。"""

    def __init__(self, inner: FakeDB, job: SimpleNamespace) -> None:
        self.inner = inner
        self.job = job
        self.order: list[str] = []

    async def get(self, model, key, with_for_update=False):
        del model, key, with_for_update
        return self.job

    async def execute(self, stmt, params=None):
        self.order.append(" ".join(str(stmt).split()))
        return await self.inner.execute(stmt, params)

    async def commit(self):
        self.order.append("COMMIT")
        await self.inner.commit()

    async def refresh(self, obj):
        del obj


def _emergence_job() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        land_id="33052",
        type="satellite_batch",
        parent_job_id=None,
        status="running",
        progress_json={"scenes_total": 19, "scenes_done": 19},
        error=None,
        params_json={
            "autumn_emergence_manual": True,
            "purpose": "autumn_emergence",
            "display_name": "出苗",
            "season_key": "2026-autumn",
            "sowing_date": "2026-10-20",
            "date_from": "2026-10-20",
            "date_to": "2026-11-26",
            "sensor": "S2",
            "force": False,
        },
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        finished_at=None,
    )


def _patch_status(db: FakeDB, job: SimpleNamespace, status: str):
    from app.routers import internal_jobs as jobs_mod

    scheduled: list[str] = []

    def _capture(coro, *args, **kwargs):
        del args, kwargs
        scheduled.append(getattr(coro.cr_code, "co_name", ""))
        coro.close()
        return object()

    session = _OrderDB(db, job)
    with patch("asyncio.create_task", _capture):
        out = asyncio.run(
            jobs_mod.patch_job(
                job.id,
                jobs_mod.InternalJobPatch(status=status),
                None,
                session,
            )
        )
    return out, session, scheduled


def _completed_pull_row() -> dict:
    return {
        "id": "job-done",
        "status": "completed",
        "error": None,
        "progress_json": {"scenes_total": 19, "scenes_done": 19},
        "work_status": "completed",
        "lease_owner": None,
        "last_claimed_by": "download-boen",
        "work_error": None,
    }


class CompletionEnqueueTests(unittest.TestCase):
    def test_job_completion_enqueues_compute_in_same_commit(self) -> None:
        from app.routers import internal_jobs as jobs_mod

        db = FakeDB(crop="")
        job = _emergence_job()
        out, session, scheduled = _patch_status(db, job, "completed")
        self.assertEqual(out.status, "completed")
        self.assertEqual(len(db.enqueued), 1)
        self.assertEqual(db.enqueued[0]["land_id"], "33052")
        self.assertEqual(db.enqueued[0]["delay"], 0)
        self.assertEqual(db.enqueued[0]["scene_date"], date(2026, 11, 26))
        insert_at = next(
            index
            for index, sql in enumerate(session.order)
            if "parcel_autumn_emergence_outbox" in sql and "INSERT" in sql
        )
        self.assertLess(insert_at, session.order.index("COMMIT"))
        self.assertNotIn("enqueue_after_satellite_job", scheduled)
        receipt = inspect.getsource(jobs_mod._enqueue_emergence_before_commit)
        self.assertNotIn("create_task", receipt)
        self.assertNotIn("enqueue_after_satellite_job", receipt)
        self.assertNotIn(
            "enqueue_after_satellite_job", inspect.getsource(jobs_mod.patch_job)
        )
        self.assertEqual(db.crop, "")
        self.assertNotIn("update agric_satellite.land_parcels", db.sql_text().lower())

        # 结果行已经在时，重复的成功回执不再把 outbox 打回 pending。
        done = FakeDB(crop="")
        done.row = {
            "land_id": "33052",
            "season_key": "2026-autumn",
            "status": "emerged",
            "pixel_oss_key": "emergence/autumn/33052/2026-autumn/frozen-abc",
            "frozen": True,
        }
        _out, again, _scheduled = _patch_status(done, _emergence_job(), "succeeded")
        self.assertEqual(done.enqueued, [])
        self.assertNotIn("parcel_autumn_emergence_outbox", "\n".join(again.order))

    def test_failed_and_backfill_jobs_do_not_enqueue_compute(self) -> None:
        failed_db = FakeDB(crop="")
        failed_job = _emergence_job()
        failed_job.error = "STAC search failed"
        out, session, scheduled = _patch_status(failed_db, failed_job, "failed")
        self.assertEqual(out.status, "failed")
        self.assertEqual(failed_db.enqueued, [])
        self.assertNotIn("parcel_autumn_emergence_outbox", "\n".join(session.order))
        self.assertNotIn("enqueue_after_satellite_job", scheduled)
        self.assertIn("COMMIT", session.order)

        backfill = FakeDB(crop="冬小麦")
        job = _emergence_job()
        job.params_json = {
            "is_backfill": True,
            "sensor": "S2",
            "date_to": "2026-11-26",
            "force": False,
        }
        _out, backfill_session, _scheduled = _patch_status(backfill, job, "completed")
        self.assertEqual(backfill.enqueued, [])
        self.assertNotIn(
            "parcel_autumn_emergence_outbox", "\n".join(backfill_session.order)
        )

    def test_skipped_scenes_still_compute_and_pixels_after_job_completes(self) -> None:
        sowing = date(2026, 10, 20)
        db = FakeDB(crop="", scenes=_rows_from_stacks(_plateau_series()))
        db.request = {
            "land_id": "33052",
            "season_key": "2026-autumn",
            "sowing_date": sowing,
            "status": "scheduled",
            "dispatch": "s2_download",
            "date_from": sowing,
            "date_to": date(2026, 11, 26),
            "job_ids": "[]",
        }
        with patch("app.core.database.async_session", return_value=_Session(db)):
            skipped = asyncio.run(
                svc.enqueue_from_scene_result(
                    {"land_id": "33052", "sensor": "S2", "date": "2026-11-24"},
                    {"scene_upserts": 0, "oss": {"scene_upserts": 0}},
                )
            )
        self.assertFalse(skipped)
        self.assertIsNone(
            svc.emergence_target_from_scene_result(
                {"land_id": "33052", "sensor": "S2", "date": "2026-11-24"},
                {"scene_upserts": 0},
            )
        )
        self.assertEqual(db.enqueued, [])

        out, session, scheduled = _patch_status(db, _emergence_job(), "completed")
        self.assertEqual(out.status, "completed")
        self.assertFalse(session.job.params_json["force"])
        self.assertEqual(len(db.enqueued), 1)
        self.assertLess(
            next(
                index
                for index, sql in enumerate(session.order)
                if "parcel_autumn_emergence_outbox" in sql
            ),
            session.order.index("COMMIT"),
        )
        self.assertNotIn("enqueue_after_satellite_job", scheduled)

        storage = MemStorage()
        missing = asyncio.run(
            svc.get_pixels(
                db, "33052", "2026-autumn", date(2026, 11, 24), storage=storage
            )
        )
        self.assertTrue(missing["missing"])
        outcome = asyncio.run(
            svc.recompute_land(db, "33052", date(2026, 11, 26), storage=storage)
        )
        self.assertEqual(outcome["action"], "updated")
        self.assertIsNotNone(outcome["status"])
        self.assertEqual(outcome["sowing_date"], sowing.isoformat())
        self.assertEqual(db.crop, "")
        self.assertNotIn("update agric_satellite.land_parcels", db.sql_text().lower())
        pixels = asyncio.run(
            svc.get_pixels(
                db, "33052", "2026-autumn", date(2026, 11, 24), storage=storage
            )
        )
        self.assertFalse(pixels["missing"])
        layer = AutumnEmergencePixelsOut.model_validate(
            {**pixels, "class_labels": class_labels()}
        )
        self.assertEqual(layer.date, date(2026, 11, 24))
        self.assertGreater(layer.pixel_count, 0)
        self.assertEqual(layer.status, outcome["status"])

    def test_get_reenqueues_when_completed_without_result(self) -> None:
        db = FakeDB(crop="")
        db.request = {
            "land_id": "33052",
            "season_key": "2026-autumn",
            "sowing_date": date(2026, 10, 20),
            "status": "scheduled",
            "dispatch": "s2_download",
            "date_from": date(2026, 10, 20),
            "date_to": date(2026, 11, 26),
            "job_ids": '["job-done"]',
        }
        db.job_progress = [_completed_pull_row()]

        def _forbid_download(*_args, **_kwargs):
            raise AssertionError("GET must not dispatch a download")

        with patch(
            "app.services.satellite_batch.create_satellite_batch_jobs",
            new=_forbid_download,
        ):
            view = asyncio.run(svc.get_summary(db, "33052", "2026-autumn"))
        parsed = AutumnEmergenceOut.model_validate(view)
        self.assertTrue(parsed.eligible)
        self.assertFalse(parsed.pending)
        self.assertIsNone(parsed.status)
        self.assertIsNone(parsed.pixel_oss_key)
        self.assertEqual(parsed.job_status, "completed")
        self.assertEqual(parsed.percent, 100.0)
        self.assertEqual(parsed.scenes_done, 19)
        self.assertEqual(parsed.scenes_total, 19)
        self.assertEqual(parsed.claimed_by, "download-boen")
        self.assertEqual(parsed.failed_jobs, 0)
        self.assertIsNone(parsed.failure_reason)
        self.assertEqual(len(db.enqueued), 1)
        self.assertEqual(db.enqueued[0]["land_id"], "33052")
        self.assertEqual(db.enqueued[0]["delay"], 0)
        self.assertEqual(db.enqueued[0]["scene_date"], date(2026, 11, 26))
        self.assertEqual(db.crop, "")
        self.assertNotIn("update agric_satellite.land_parcels", db.sql_text().lower())

        # 客户端继续轮询时，已在队列里的计算不能被清掉租约再入一次。
        polled = asyncio.run(svc.get_summary(db, "33052", "2026-autumn"))
        self.assertIsNone(polled["status"])
        self.assertEqual(len(db.enqueued), 1)

        db.enqueued.clear()
        asyncio.run(svc.get_summary(db, "33052", "2026-autumn", enqueue=False))
        self.assertEqual(db.enqueued, [])

        db.job_progress = [
            {
                "id": "job-failed",
                "status": "failed",
                "error": "STAC search failed",
                "progress_json": {"scenes_total": 19, "scenes_done": 0},
                "work_status": "failed",
                "lease_owner": None,
                "last_claimed_by": "download-boen",
                "work_error": None,
            }
        ]
        db.request["job_ids"] = '["job-failed"]'
        failed = asyncio.run(svc.get_summary(db, "33052", "2026-autumn"))
        self.assertEqual(failed["job_status"], "failed")
        self.assertFalse(failed["pending"])
        self.assertIsNone(failed["status"])
        self.assertEqual(db.enqueued, [])

        db.job_progress = [
            {
                "id": "job-running",
                "status": "running",
                "error": None,
                "progress_json": {"scenes_total": 19, "scenes_done": 4},
                "work_status": "leased",
                "lease_owner": "download-boen",
                "last_claimed_by": "download-boen",
                "work_error": None,
            }
        ]
        db.request["job_ids"] = '["job-running"]'
        running = asyncio.run(svc.get_summary(db, "33052", "2026-autumn"))
        self.assertEqual(running["job_status"], "running")
        self.assertTrue(running["pending"])
        self.assertEqual(db.enqueued, [])

        db.job_progress = [_completed_pull_row()]
        db.request["job_ids"] = '["job-done"]'
        db.row = {
            "season_key": "2026-autumn",
            "status": "emerged",
            "emerged_flag": True,
            "frozen": True,
            "sowing_date": date(2026, 10, 20),
            "pixel_oss_key": "emergence/autumn/33052/2026-autumn/frozen-abc",
            "summary": "{}",
            "timeseries": "[]",
        }
        ready = asyncio.run(svc.get_summary(db, "33052", "2026-autumn"))
        self.assertEqual(ready["status"], "emerged")
        self.assertEqual(db.enqueued, [])


if __name__ == "__main__":
    unittest.main()
