"""验证每日增量范围、批次恢复、MQ入库屏障与不可变历史的各级汇总。"""

import json
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.models.tables import Job
from app.schemas.agri import OverviewStatsOut
from app.services import overview_daily as daily
from test_satellite_batch import local_land

DAY = date(2026, 9, 16)


def template():
    return OverviewStatsOut.model_validate(
        {
            "region": {"level": "country", "code": None, "name": "全国", "path": []},
            "filters": {
                "from": "2026-07-18",
                "to": DAY.isoformat(),
                "crop": None,
                "drought_source": "scene_avg",
            },
            "totals": {"parcel_count": 0, "area_mu": 0},
            "drought": {},
            "flood": {},
            "weak_growth": {},
            "children": [],
        }
    )


def fact(
    land,
    *,
    province="11",
    city="1101",
    county="110101",
    drought="normal",
    flood="dry",
    s1="2026-09-16",
    s2="2026-09-15",
    weak=False,
):
    return {
        "land_id": land,
        "area_mu": 10.5,
        "province_code": province,
        "province_name": f"省{province}",
        "city_code": city,
        "city_name": f"市{city}",
        "county_code": county,
        "county_name": f"县{county}",
        "drought": drought,
        "flood": flood,
        "s1_date": s1,
        "s2_date": s2,
        "weak": weak,
    }


def result(**kwargs):
    value = MagicMock()
    for name, answer in kwargs.items():
        if name == "lands":
            value.scalars.return_value.all.return_value = answer
        else:
            getattr(value, name).return_value = answer
    return value


def run_with(jobs, *, age=0):
    return Job(
        id=daily.run_id_for(DAY),
        type=daily.RUN_TYPE,
        status="running",
        started_at=datetime.now(timezone.utc) - timedelta(hours=age),
        params_json={
            "as_of_date": DAY.isoformat(),
            "job_ids": [str(job.id) for job in jobs],
        },
        progress_json={"phase": "downloading", "invalid_land_ids": []},
    )


def satellite_job(status="completed", products=None):
    products = [] if products is None else products
    return Job(
        id=uuid.uuid4(),
        land_id="A",
        type="satellite_batch",
        status=status,
        params_json={
            "sensor": "S2",
            "land_ids": ["A"],
            "date_from": "2026-09-09",
            "date_to": DAY.isoformat(),
        },
        progress_json={
            "products_published": len(products),
            "published_products": products,
        },
    )


