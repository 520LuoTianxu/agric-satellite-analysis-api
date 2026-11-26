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

    def first(self):
        if self._one is not None:
            return self._one
        return self._many[0] if self._many else None

    def scalar(self):
        row = self.first()
        if row is None:
            return None
        if hasattr(row, "_mapping"):
            values = list(row._mapping.values())
            return values[0] if values else None
        if isinstance(row, dict):
            return next(iter(row.values()), None)
        return row


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



class DailyAsOfTests(unittest.TestCase):
    """A day's result uses all clear official dates from sowing through that day."""

    def _through(self, last: str, *days_to_drop: str):
        stacks = [
            stack
            for stack in _plateau_series()
            if stack.day <= date.fromisoformat(last)
            and stack.day.isoformat() not in days_to_drop
        ]
        from agric_satellite_analysis_common.autumn_emergence import analyze_through

        return analyze_through(stacks, date.fromisoformat(last))

    def test_user_pattern_33052_baseline_then_unsustained_then_emerged(self) -> None:
        # Discussed 33052 pattern: 10-28/10-30/11-02 baseline, 11-14 unsustained,
        # 11-24 may become emerged. Drop the mid 11-09 date so 11-14 is first post.
        drop = ("2026-11-09",)
        mid = self._through("2026-11-02", *drop)
        self.assertEqual(mid.status, STATUS_NOT_EMERGED)
        self.assertFalse(mid.emerged_flag)
        if mid.classes is not None and mid.n_pixels:
            self.assertGreaterEqual(int((mid.classes == 0).sum()), mid.n_pixels - 2)

        first_rise = self._through("2026-11-14", *drop)
        self.assertEqual(first_rise.status, STATUS_EMERGING)
        self.assertFalse(first_rise.emerged_flag)
        self.assertIsNotNone(first_rise.classes)
        assert first_rise.classes is not None
        self.assertGreater(int((first_rise.classes == 1).sum()), 0)
        self.assertEqual(int((first_rise.classes == 2).sum()), 0)
        field = first_rise.to_daily_field("33052", sowing_date=date(2026, 10, 20))
        self.assertEqual(field["as_of_date"], "2026-11-14")
        self.assertEqual(field["status"], STATUS_EMERGING)
        self.assertIn("pixels", field)
        self.assertNotIn(2, field["pixels"]["class"])

        emerged = self._through("2026-11-24", *drop)
        self.assertEqual(emerged.status, STATUS_EMERGED)
        self.assertTrue(emerged.emerged_flag)
        self.assertGreaterEqual(emerged.share_sustained or 0.0, 0.80)
        assert emerged.classes is not None
        self.assertGreater(int((emerged.classes == 2).sum()), 0)
        daily = emerged.to_daily_field("33052")
        self.assertEqual(daily["as_of_date"], "2026-11-24")
        self.assertTrue(daily["emerged_flag"])
        from agric_satellite_analysis_common.autumn_emergence import (
            attach_daily_field,
            daily_field_of,
            pixels_from_daily_field,
        )

        product = {"land_id": "33052", "date": "2026-11-24", "pixel_data": {"source": "stac_direct"}}
        attach_daily_field(product, daily)
        self.assertEqual(daily_field_of(product)["status"], STATUS_EMERGED)
        points = pixels_from_daily_field(product["pixel_data"]["autumn_emergence"])
        self.assertGreater(points["pixel_count"], 0)
        self.assertIn("class", points["pixels_lonlat"][0])

    def test_daily_result_is_not_from_that_day_alone(self) -> None:
        stacks = _plateau_series()
        from agric_satellite_analysis_common.autumn_emergence import analyze_through

        only_last = analyze([stacks[-1]])
        through = analyze_through(stacks, stacks[-1].day)
        self.assertEqual(only_last.status, STATUS_UNCERTAIN)
        self.assertEqual(through.status, STATUS_EMERGED)


class SummaryFromDailyFieldsTests(unittest.TestCase):
    def test_get_summary_reads_daily_json_and_land_flag(self) -> None:
        emerged = analyze(_plateau_series())
        field = emerged.to_daily_field("33052")
        db = _ProductDB(
            land={
                "land_id": "33052",
                "crop_type": "冬小麦",
                "autumn_emerged": True,
                "autumn_emerged_season": "2026-autumn",
                "autumn_emerged_date": date(2026, 11, 24),
                "autumn_emerged_at": datetime(2026, 11, 24, 8, tzinfo=timezone.utc),
                "autumn_sowing_date": date(2026, 10, 20),
                "autumn_sowing_season": "2026-autumn",
            },
            daily={"2026-11-24": field},
        )
        view = asyncio.run(svc.get_summary(db, "33052", "2026-autumn"))
        assert view is not None
        self.assertTrue(view["eligible"])
        self.assertTrue(view["emerged_flag"])
        self.assertTrue(view["frozen"])
        self.assertEqual(view["status"], STATUS_EMERGED)
        self.assertEqual(view["sowing_date"], date(2026, 10, 20))
        self.assertEqual(len(view["timeseries"]), 1)
        self.assertIsNone(view["pixel_oss_key"])

    def test_get_pixels_uses_selected_day_field_not_oss(self) -> None:
        stacks = [
            s
            for s in _plateau_series()
            if s.day.isoformat() != "2026-11-09"
        ]
        mid = analyze([s for s in stacks if s.day <= date(2026, 11, 14)])
        last = analyze(stacks)
        db = _ProductDB(
            land={
                "land_id": "33052",
                "crop_type": "冬小麦",
                "autumn_emerged": False,
                "autumn_emerged_season": None,
                "autumn_sowing_date": date(2026, 10, 20),
                "autumn_sowing_season": "2026-autumn",
            },
            daily={
                "2026-11-14": mid.to_daily_field("33052"),
                "2026-11-24": last.to_daily_field("33052"),
            },
        )
        view = asyncio.run(
            svc.get_pixels(db, "33052", "2026-autumn", date(2026, 11, 14))
        )
        assert view is not None
        self.assertEqual(str(view["date"]), "2026-11-14")
        self.assertEqual(view["status"], STATUS_EMERGING)
        self.assertGreater(view["pixel_count"], 0)
        self.assertFalse(view.get("missing"))


