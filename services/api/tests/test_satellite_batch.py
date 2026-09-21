"""验证米制聚合边界、请求兼容和批量任务的完整派发。"""

import unittest
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pyproj import CRS, Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform

from app.routers.satellite_batch import backfill_satellite_batch
from app.schemas.satellite_batch import SatelliteBatchRequest
from app.services.satellite_batch import group_satellite_lands
from app.services.smart_land_backfill import ensure_land_parcels, run_smart_land_backfill


def local_land(land_id, x=0, y=0, size=100, latitude=35):
    local = CRS.from_proj4(
        f"+proj=aeqd +lat_0={latitude} +lon_0=110 +datum=WGS84 +units=m"
    )
    to_wgs = Transformer.from_crs(local, 4326, always_xy=True)
    geom = transform(
        to_wgs.transform, box(x - size / 2, y - size / 2, x + size / 2, y + size / 2)
    )
    return SimpleNamespace(
        land_id=land_id, boundary_geojson=mapping(geom), boundary_srid=4326
    )


def fake_db(lands):
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = lands
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    return db


class GroupTests(unittest.TestCase):
    def test_nearby_and_distant_lands_have_one_assignment(self):
        lands = [local_land("C", 8000), local_land("B", 2000), local_land("A")]
        groups = group_satellite_lands(lands)
        self.assertEqual([group.land_ids for group in groups], [["A", "B"], ["C"]])
        self.assertLess(
            groups[0].download_bbox[2] - groups[0].download_bbox[0],
            groups[0].aggregation_bbox[2] - groups[0].aggregation_bbox[0],
        )

    def test_rectangle_corner_and_full_boundary(self):
        lands = [local_land("A"), local_land("B", 2200, 2200), local_land("C", 2490)]
        self.assertEqual(
            [group.land_ids for group in group_satellite_lands(lands)],
            [["A", "B"], ["C"]],
        )

    def test_high_latitude_uses_kilometers(self):
        lands = [
            local_land("A", latitude=70),
            local_land("B", 2200, latitude=70),
            local_land("C", 3000, latitude=70),
        ]
        self.assertEqual(
            [group.land_ids for group in group_satellite_lands(lands)],
            [["A", "B"], ["C"]],
        )

    def test_oversized_land_keeps_full_bounds_and_is_not_grouped(self):
        land = local_land("A", size=6000)
        groups = group_satellite_lands([land, local_land("B")])
        self.assertTrue(groups[0].oversized)
        self.assertEqual(groups[0].land_ids, ["A"])
        self.assertEqual(groups[0].download_bbox, shape(land.boundary_geojson).bounds)

    def test_invalid_geometry_is_rejected(self):
        land = local_land("A")
        land.boundary_geojson = {"type": "Point", "coordinates": [110, 35]}
        with self.assertRaisesRegex(ValueError, "地块A"):
            group_satellite_lands([land])


class RequestTests(unittest.TestCase):
    def test_only_land_ids_defaults_to_three_years_through_today(self):
        body = SatelliteBatchRequest.model_validate({"landIdList": ["A"]})
        today = date.today()
        expected_day = 28 if (today.month, today.day) == (2, 29) else today.day
        self.assertEqual(body.months, 36)
        self.assertEqual(body.date_to, today)
        self.assertEqual(
            body.date_from, date(today.year - 3, today.month, expected_day)
        )

    def test_three_year_calendar_range_and_leap_day(self):
        for end, start in (
            ("2026-09-16", date(2023, 9, 16)),
            ("2024-02-29", date(2021, 2, 28)),
        ):
            with self.subTest(end=end):
                body = SatelliteBatchRequest.model_validate(
                    {"landIdList": ["A"], "date_to": end}
                )
                self.assertEqual(body.date_from, start)

    def test_aliases_numbers_and_duplicates(self):
        for alias in ("landIdList", "landIdlist", "land_ids"):
            body = SatelliteBatchRequest.model_validate(
                {alias: [123, " 123 ", "456"], "date_to": "2026-07-01"}
            )
            self.assertEqual(body.land_ids, ["123", "456"])

    def test_numeric_range_and_years_are_inclusive(self):
        body = SatelliteBatchRequest.model_validate(
            {
                "from_land_id": 1001,
                "to_land_id": 1003,
                "years": 2,
                "date_to": "2026-02-28",
            }
        )
        self.assertIsNone(body.land_ids)
        self.assertEqual(body.resolved_land_ids(), ["1001", "1002", "1003"])
        self.assertEqual(body.date_from, date(2024, 2, 28))

    def test_large_selection_is_accepted_and_capped_during_execution(self):
        body = SatelliteBatchRequest.model_validate(
            {"from_land_id": "15360", "to_land_id": "61229", "years": 3}
        )
        self.assertEqual(len(body.resolved_land_ids()), 45870)
        explicit = SatelliteBatchRequest.model_validate(
            {"landIdList": [str(value) for value in range(1001)]}
        )
        self.assertEqual(len(explicit.resolved_land_ids()), 1001)

    def test_list_and_range_are_mutually_exclusive(self):
        with self.assertRaises(ValidationError):
            SatelliteBatchRequest.model_validate(
                {"landIdList": ["A"], "from_land_id": "1", "to_land_id": "2"}
            )
        with self.assertRaises(ValidationError):
            SatelliteBatchRequest.model_validate(
                {"from_land_id": "A", "to_land_id": "B"}
            )

    def test_bad_lists_and_dates_are_rejected(self):
        for values in ([], [" "], [True], [None]):
            with self.assertRaises(ValidationError):
                SatelliteBatchRequest.model_validate({"landIdList": values})
        with self.assertRaises(ValidationError):
            SatelliteBatchRequest.model_validate(
                {
                    "landIdList": ["A"],
                    "date_from": "2026-08-02",
                    "date_to": "2026-08-01",
                }
            )


class BatchRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_caps_existing_lands_after_query(self):
        requested = [str(value) for value in range(1001)]
        db = fake_db([local_land(land_id) for land_id in requested])

        lands, summary = await ensure_land_parcels(
            db,
            requested,
            max_lands=1000,
            allow_partial=True,
        )

        self.assertEqual(len(lands), 1000)
        self.assertEqual(summary["selected_land_count"], 1000)
        self.assertEqual(summary["selected_land_ids"], requested[:1000])
        self.assertEqual(summary["skipped_land_count"], 1)

    async def test_smart_backfill_creates_parent_job_for_satellite_children(self):
        db = MagicMock()
        db.add = MagicMock()
        db.commit = AsyncMock()
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=db)
        session.__aexit__ = AsyncMock(return_value=None)
        child = SimpleNamespace(
            id=uuid.uuid4(),
            land_id="A",
            status="pending",
            error=None,
        )
        group = SimpleNamespace(land_ids=["A"])
        selection = {
            "requested_land_count": 1,
            "selected_land_ids": ["A"],
            "selected_land_count": 1,
            "skipped_land_count": 0,
            "source_sync": None,
        }

        with (
            patch("app.core.database.async_session", return_value=session),
            patch(
                "app.services.smart_land_backfill.ensure_land_parcels",
                new=AsyncMock(return_value=([local_land("A")], selection)),
            ),
            patch(
                "app.services.smart_land_backfill.build_satellite_batch_jobs",
                return_value=([group], [child]),
            ) as build_jobs,
            patch("app.mq_publish.publish_api_task") as publish,
        ):
            result = await run_smart_land_backfill(
                land_ids=["A"],
                date_from=date(2026, 1, 1),
                date_to=date(2026, 1, 1),
                sensors=["S2"],
            )

        parent = db.add.call_args_list[0].args[0]
        self.assertEqual(parent.type, "smart_land_backfill")
        self.assertIs(db.add.call_args_list[1].args[0], child)
        self.assertEqual(result["parent_job_id"], str(parent.id))
        self.assertEqual(result["selected_land_ids"], ["A"])
        self.assertEqual(build_jobs.call_args.kwargs["parent_job_id"], parent.id)
        self.assertEqual(publish.call_count, 1)

    async def test_single_day_creates_one_group_job_per_sensor_after_commit(self):
        db = fake_db([local_land("A"), local_land("B", 2000)])
        body = SatelliteBatchRequest.model_validate(
            {
                "landIdList": ["A", "B"],
                "date_from": "2026-08-01",
                "date_to": "2026-08-01",
            }
        )

        def publish(**kwargs):
            self.assertEqual(db.commit.await_count, 1)
            return kwargs["task_id"]

        with patch(
            "app.routers.satellite_batch.publish_api_task", side_effect=publish
        ) as send:
            response = await backfill_satellite_batch(body, None, db)
        self.assertEqual(
            (response.land_count, response.group_count, response.job_count), (2, 1, 2)
        )
        self.assertEqual(send.call_count, 2)
        jobs = [call.args[0] for call in db.add.call_args_list]
        self.assertEqual({job.params_json["sensor"] for job in jobs}, {"S1", "S2"})
        self.assertTrue(all(job.params_json["land_ids"] == ["A", "B"] for job in jobs))

    async def test_inclusive_chunks_do_not_miss_the_last_day(self):
        db = fake_db([local_land("A")])
        body = SatelliteBatchRequest.model_validate(
            {
                "landIdList": ["A"],
                "sensors": ["S2"],
                "date_from": "2026-08-01",
                "date_to": "2026-08-04",
            }
        )
        with (
            patch("app.routers.satellite_batch.settings.index_backfill_chunk_days", 3),
            patch("app.routers.satellite_batch.publish_api_task"),
        ):
            response = await backfill_satellite_batch(body, None, db)
        self.assertEqual(response.job_count, 2)
        jobs = [call.args[0] for call in db.add.call_args_list]
        self.assertEqual(jobs[1].params_json["date_from"], "2026-08-04")
        self.assertEqual(jobs[1].params_json["date_to"], "2026-08-04")

    async def test_missing_land_does_not_partially_queue(self):
        db = fake_db([local_land("A")])
        body = SatelliteBatchRequest.model_validate({"landIdList": ["A", "missing"]})
        with (
            patch("app.routers.satellite_batch.publish_api_task") as send,
            self.assertRaises(HTTPException) as error,
        ):
            await backfill_satellite_batch(body, None, db)
        self.assertEqual(error.exception.detail, {"missing_land_ids": ["missing"]})
        db.add.assert_not_called()
        db.commit.assert_not_awaited()
        send.assert_not_called()

    async def test_missing_land_is_synced_from_smart_before_queueing(self):
        initial = [local_land("1001")]
        final = [local_land("1001"), local_land("1002", 2000)]

        def result(lands):
            value = MagicMock()
            value.scalars.return_value.all.return_value = lands
            return value

        db = MagicMock()
        db.execute = AsyncMock(side_effect=[result(initial), result(final)])
        db.commit = AsyncMock()
        body = SatelliteBatchRequest.model_validate(
            {
                "from_land_id": "1001",
                "to_land_id": "1002",
                "years": 1,
                "date_to": "2026-08-01",
            }
        )
        with (
            patch("app.services.smart_land_backfill.settings.mysql_source_enabled", True),
            patch(
                "app.services.smart_land_backfill.sync_selected_lands",
                new_callable=AsyncMock,
                return_value={"status": "completed", "synced_land_ids": ["1002"]},
            ) as sync,
            patch("app.routers.satellite_batch.publish_api_task"),
        ):
            response = await backfill_satellite_batch(body, None, db)

        self.assertEqual(response.land_count, 2)
        sync.assert_awaited_once_with(
            ["1002"], include_excluded_schedule_lands=True
        )

    async def test_partial_dispatch_failure_returns_all_job_ids(self):
        db = fake_db([local_land("A")])
        body = SatelliteBatchRequest.model_validate(
            {"landIdList": ["A"], "date_from": "2026-08-01", "date_to": "2026-08-01"}
        )
        with (
            patch(
                "app.routers.satellite_batch.publish_api_task",
                side_effect=["ok", HTTPException(503, "queue down")],
            ),
            self.assertRaises(HTTPException) as error,
        ):
            await backfill_satellite_batch(body, None, db)
        self.assertEqual(len(error.exception.detail["queued_job_ids"]), 1)
        self.assertEqual(len(error.exception.detail["failed_job_ids"]), 1)
        self.assertEqual(db.add.call_args_list[1].args[0].status, "failed")