class AggregateTests(unittest.TestCase):
    def test_each_level_has_identical_counts_and_area(self):
        facts = {
            "A": fact("A", drought="severe", flood="flood_severe", weak=True),
            "B": fact("B", drought="unknown", flood="unknown", s1=None, s2=None),
            "C": fact(
                "C", province="12", city="1201", county="120101", s2=DAY.isoformat()
            ),
        }
        outputs = daily.aggregate_snapshots(facts, template(), DAY)
        country = next(out for out in outputs if out.region["level"] == "country")
        self.assertEqual(country.totals.parcel_count, 3)
        self.assertEqual(country.totals.area_mu, 31.5)
        self.assertEqual(country.drought.severe, 1)
        self.assertEqual(country.drought.unknown, 1)
        self.assertEqual(country.flood.flood, 1)
        self.assertEqual(country.weak_growth.parcel_count, 1)
        self.assertEqual(sum(c.parcel_count for c in country.children), 3)
        self.assertEqual(sum(c.drought_alert for c in country.children), 1)
        self.assertEqual(sum(c.flood for c in country.children), 1)
        province_11 = next(c for c in country.children if c.code == "110000")
        self.assertEqual(province_11.drought_ratio, 0.5)
        self.assertEqual(province_11.flood_ratio, 0.5)
        self.assertEqual(province_11.weak_growth_ratio, 0.5)
        self.assertEqual(
            country.filters["freshness"]["s2"],
            {
                "today": 1,
                "carried": 1,
                "unknown": 1,
                "oldest": "2026-09-15",
                "latest": "2026-09-16",
            },
        )
        for out in outputs:
            self.assertEqual(
                sum(out.drought.model_dump(exclude={"area_mu"}).values()),
                out.totals.parcel_count,
            )
            self.assertEqual(
                out.flood.flood_severe
                + out.flood.flood_moderate
                + out.flood.flood_mild
                + out.flood.dry
                + out.flood.unknown,
                out.totals.parcel_count,
            )
            self.assertTrue(out.filters["snapshot"])
        # 区划和面积使用保存时事实，调用方后续修改不会改变已有输出。
        facts["A"]["area_mu"] = 99
        self.assertEqual(country.totals.area_mu, 31.5)

    def test_missing_codes_do_not_merge_same_name_in_different_provinces(self):
        facts = {
            "A": fact("A", city=None, county=None),
            "B": fact("B", province="12", city=None, county=None),
        }
        outputs = daily.aggregate_snapshots(facts, template(), DAY)
        self.assertEqual(
            len([out for out in outputs if out.region["level"] == "city"]), 2
        )
        self.assertEqual(
            len([out for out in outputs if out.region["level"] == "county"]), 2
        )

    def test_empty_country_snapshot_is_still_saved(self):
        outputs = daily.aggregate_snapshots({}, template(), DAY)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0].totals.parcel_count, 0)

    def test_incremental_dates_include_late_scenes_and_outages(self):
        self.assertEqual(daily.download_start(None, DAY), DAY - timedelta(days=6))
        self.assertEqual(daily.download_start(DAY, DAY), DAY - timedelta(days=6))
        self.assertEqual(
            daily.download_start(date(2026, 7, 1), DAY), DAY - timedelta(days=6)
        )
        self.assertEqual(daily.run_id_for(DAY), daily.run_id_for(DAY))


class PrepareTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_active_lands_are_grouped_and_invalid_land_is_isolated(self):
        lands = [
            local_land("A"),
            local_land("B", 2000),
            local_land("C", 8000),
            local_land("bad"),
        ]
        lands[-1].boundary_geojson = None
        db = MagicMock()
        objects = {}
        db.get = AsyncMock(side_effect=lambda model, key: objects.get(key))
        db.add.side_effect = lambda obj: objects.setdefault(obj.id, obj)
        db.commit = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[result(), result(lands=lands), result(all=[]), result()]
        )
        with patch.object(daily, "publish_api_task", return_value="queued") as publish:
            out = await daily.prepare_daily(db, DAY)
            again = await daily.prepare_daily(db, DAY)
        self.assertEqual(
            (out["lands_checked"], out["group_count"], out["job_count"]), (4, 2, 4)
        )
        self.assertEqual(out["invalid_land_ids"], ["bad"])
        self.assertEqual(again["run_id"], out["run_id"])
        self.assertEqual(publish.call_count, 4)
        jobs = [obj for obj in objects.values() if obj.type == "satellite_batch"]
        self.assertEqual(
            [job.params_json["land_ids"] for job in jobs],
            [["A", "B"], ["A", "B"], ["C"], ["C"]],
        )
        self.assertEqual(
            {job.params_json["sensor"] for job in jobs},
            {"S1", "S2"},
        )
        self.assertTrue(
            all(
                job.params_json["date_from"] == (DAY - timedelta(days=6)).isoformat()
                and job.params_json["date_to"] == DAY.isoformat()
                for job in jobs
            )
        )

    async def test_partial_dispatch_retries_only_unconfirmed_jobs(self):
        a, b = satellite_job("pending"), satellite_job("pending")
        run = run_with([a, b])
        run.progress_json["dispatched_job_ids"] = [str(a.id)]
        db = MagicMock()
        objects = {obj.id: obj for obj in (run, a, b)}
        db.get = AsyncMock(side_effect=lambda model, key: objects.get(key))
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        with patch.object(daily, "publish_api_task", return_value="queued") as publish:
            out = await daily.prepare_daily(db, DAY)
        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["task_id"], str(b.id))
        self.assertEqual(len(out["dispatched_job_ids"]), 2)
        db.add.assert_not_called()