class ManualTriggerNoCropRewriteTests(unittest.TestCase):
    def test_empty_crop_type_still_dispatches_and_never_writes_crop_type(self) -> None:
        created = {}

        async def fake_dispatch(db, land_id, **kwargs):
            created["land_id"] = land_id
            created["kwargs"] = kwargs
            return ["job-1"]

        db = _ProductDB(
            land={
                "land_id": "33052",
                "crop_type": "",
                "autumn_emerged": False,
                "autumn_emerged_season": None,
                "autumn_sowing_date": None,
                "autumn_sowing_season": None,
            }
        )
        with patch.object(svc, "dispatch_s2_download", fake_dispatch):
            scheduled = asyncio.run(
                svc.schedule_manual_emergence(
                    db, "33052", date(2026, 10, 20), "2026-autumn", today=date(2026, 11, 26)
                )
            )
        assert scheduled is not None
        self.assertEqual(scheduled["status"], "dispatched")
        self.assertEqual(scheduled["job_ids"], ["job-1"])
        self.assertEqual(scheduled["display_name"], "出苗")
        self.assertIn("sowing", db.updates)
        self.assertEqual(db.updates["sowing"]["sowing_date"], date(2026, 10, 20))
        self.assertNotIn("crop_type", db.updates["sowing"])
        updates = [sql for sql in db.executed_sql if "update" in sql.lower()]
        self.assertTrue(updates)
        self.assertFalse(any("crop_type" in sql.lower() for sql in updates))

    def test_frozen_season_does_not_dispatch(self) -> None:
        db = _ProductDB(
            land={
                "land_id": "33052",
                "crop_type": None,
                "autumn_emerged": True,
                "autumn_emerged_season": "2026-autumn",
                "autumn_sowing_date": date(2026, 10, 20),
                "autumn_sowing_season": "2026-autumn",
            }
        )
        scheduled = asyncio.run(
            svc.schedule_manual_emergence(
                db, "33052", date(2026, 10, 22), "2026-autumn", today=date(2026, 11, 26)
            )
        )
        assert scheduled is not None
        self.assertEqual(scheduled["status"], "frozen")
        self.assertEqual(scheduled["job_ids"], [])
        self.assertEqual(scheduled["sowing_date"], date(2026, 10, 20))
        self.assertNotIn("sowing", db.updates)


class _ProductDB:
    def __init__(self, *, land: dict, daily: dict | None = None, jobs: list | None = None) -> None:
        self.land = land
        self.daily = daily or {}
        self.jobs = jobs or []
        self.updates: dict = {}
        self.executed_sql: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(getattr(statement, "text", statement))
        self.executed_sql.append(sql)
        params = params or {}
        low = sql.lower()
        if "from agric_satellite.land_parcels" in low and "select" in low:
            if not self.land:
                return _Result()
            row = _Row(self.land)
            return _Result(one=row, many=[row])
        if "update agric_satellite.land_parcels" in low:
            self.updates["sowing"] = dict(params)
            return _Result([])
        if "pg_advisory_xact_lock" in low:
            return _Result([])
        if "from agric_satellite.parcel_scene_products" in low:
            rows = []
            for day, field in self.daily.items():
                rows.append(
                    {
                        "date": date.fromisoformat(day),
                        "scene_id": f"S2-{day}",
                        "pixel_data": {"autumn_emergence": field, "source": "stac_direct"},
                        "json_oss_key": f"s1s2_parcel/json/33052/{day}_S2.json",
                        "parcel_cloud_cover_pct": 0,
                        "cloud_cover": 0,
                        "product_source": "stac_direct",
                    }
                )
            return _Result(many=rows)
        if "from agric_satellite.jobs" in low:
            return _Result(many=self.jobs)
        return _Result([])

    async def get(self, model, key):
        if self.land and str(self.land.get("land_id")) == str(key):
            return SimpleNamespace(**self.land, deleted_at=None)
        return None


class ServiceSurfaceTests(unittest.TestCase):
    def test_api_no_longer_computes_or_runs_outbox(self) -> None:
        self.assertFalse(hasattr(svc, "recompute_land"))
        self.assertFalse(hasattr(svc, "run_autumn_emergence_outbox"))
        self.assertFalse(hasattr(svc, "enqueue_from_scene_result"))
        source = inspect.getsource(svc)
        self.assertNotIn("mkdir(", source)
        self.assertNotIn("put_bytes(", source)
        self.assertNotIn("get_parcel_product_storage", source)
        self.assertNotIn("parcel_autumn_emergence_outbox", source)