class HttpRouteTests(unittest.TestCase):
    def test_registered_endpoint_accepts_camelcase_ids(self):
        from app.core.database import get_db
        from app.main import app

        db = fake_db([local_land("123")])

        async def override_db():
            yield db

        app.dependency_overrides[get_db] = override_db
        try:
            with (
                patch("app.routers.satellite_batch.publish_api_task"),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/v1/lands/backfill-indices/batch",
                    json={
                        "landIdList": [123],
                    },
                )
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["groups"][0]["land_ids"], ["123"])
                body = SatelliteBatchRequest.model_validate({"landIdList": [123]})
                self.assertEqual(
                    response.json()["date_from"], body.date_from.isoformat()
                )
                self.assertEqual(response.json()["date_to"], date.today().isoformat())
                jobs = [call.args[0] for call in db.add.call_args_list]
                self.assertEqual(
                    min(job.params_json["date_from"] for job in jobs),
                    body.date_from.isoformat(),
                )
                self.assertEqual(
                    max(job.params_json["date_to"] for job in jobs),
                    date.today().isoformat(),
                )
                self.assertEqual(
                    {job.params_json["sensor"] for job in jobs}, {"S1", "S2"}
                )
                self.assertIn(
                    "/v1/lands/backfill-indices/batch", app.openapi()["paths"]
                )
        finally:
            app.dependency_overrides.pop(get_db, None)


if __name__ == "__main__":
    unittest.main()