class FinalizeTests(unittest.IsolatedAsyncioTestCase):
    async def finalize(self, job, *, missing=0, age=0):
        run = run_with([job], age=age)
        db = MagicMock()
        db.commit = AsyncMock()
        db.flush = AsyncMock()
        calls = [result(scalar_one_or_none=run), result(lands=[job])]
        if job.progress_json.get("published_products"):
            calls.append(result(scalar_one=missing))
        db.execute = AsyncMock(side_effect=calls + [result()] * 20)
        with (
            patch(
                "app.routers.agri_overview._compute_live_stats",
                new=AsyncMock(return_value=template()),
            ) as compute,
            patch(
                "app.routers.agri_overview.ensure_overview_cache_table", new=AsyncMock()
            ),
        ):
            out = await daily.finalize_daily(db, run.id)
        return out, db, compute

    async def test_completed_download_waits_until_result_is_ingested(self):
        job = satellite_job(products=[{"land_id": "A", "date": DAY.isoformat()}])
        out, db, compute = await self.finalize(job, missing=1)
        self.assertEqual(out["phase"], "waiting_results")
        self.assertEqual(out["results_pending"], 1)
        compute.assert_not_awaited()
        expected = json.loads(db.execute.call_args_list[2].args[1]["expected"])
        self.assertEqual(expected[0]["sensor"], "S2")

    async def test_pending_download_does_not_publish_snapshot(self):
        out, _, compute = await self.finalize(satellite_job("running"))
        self.assertEqual(out["pending_jobs"], 1)
        compute.assert_not_awaited()

    async def test_dispatch_failed_job_is_requeued_before_finalization(self):
        job = satellite_job("failed")
        job.error = "下载机任务连续3次未完成，已标记失败"
        job.progress_json["work_item_failed"] = True
        with patch.object(daily, "publish_api_task", return_value=str(job.id)) as publish:
            out, _, compute = await self.finalize(job)
        publish.assert_called_once_with(
            type="satellite_batch",
            land_id="A",
            task_id=str(job.id),
            extras={"job_id": str(job.id)},
            priority=daily.BACKGROUND_TASK_PRIORITY,
        )
        self.assertEqual(job.status, "pending")
        self.assertIsNone(job.error)
        self.assertNotIn("work_item_failed", job.progress_json)
        self.assertIsNotNone(job.started_at)
        self.assertEqual(out["pending_jobs"], 1)
        self.assertEqual(out["redispatched_job_ids"], [str(job.id)])
        compute.assert_not_awaited()

    async def test_legacy_stale_failed_job_can_be_requeued(self):
        job = satellite_job("failed")
        job.progress_json["stale_recovered"] = True
        with patch.object(daily, "publish_api_task", return_value=str(job.id)) as publish:
            out, _, compute = await self.finalize(job)
        publish.assert_called_once()
        self.assertEqual(job.status, "pending")
        self.assertNotIn("stale_recovered", job.progress_json)
        self.assertEqual(out["redispatched_job_ids"], [str(job.id)])
        compute.assert_not_awaited()

    async def test_old_running_download_still_waits_for_terminal_state(self):
        job = satellite_job("running")
        job.started_at = datetime.now(timezone.utc) - (
            timedelta(hours=48)
        )
        out, _, compute = await self.finalize(job)
        self.assertEqual(job.status, "running")
        self.assertEqual(out["status"], "running")
        self.assertEqual(out["pending_jobs"], 1)
        compute.assert_not_awaited()

    async def test_expired_pending_download_still_waits_for_terminal_state(self):
        out, _, compute = await self.finalize(satellite_job("pending"), age=24)
        self.assertEqual(out["pending_jobs"], 1)
        compute.assert_not_awaited()

    async def test_ingested_results_produce_complete_snapshot(self):
        out, db, compute = await self.finalize(
            satellite_job(products=[{"land_id": "A", "date": DAY.isoformat()}])
        )
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["phase"], "finished")
        self.assertEqual(compute.call_args.kwargs["to_d"], DAY)
        writes = [
            call
            for call in db.execute.call_args_list
            if "INSERT INTO" in str(call.args[0])
        ]
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(writes[0].args[1]), 1)
        self.assertTrue(
            json.loads(writes[0].args[1][0]["metric"])["filters"]["snapshot"]
        )
        self.assertIn("<> 'true'", str(writes[0].args[0]))

    async def test_no_new_scene_still_produces_snapshot(self):
        out, _, compute = await self.finalize(satellite_job())
        self.assertEqual(out["status"], "completed")
        compute.assert_awaited_once()

    async def test_failure_and_ingestion_timeout_are_visible_as_partial(self):
        out, _, _ = await self.finalize(satellite_job("failed"))
        self.assertEqual(out["status"], "partial")
        out, _, compute = await self.finalize(
            satellite_job(products=[{"land_id": "A", "date": DAY.isoformat()}]),
            missing=1,
            age=24,
        )
        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["results_pending"], 1)
        compute.assert_awaited_once()

    async def test_terminal_failed_job_does_not_block_final_aggregation(self):
        completed = satellite_job(
            products=[{"land_id": "A", "date": DAY.isoformat()}]
        )
        failed = satellite_job("failed")
        legacy_succeeded = satellite_job("succeeded")
        run = run_with([completed, failed, legacy_succeeded])
        db = MagicMock()
        db.commit = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                result(scalar_one_or_none=run),
                result(lands=[completed, failed, legacy_succeeded]),
                result(scalar_one=0),
                *([result()] * 20),
            ]
        )
        with (
            patch(
                "app.routers.agri_overview._compute_live_stats",
                new=AsyncMock(return_value=template()),
            ) as compute,
            patch(
                "app.routers.agri_overview.ensure_overview_cache_table", new=AsyncMock()
            ),
        ):
            out = await daily.finalize_daily(db, run.id)

        self.assertEqual(out["status"], "partial")
        self.assertEqual(out["pending_jobs"], 0)
        self.assertEqual(out["failed_jobs"], 1)
        self.assertEqual(out["failed_land_ids"], ["A"])
        self.assertEqual(out["failed_land_count"], 1)
        compute.assert_awaited_once()

    async def test_old_worker_without_product_details_cannot_skip_ingestion(self):
        job = satellite_job()
        job.progress_json["products_published"] = 2
        out, _, compute = await self.finalize(job)
        self.assertEqual(out["results_pending"], 2)
        compute.assert_not_awaited()

    async def test_finished_run_is_not_recomputed(self):
        run = run_with([])
        run.status = "completed"
        db = MagicMock(execute=AsyncMock(return_value=result(scalar_one_or_none=run)))
        with patch(
            "app.routers.agri_overview._compute_live_stats", new=AsyncMock()
        ) as compute:
            out = await daily.finalize_daily(db, run.id)
        self.assertEqual(out["status"], "completed")
        compute.assert_not_awaited()


class ReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_historical_snapshot_returns_no_data_without_live_fallback(
        self,
    ):
        db = MagicMock(execute=AsyncMock(return_value=result(first=None)))
        with (
            patch(
                "app.routers.agri_overview.ensure_overview_cache_table", new=AsyncMock()
            ),
            patch(
                "app.routers.agri_overview._compute_live_stats", new=AsyncMock()
            ) as compute,
        ):
            out = await daily.read_daily_snapshot(
                db, level="province", code="11", name=None, as_of=DAY
            )
        self.assertIsNone(out)
        self.assertTrue(db.execute.call_args.args[1]["exact"])
        self.assertEqual(db.execute.call_args.args[1]["code"], "110000")
        compute.assert_not_awaited()

    async def test_today_reads_latest_snapshot_but_retains_original_date(self):
        payload = template().model_dump(mode="json")
        payload["filters"].update(snapshot=True, as_of_date="2026-09-15")
        row = SimpleNamespace(
            metric_json=payload, updated_at=datetime(2026, 9, 15, tzinfo=timezone.utc)
        )
        db = MagicMock(execute=AsyncMock(return_value=result(first=row)))
        with patch(
            "app.routers.agri_overview.ensure_overview_cache_table", new=AsyncMock()
        ):
            out = await daily.read_daily_snapshot(
                db, level="country", code=None, name=None, as_of=None
            )
        self.assertFalse(db.execute.call_args.args[1]["exact"])
        self.assertEqual(out.filters["as_of_date"], "2026-09-15")

    async def test_region_is_required_outside_country(self):
        with self.assertRaises(HTTPException):
            await daily.read_daily_snapshot(
                MagicMock(), level="county", code=None, name=None, as_of=DAY
            )


