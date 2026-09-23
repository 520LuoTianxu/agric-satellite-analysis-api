"""验证项目风险、观测有效性和完整批量摘要，防止缺数据被显示为正常。"""

import unittest
import uuid
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.models.tables import Alert
from app.routers.project_monitoring import router
from app.schemas.project_monitoring import ProjectObservation
from app.services.project_monitoring import get_project_monitoring, summarize_land

TODAY = date(2026, 9, 17)


def alert(*, severity="high", status="open", day=TODAY, index="ndvi"):
    return Alert(
        id=uuid.uuid4(),
        land_id="A",
        date=day,
        severity=severity,
        status=status,
        rule_name=f"{index}_drop",
        message="指数下降，需现场核查",
        index_type=index,
    )


def summarize(*, observations=None, alerts=None, **land):
    return summarize_land(
        {"land_id": "A", **land},
        observations or [],
        alerts or [],
        as_of=TODAY,
        freshness_days=14,
    )


class MonitoringStateTests(unittest.TestCase):
    def test_missing_and_bad_quality_never_become_normal(self):
        missing = summarize()
        cloudy = summarize(latest_scene_date=TODAY)
        self.assertEqual(
            (missing.data_status, missing.risk_level), ("missing", "unknown")
        )
        self.assertEqual(
            (cloudy.data_status, cloudy.risk_level), ("low_quality", "unknown")
        )
        self.assertIsNone(cloudy.observation)

    def test_freshness_boundary_and_zero_index(self):
        current = summarize(
            observations=[ProjectObservation(date=TODAY - timedelta(days=14), ndvi=0)]
        )
        stale = summarize(
            observations=[ProjectObservation(date=TODAY - timedelta(days=15), ndvi=0.5)]
        )
        self.assertEqual((current.data_status, current.risk_level), ("fresh", "normal"))
        self.assertEqual(current.observation.ndvi, 0)
        self.assertEqual((stale.data_status, stale.risk_level), ("stale", "unknown"))

    def test_open_warning_survives_missing_or_stale_observations(self):
        result = summarize(alerts=[alert(day=TODAY - timedelta(days=50))])
        self.assertEqual(result.risk_level, "high")
        self.assertEqual(result.data_status, "missing")
        self.assertEqual(result.open_alert_count, 1)

    def test_closing_latest_alert_does_not_indicate_recovery(self):
        result = summarize(
            observations=[ProjectObservation(date=TODAY, ndvi=0.2)],
            alerts=[alert(status="closed")],
        )
        self.assertEqual(result.risk_level, "high")
        self.assertEqual(result.open_alert_count, 0)
        self.assertEqual(result.risk_alert_count, 1)

    def test_newer_valid_observation_supersedes_closed_optical_alert(self):
        result = summarize(
            observations=[ProjectObservation(date=TODAY, ndvi=0.7)],
            alerts=[alert(status="closed", day=TODAY - timedelta(days=1))],
        )
        self.assertEqual(result.risk_level, "normal")
        self.assertEqual(result.risk_alert_count, 0)

    def test_unrelated_newer_index_does_not_clear_closed_water_alert(self):
        result = summarize(
            observations=[ProjectObservation(date=TODAY, ndvi=0.7)],
            alerts=[
                alert(status="closed", index="ndmi", day=TODAY - timedelta(days=1))
            ],
        )
        self.assertEqual(result.risk_level, "high")
        self.assertEqual(result.open_alert_count, 0)

    def test_closed_recent_alert_without_observations_keeps_its_evidence(self):
        result = summarize(alerts=[alert(status="closed")])
        self.assertEqual(result.risk_level, "high")
        self.assertEqual(result.data_status, "missing")

    def test_highest_severity_and_counts_are_independent_of_preview_limit(self):
        alerts = [alert(severity="medium") for _ in range(210)] + [alert()]
        result = summarize(alerts=alerts)
        self.assertEqual(result.risk_level, "high")
        self.assertEqual(result.open_alert_count, 211)
        self.assertEqual(result.open_high_count, 1)
        self.assertEqual(len(result.alerts), 5)
        self.assertEqual(result.alerts[0].severity, "high")

    def test_area_uses_mu_and_only_falls_back_to_hectares_when_missing(self):
        self.assertEqual(summarize(land_area_mu=100, area_ha=20).area_mu, 100)
        self.assertEqual(summarize(area_ha=2).area_mu, 30)
        self.assertIsNone(summarize(land_area_mu=float("nan")).area_mu)
        self.assertIsNone(summarize(land_area_mu=-2).area_mu)


class BatchQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_more_than_500_parcels_and_same_project_bound_queries(self):
        rows = [{"land_id": str(i), "latest_scene_date": None} for i in range(601)]
        lands_result = MagicMock()
        lands_result.mappings.return_value.all.return_value = rows
        alerts_result = MagicMock()
        alerts_result.scalars.return_value.all.return_value = []
        db = MagicMock()
        db.execute = AsyncMock(side_effect=[lands_result, alerts_result])
        result = await get_project_monitoring(db, "project-42")
        self.assertEqual(len(result.items), 601)
        self.assertEqual(db.execute.await_count, 2)
        scene_call, alert_call = db.execute.call_args_list
        self.assertEqual(scene_call.args[1]["group_id"], "project-42")
        scene_sql = str(scene_call.args[0])
        self.assertIn("p.group_id = :group_id", scene_sql)
        self.assertIn("p.deleted_at IS NULL", scene_sql)
        self.assertIn("DISTINCT ON (s.date)", scene_sql)
        self.assertIn("s.product_source", scene_sql)
        self.assertNotIn("pixel_data->>", scene_sql)
        compiled = alert_call.args[0].compile()
        self.assertIn("project-42", compiled.params.values())
        self.assertIn("land_parcels.deleted_at IS NULL", str(compiled))
        self.assertTrue(all(item.risk_level == "unknown" for item in result.items))

    async def test_two_observations_do_not_duplicate_parcels(self):
        row = {"land_id": "A", "latest_scene_date": TODAY, "cloud_cover": 5}
        result = MagicMock()
        result.mappings.return_value.all.return_value = [
            {**row, "observation_date": TODAY, "ndvi_avg": 0.4},
            {**row, "observation_date": TODAY - timedelta(days=5), "ndvi_avg": 0.6},
        ]
        empty_alerts = MagicMock()
        empty_alerts.scalars.return_value.all.return_value = []
        db = MagicMock()
        db.execute = AsyncMock(side_effect=[result, empty_alerts])
        output = await get_project_monitoring(db, "project-42")
        self.assertEqual(len(output.items), 1)
        self.assertEqual(output.items[0].observation.ndvi, 0.4)
        self.assertEqual(output.items[0].previous_observation.ndvi, 0.6)

    async def test_empty_project_does_not_query_unrelated_alerts(self):
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        output = await get_project_monitoring(db, "empty")
        self.assertEqual(output.items, [])
        self.assertEqual(db.execute.await_count, 1)


class RouteTests(unittest.TestCase):
    def test_freshness_validation_and_empty_response(self):
        app = FastAPI()
        app.include_router(router, prefix="/v1")
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            response = client.get("/v1/projects/project-42/monitoring?freshness_days=7")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["freshness_days"], 7)
            self.assertEqual(response.json()["group_id"], "project-42")
            self.assertEqual(response.json()["items"], [])
            for days in (0, 91):
                self.assertEqual(
                    client.get(
                        f"/v1/projects/p/monitoring?freshness_days={days}"
                    ).status_code,
                    422,
                )


if __name__ == "__main__":
    unittest.main()
