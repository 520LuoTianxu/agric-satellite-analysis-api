"""批量选地报告接口的纯单元测试。"""

import unittest
import uuid
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError
from pyproj import CRS, Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform

from app.models.tables import Job
from app.routers.assessment import AssessmentBatchRequest, _assessment_batch_id, create_assessment_reports_batch
from app.services.mysql_land_sync import _selected_source_query, sync_selected_lands
from app.services.satellite_batch import build_satellite_batch_jobs


def local_land(land_id: str, x: float = 0, y: float = 0):
    local = CRS.from_proj4(
        "+proj=aeqd +lat_0=35 +lon_0=110 +datum=WGS84 +units=m"
    )
    to_wgs = Transformer.from_crs(local, 4326, always_xy=True)
    geom = transform(
        to_wgs.transform, box(x - 100 / 2, y - 100 / 2, x + 100 / 2, y + 100 / 2)
    )
    return SimpleNamespace(
        land_id=land_id,
        boundary_geojson=mapping(geom),
        boundary_srid=4326,
    )


class AssessmentBatchRequestTests(unittest.TestCase):
    def test_aliases_numbers_duplicates_and_sensor_deduplication(self):
        for alias in ("landIdList", "landIdlist", "land_ids"):
            body = AssessmentBatchRequest.model_validate(
                {
                    alias: [123, " 123 ", "456"],
                    "sensors": ["S2", "S1", "S2"],
                    "date_from": " 2024-01-02T00:00:00 ",
                }
            )
            self.assertEqual(body.land_ids, ["123", "456"])
            self.assertEqual(body.sensors, ["S2", "S1"])
            self.assertEqual(body.date_from, "2024-01-02")

    def test_invalid_ids_are_rejected(self):
        for value in ([], [True], [None], [" "], ["A"] * 1001):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    AssessmentBatchRequest.model_validate({"landIdList": value})

    def test_batch_id_is_order_independent(self):
        first = AssessmentBatchRequest.model_validate({"landIdList": ["B", "A"]})
        second = AssessmentBatchRequest.model_validate({"landIdList": ["A", "B"]})
        args = {
            "date_from": "2023-09-20",
            "date_to": "2026-09-20",
            "crop_type": "rice",
        }
        self.assertEqual(
            _assessment_batch_id(first, **args), _assessment_batch_id(second, **args)
        )


class AssessmentBatchJobTests(unittest.TestCase):
    def test_builds_shared_jobs_with_stable_ids_and_inclusive_chunks(self):
        lands = [local_land("A"), local_land("B", 2000)]
        namespace = uuid.UUID("11111111-1111-1111-1111-111111111111")
        groups, jobs = build_satellite_batch_jobs(
            lands,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 4),
            sensors=["S1", "S2"],
            parent_job_id=namespace,
            id_namespace=namespace,
            chunk_days=3,
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].land_ids, ["A", "B"])
        self.assertEqual(len(jobs), 4)
        self.assertEqual(
            {
                (
                    job.params_json["sensor"],
                    job.params_json["date_from"],
                    job.params_json["date_to"],
                )
                for job in jobs
            },
            {
                ("S1", "2026-08-01", "2026-08-03"),
                ("S2", "2026-08-01", "2026-08-03"),
                ("S1", "2026-08-04", "2026-08-04"),
                ("S2", "2026-08-04", "2026-08-04"),
            },
        )
        self.assertTrue(all(job.parent_job_id == namespace for job in jobs))
        self.assertEqual(
            [
                job.id
                for job in build_satellite_batch_jobs(
                    lands,
                    date_from=date(2026, 8, 1),
                    date_to=date(2026, 8, 4),
                    sensors=["S1", "S2"],
                    parent_job_id=namespace,
                    id_namespace=namespace,
                    chunk_days=3,
                )[1]
            ],
            [job.id for job in jobs],
        )


class _BatchDb:
    """让路由测试只验证编排顺序，不连接真实 PostgreSQL。"""

    def __init__(self, lands):
        self.lands = lands
        self.added = []
        self.execute_count = 0
        self.commit_count = 0

    async def get(self, model, object_id):
        if model is Job:
            return next(
                (
                    item
                    for item in self.added
                    if item.id == object_id and item.type == "assessment_batch"
                ),
                None,
            )
        return None

    async def execute(self, statement):
        del statement
        self.execute_count += 1
        if self.execute_count == 1:
            items = self.lands
        else:
            items = [item for item in self.added if item.type != "assessment_batch"]
        result = MagicMock()
        result.scalars.return_value.all.return_value = items
        return result

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commit_count += 1


class AssessmentBatchRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_smart_sync_precedes_shared_jobs_and_per_land_reports(self):
        lands = [local_land("A"), local_land("B", 2000)]
        lands[0].crop_type = "rice"
        lands[1].crop_type = "rice"
        db = _BatchDb(lands)
        body = AssessmentBatchRequest.model_validate(
            {
                "landIdList": ["A", "B"],
                "crop_type": "rice",
                "date_from": "2026-08-01",
                "sensors": ["S2"],
            }
        )
        sync_order = []

        async def sync_selected(ids):
            sync_order.append(list(ids))
            return {"status": "completed", "synced_land_ids": list(ids)}

        with (
            patch("app.routers.assessment.settings.mysql_source_enabled", True),
            patch(
                "app.routers.assessment.sync_selected_lands",
                side_effect=sync_selected,
            ),
            patch(
                "app.mq_publish.publish_api_task",
                side_effect=lambda **kwargs: kwargs["task_id"],
            ) as publish,
        ):
            response = await create_assessment_reports_batch.__wrapped__(
                request=MagicMock(), body=body, ctx=None, db=db
            )

        self.assertEqual(sync_order, [["A", "B"]])
        self.assertEqual(response.status, "running")
        self.assertEqual(response.group_count, 1)
        self.assertEqual(response.report_job_count, 2)
        self.assertEqual(response.satellite_job_count, 1)
        self.assertEqual(db.commit_count, 2)
        self.assertEqual(publish.call_count, 3)
        self.assertEqual(
            {call.kwargs["type"] for call in publish.call_args_list},
            {"satellite_batch", "land_bootstrap"},
        )


class SmartSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_query_uses_bound_parameters(self):
        query, params = _selected_source_query(["A", " A ", "B"])
        self.assertIn("CAST(al.land_id AS CHAR) IN (:selected_land_0, :selected_land_1)", query.text)
        self.assertNotIn("'A'", query.text)
        self.assertEqual(params, {"selected_land_0": "A", "selected_land_1": "B"})

    async def test_disabled_smart_source_does_not_open_database_connections(self):
        with patch("app.services.mysql_land_sync.settings.mysql_source_enabled", False):
            self.assertEqual(
                (await sync_selected_lands(["A"]))["status"],
                "disabled",
            )


class CeleryFallbackTests(unittest.TestCase):
    def test_shared_batch_bootstrap_still_dispatches_report_followup(self):
        from app.mq_publish import _fallback_celery

        results = [SimpleNamespace(id="weather-id"), SimpleNamespace(id="soil-id")]
        with patch("app.celery_client.send_task", side_effect=results + [SimpleNamespace(id="report-id")]) as send:
            _fallback_celery(
                type="land_bootstrap",
                land_id="A",
                extras={
                    "skip_indices": True,
                    "followup_assessment": {"job_id": "report-job"},
                },
            )
        self.assertEqual(send.call_count, 3)
        report_call = send.call_args_list[-1]
        self.assertEqual(
            report_call.kwargs["kwargs"]["wait_celery_ids"],
            ["weather-id", "soil-id"],
        )


if __name__ == "__main__":
    unittest.main()