class LiveFactsTests(unittest.IsolatedAsyncioTestCase):
    async def test_classification_facts_match_country_counts_and_missing_observations(
        self,
    ):
        from app.routers.agri_overview import _compute_live_stats

        parcels = []
        for land in ("A", "B", "C"):
            values = {
                key: value
                for key, value in fact(land).items()
                if key
                in {
                    "land_id",
                    "area_mu",
                    "province_code",
                    "province_name",
                    "city_code",
                    "city_name",
                    "county_code",
                    "county_name",
                }
            }
            parcels.append(SimpleNamespace(**values, _mapping=values))
        dry = SimpleNamespace(land_id="A", date=DAY, ndvi_avg=0.5, ndmi_avg=-0.3)
        wet = SimpleNamespace(
            land_id="B", date=DAY - timedelta(days=1), ndvi_avg=0.6, ndmi_avg=0.3
        )
        flood = SimpleNamespace(
            land_id="A", date=DAY, vv_avg=-22.0, vh_avg=-27.0, relative_orbit=42
        )
        baseline = [
            SimpleNamespace(
                land_id="A",
                date=DAY - timedelta(days=days),
                vv_avg=-12.0,
                vh_avg=-18.0,
                relative_orbit=42,
            )
            for days in (30, 18, 6)
        ]
        db = MagicMock(
            execute=AsyncMock(
                side_effect=[
                    result(fetchall=parcels),
                    result(fetchall=[dry, wet]),
                    result(fetchall=[*baseline, flood]),
                    result(fetchall=[]),
                ]
            )
        )
        facts = {}
        out = await _compute_live_stats(
            db,
            level="country",
            code=None,
            name=None,
            from_d=DAY - timedelta(days=60),
            to_d=DAY,
            crop=None,
            allow_pixels=False,
            parcel_facts=facts,
        )
        self.assertEqual(
            (out.drought.severe, out.drought.normal, out.drought.unknown), (1, 1, 1)
        )
        self.assertEqual(facts["A"]["drought"], "severe")
        self.assertEqual(facts["A"]["flood"], "flood_severe")
        self.assertEqual(facts["C"]["drought"], "unknown")
        self.assertIsNone(facts["C"]["s2_date"])
        snapshots = daily.aggregate_snapshots(facts, out, DAY)
        country = next(
            value for value in snapshots if value.region["level"] == "country"
        )
        self.assertEqual(country.drought, out.drought)
        self.assertEqual(country.flood, out.flood)
        child = next(value for value in country.children if value.code == "110000")
        self.assertAlmostEqual(child.drought_ratio, 1 / 3, places=6)
        self.assertAlmostEqual(child.flood_ratio, 1 / 3, places=6)
        self.assertEqual(child.weak_growth_ratio, 0)
        self.assertIn("p.deleted_at IS NULL", str(db.execute.call_args_list[0].args[0]))


