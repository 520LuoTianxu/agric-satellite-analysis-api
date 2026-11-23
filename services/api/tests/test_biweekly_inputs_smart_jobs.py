"""Official inputs must use the persisted job window when no Smart catalog is present.

Production (2026-11-23 14:04:29 Asia/Shanghai): after PR75, GET /jobs and
finalize dispatch a biweekly_report, but
GET /v1/internal/biweekly/jobs/{bj-id}/inputs still called
service.catalog.period() via a catalog-less TriggerService and raised
PERIOD_NOT_FOUND as HTTP 500. The download machine never reached PDF render.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agric_satellite_analysis_common.biweekly.calendar import iter_periods
from agric_satellite_analysis_common.biweekly.trigger import TriggerService

from app.core.config import settings
from app.middleware.internal_auth import require_internal_token
from app.routers import internal_biweekly as biweekly_mod
from app.services.biweekly_plans import SmartPlantingCatalog
from app.services.biweekly_report_flow import maybe_finalize_from_satellite_job
from app.services.biweekly_trigger import build_trigger_service

INTERNAL = "local-internal-token-do-not-use-in-prod"
LAND = "32696"
SEASON_START = "2025-09-06"
SEASON_END = "2025-10-03"
S1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa0001"
S2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbb0002"

REPORT_DISPATCH = {
    "dispatched": True,
    "dispatched_to_production": True,
    "reason": None,
    "job_ids": ["report-job-1"],
    "job_count": 1,
    "job_id": "report-job-1",
    "job_type": "biweekly_report",
    "claim_type": "biweekly_report",
    "display_name": "双周报正式出报",
}


def _smart_payload(land_id: str = LAND) -> dict:
    periods = iter_periods(SEASON_START, SEASON_END)
    return {
        "land_id": land_id,
        "plan_available": True,
        "plan_status": "smart_plant_task",
        "plan_source": "smart_agric.agriculture_plant_task",
        "seasons": [
            {
                "season_id": "2025/26",
                "label": "2025/26 种植季",
                "land_id": land_id,
                "crop": "wheat",
                "crop_id": 4,
                "start": SEASON_START,
                "end_inclusive": SEASON_END,
                "end_exclusive": "2025-10-04",
                "cadence_days": 14,
                "periods": periods,
                "native_plan": {
                    "land_id": land_id,
                    "start": SEASON_START,
                    "end_inclusive": SEASON_END,
                    "end_exclusive": "2025-10-04",
                    "intervals": [],
                },
            }
        ],
    }


def _observations():
    return [
        {
            "date": "2025-09-08",
            "sensor": "S2",
            "scene_id": "S2-1",
            "quality_status": "eligible_descriptive",
            "exclusion_reason": None,
            "ndvi_avg": 0.4,
            "evi_avg": 0.3,
            "ndre_avg": 0.2,
            "ndmi_avg": 0.1,
            "metrics": {"ndvi": 0.4, "evi": 0.3, "ndre": 0.2, "ndmi": 0.1},
            "source": "parcel_scene_products",
        }
    ]


class SmartInputsTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            name: getattr(settings, name)
            for name in (
                "biweekly_work_dir",
                "biweekly_pack_root",
                "internal_api_token",
            )
        }
        self.tmp = tempfile.TemporaryDirectory(prefix="smart-inputs-")
        settings.biweekly_work_dir = self.tmp.name
        settings.biweekly_pack_root = str(Path(self.tmp.name) / "no-pack")
        settings.internal_api_token = INTERNAL
        self.catalog = SmartPlantingCatalog(_smart_payload())
        self.service = TriggerService(Path(self.tmp.name), self.catalog, auto_run=False)
        self.bare = build_trigger_service()
        self.statuses: dict[str, str] = {S1: "completed", S2: "completed"}
        self.report = AsyncMock(return_value=dict(REPORT_DISPATCH))
        self._status_patch = patch(
            "app.services.biweekly_report_flow._job_statuses",
            new=AsyncMock(
                side_effect=lambda ids: {
                    i: self.statuses.get(i, "running") for i in ids
                }
            ),
        )
        self._report_patch = patch(
            "app.services.biweekly_report_flow.dispatch_biweekly_report",
            new=self.report,
        )
        self._status_patch.start()
        self._report_patch.start()

    def tearDown(self):
        self._status_patch.stop()
        self._report_patch.stop()
        for name, value in self._saved.items():
            setattr(settings, name, value)
        self.tmp.cleanup()

    def _create_waiting(self, period_id: str = "1") -> dict:
        period = self.catalog.period(LAND, "2025/26", period_id)
        queued = self.service.request_generate(
            LAND, "2025/26", period_id, defer_run=True
        )
        return self.service.record_generate_backfill(
            queued["job_id"],
            {
                "dispatched": True,
                "dispatched_to_production": True,
                "reason": None,
                "job_ids": [S1, S2],
                "job_count": 2,
                "date_from": period["start"],
                "date_to": period["end_inclusive"],
            },
        )

    def _client(self, service) -> TestClient:
        app = FastAPI()
        app.include_router(biweekly_mod.router, prefix="/v1")
        # production hook uses build_trigger_service() without Smart
        app.dependency_overrides[require_internal_token] = lambda: None
        self._inputs_patch = patch(
            "app.routers.internal_biweekly.build_trigger_service",
            return_value=service,
        )
        self._inputs_patch.start()
        client = TestClient(app)
        self.addCleanup(self._inputs_patch.stop)
        return client

    def _raw_client(self, service) -> TestClient:
        app = FastAPI()
        app.include_router(biweekly_mod.router, prefix="/v1")
        with patch(
            "app.routers.internal_biweekly.build_trigger_service", return_value=service
        ):
            # real InternalAuth
            return TestClient(app)

    def test_inputs_without_smart_catalog_uses_persisted_window(self):
        job = self._create_waiting("1")
        self.assertTrue((job.get("window") or {}).get("start"))
        # Production finalize already dispatched; inputs must still work.
        asyncio.run(
            maybe_finalize_from_satellite_job(
                {"biweekly_job_id": job["job_id"], "biweekly_generate_after": True},
                "completed",
            )
        )
        self.assertEqual(self.report.call_count, 1)

        captured = []

        async def enrich(land_id, period, periods, **_kwargs):
            captured.append({"land_id": land_id, "period": period, "periods": periods})
            return {
                "observations_override": _observations(),
                "prior_observations_override": [],
                "observation_source": "parcel_scene_products",
                "land_name": "乐观社区居民委员会7号地块",
                "weather_daily": [],
                "weather_period": {"days": 0, "precipitation_mm": None},
                "weather_antecedent": None,
                "s1_count": 0,
            }

        # Official inputs path: default service, no Smart catalog.
        self.assertFalse(self.bare.catalog._smart_has_plan(LAND))
        with patch("app.routers.internal_biweekly._enrich_payload", new=enrich):
            client = self._client(self.bare)
            resp = client.get(f"/v1/internal/biweekly/jobs/{job['job_id']}/inputs")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertNotEqual(
            body.get("error") or body.get("error_code"), "PERIOD_NOT_FOUND"
        )
        self.assertEqual(body.get("error_code"), None)
        self.assertEqual(body["job_id"], job["job_id"])
        self.assertEqual(body["land_id"], LAND)
        self.assertEqual(body["period_id"], "1")
        self.assertEqual(captured[0]["period"]["start"], "2025-09-06")
        self.assertEqual(captured[0]["period"]["end_inclusive"], "2025-09-19")
        self.assertEqual(captured[0]["period"]["end_exclusive"], "2025-09-20")
        self.assertTrue(body.get("facts"))
        self.assertIn(body.get("status"), {"ready", "evidence_limited"})

    def test_inputs_http_requires_internal_token_and_does_not_500(self):
        job = self._create_waiting("1")
        app = FastAPI()
        app.include_router(biweekly_mod.router, prefix="/v1")
        with patch(
            "app.routers.internal_biweekly.build_trigger_service",
            return_value=self.bare,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            missing = client.get(f"/v1/internal/biweekly/jobs/{job['job_id']}/inputs")
            self.assertEqual(missing.status_code, 401)

            wrong = client.get(
                f"/v1/internal/biweekly/jobs/{job['job_id']}/inputs",
                headers={"Authorization": "Bearer wrong-token"},
            )
            self.assertEqual(wrong.status_code, 401)

            ok_job = client.get(
                f"/v1/internal/biweekly/jobs/{job['job_id']}/inputs",
                headers={"Authorization": f"Bearer {INTERNAL}"},
            )
            # May be 200 with facts or 200 with SCENE_QUERY_FAILED if enrich hits no DB;
            # must not be 500 PERIOD_NOT_FOUND.
            self.assertNotEqual(ok_job.status_code, 500, ok_job.text)
            if ok_job.status_code == 200:
                self.assertNotEqual(ok_job.json().get("error_code"), "PERIOD_NOT_FOUND")

            missing_job = client.get(
                "/v1/internal/biweekly/jobs/bj-does-not-exist0000/inputs",
                headers={"Authorization": f"Bearer {INTERNAL}"},
            )
            self.assertEqual(missing_job.status_code, 404)

    def test_inputs_without_window_or_catalog_is_not_unhandled_500(self):
        queued = self.service.request_generate(LAND, "2025/26", "1", defer_run=True)
        # Strip the persisted window to prove we do not invent dates.
        path = Path(self.tmp.name) / "jobs" / f"{queued['job_id']}.json"
        import json

        data = json.loads(path.read_text())
        data["window"] = {}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        app = FastAPI()
        app.include_router(biweekly_mod.router, prefix="/v1")
        with patch(
            "app.routers.internal_biweekly.build_trigger_service",
            return_value=self.bare,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                f"/v1/internal/biweekly/jobs/{queued['job_id']}/inputs",
                headers={"Authorization": f"Bearer {INTERNAL}"},
            )
        self.assertNotEqual(resp.status_code, 500, resp.text)
        if resp.status_code == 200:
            self.assertEqual(resp.json().get("error_code"), "PERIOD_NOT_FOUND")
        else:
            self.assertIn(resp.status_code, {404, 409, 400})


if __name__ == "__main__":
    unittest.main()