class PublicRouteTests(unittest.TestCase):
    def test_daily_and_history_routes_return_saved_data_and_disabled_schedule(self):
        from fastapi.testclient import TestClient
        from app.core.database import get_db
        from app.core.rate_limit import limiter
        from app.main import app

        async def override_db():
            yield MagicMock(get=AsyncMock(return_value=None))

        app.dependency_overrides[get_db] = override_db
        payload = template()
        payload.filters.update(snapshot=True, as_of_date="2026-09-15")
        try:
            with (
                patch.object(limiter, "enabled", False),
                patch(
                    "app.routers.agri_overview.read_daily_snapshot",
                    new=AsyncMock(return_value=payload),
                ) as saved,
                patch(
                    "app.routers.agri_overview.read_daily_history",
                    new=AsyncMock(return_value=[{"as_of_date": "2026-09-15"}]),
                ) as history,
                patch(
                    "agric_satellite_analysis_common.settings.settings.schedule_daily_satellite_enabled",
                    False,
                ),
                TestClient(app) as client,
            ):
                response = client.get("/v1/agri/overview/daily")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["schedule"]["enabled"])
                self.assertEqual(
                    response.json()["stats"]["filters"]["as_of_date"], "2026-09-15"
                )
                saved.return_value = None
                response = client.get("/v1/agri/overview/daily?as_of=2026-09-14")
                self.assertIsNone(response.json()["stats"])
                response = client.get(
                    "/v1/agri/overview/history?from=2026-09-01&to=2026-09-16"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["items"]), 1)
                history.assert_awaited_once()
                response = client.get(
                    "/v1/agri/overview/history?from=2026-09-16&to=2026-09-01"
                )
                self.assertEqual(response.status_code, 400)
        finally:
            app.dependency_overrides.pop(get_db, None)


class OverviewFinalizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_regions_are_upserted_in_batches(self):
        from app.routers.internal_schedule import (
            OVERVIEW_UPSERT_BATCH_SIZE,
            OverviewRefreshFinalizeIn,
            finalize_overview,
        )

        metric = template().model_dump(mode="json", by_alias=True)
        payload = {
            "window_from": "2026-07-20",
            "window_to": "2026-09-18",
            "crop": None,
            "results": [metric] * (OVERVIEW_UPSERT_BATCH_SIZE + 1),
        }
        storage = MagicMock()
        storage.get_bytes.return_value = json.dumps(payload).encode()
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()

        with (
            patch("app.routers.internal_schedule.get_storage", return_value=storage),
            patch(
                "app.routers.agri_overview.ensure_overview_cache_table",
                new=AsyncMock(),
            ),
        ):
            out = await finalize_overview(
                OverviewRefreshFinalizeIn(result_oss_key="overview/preagg/output/result.json"),
                MagicMock(),
                db,
            )

        self.assertEqual(out["regions"], OVERVIEW_UPSERT_BATCH_SIZE + 1)
        self.assertEqual(db.execute.await_count, 2)
        self.assertEqual(len(db.execute.call_args_list[0].args[1]), OVERVIEW_UPSERT_BATCH_SIZE)
        self.assertEqual(len(db.execute.call_args_list[1].args[1]), 1)
        db.commit.assert_awaited_once()


class OverviewCacheInitTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_ddl_runs_once_per_api_process(self):
        from app.routers import agri_overview

        connection = MagicMock()
        connection.execute = AsyncMock()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=connection)
        context.__aexit__ = AsyncMock(return_value=False)
        engine = MagicMock()
        engine.begin.return_value = context

        with (
            patch.object(agri_overview, "_overview_cache_ready", False),
            patch("app.core.database.engine", engine),
        ):
            await agri_overview.ensure_overview_cache_table(MagicMock())
            await agri_overview.ensure_overview_cache_table(MagicMock())

        engine.begin.assert_called_once()
        self.assertEqual(connection.execute.await_count, 2)
