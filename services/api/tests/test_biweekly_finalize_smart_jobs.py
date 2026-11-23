"""Reproduce Smart-land finalize/GET failures and season one-click handoff.

Production symptoms (land 32696): official seasons/periods work, POST generate
returns dispatched, S1/S2 backfill jobs complete, but biweekly_report is never
queued and GET /jobs/{bj-*} returns 404 PERIOD_NOT_FOUND. The hook and GET
paths build a TriggerService without the Smart catalog; catalog.period() then
fails and the hook swallows the error.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agric_satellite_analysis_common.biweekly.calendar import iter_periods
from agric_satellite_analysis_common.biweekly.trigger import TriggerService

from app.core.config import settings
from app.middleware import biweekly_auth
from app.services.biweekly_plans import SmartPlantingCatalog
from app.services.biweekly_report_flow import (
    maybe_finalize_from_satellite_job,
    start_season_generate,
)
from app.services.biweekly_trigger import build_trigger_service

SECRET = "local-dev-secret-do-not-use-in-prod"
LAND = "32696"
SEASON_START = "2025-09-06"
SEASON_END = "2025-10-03"
S1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa0001"
S2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbb0002"
S1_P2 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa0003"
S2_P2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbb0004"

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
    # Winter-wheat seasons are labeled 2025/26 even for a two-period fixture.
    season_id = "2025/26"
    return {
        "land_id": land_id,
        "plan_available": True,
        "plan_status": "smart_plant_task",
        "plan_source": "smart_agric.agriculture_plant_task",
        "seasons": [
            {
                "season_id": season_id,
                "label": f"{season_id} 种植季",
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


def _backfill_dispatch(*job_ids: str, date_from: str, date_to: str) -> dict:
    return {
        "dispatched": True,
        "dispatched_to_production": True,
        "reason": None,
        "job_ids": list(job_ids),
        "job_count": len(job_ids),
        "date_from": date_from,
        "date_to": date_to,
    }


class SmartFinalizeTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            name: getattr(settings, name)
            for name in (
                "biweekly_api_enabled",
                "biweekly_dev_auth",
                "biweekly_jwt_secret",
                "biweekly_allowed_lands",
                "biweekly_allowed_tenants",
                "biweekly_pack_root",
                "biweekly_work_dir",
                "openfarm_jwt_secret",
            )
        }
        self.tmp = tempfile.TemporaryDirectory(prefix="smart-finalize-")
        settings.biweekly_work_dir = self.tmp.name
        settings.biweekly_pack_root = str(Path(self.tmp.name) / "no-pack")
        settings.biweekly_api_enabled = True
        settings.biweekly_dev_auth = True
        settings.biweekly_jwt_secret = SECRET
        settings.biweekly_allowed_lands = LAND
        settings.biweekly_allowed_tenants = ""
        self.catalog = SmartPlantingCatalog(_smart_payload())
        self.service = TriggerService(Path(self.tmp.name), self.catalog, auto_run=False)
        self.statuses: dict[str, str] = {}
        self.report = AsyncMock(return_value=dict(REPORT_DISPATCH))
        self._status_patch = patch(
            "app.services.biweekly_report_flow._job_statuses",
            new=AsyncMock(side_effect=self._statuses),
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

    async def _statuses(self, job_ids):
        return {job_id: self.statuses.get(job_id, "running") for job_id in job_ids}

    def _create_waiting(self, period_id: str, *job_ids: str) -> dict:
        period = self.catalog.period(LAND, "2025/26", period_id)
        queued = self.service.request_generate(
            LAND, "2025/26", period_id, defer_run=True
        )
        return self.service.record_generate_backfill(
            queued["job_id"],
            _backfill_dispatch(
                *job_ids,
                date_from=period["start"],
                date_to=period["end_inclusive"],
            ),
        )

    def _hook(self, biweekly_job_id: str) -> None:
        params = {
            "biweekly_job_id": biweekly_job_id,
            "biweekly_generate_after": True,
            "biweekly_land_id": LAND,
            "biweekly_period_id": "1",
            "biweekly_season_id": "2025/26",
        }
        # Production hook does not pass a Smart-aware service.
        asyncio.run(maybe_finalize_from_satellite_job(params, "completed"))

    def test_get_job_without_smart_catalog_is_not_period_not_found(self):
        job = self._create_waiting("1", S1, S2)
        from app.biweekly_dev_app import app

        client = TestClient(app)
        token = biweekly_auth.mint_dev_jwt(
            subject="dev-user", tenant_id="dev-tenant", land_ids=[LAND]
        )
        headers = {"Authorization": f"Bearer {token}"}
        missing = client.get(
            f"/v1/lands/{LAND}/biweekly/jobs/bj-does-not-exist", headers=headers
        )
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(missing.json().get("error"), "JOB_NOT_FOUND")

        unauth = client.get(f"/v1/lands/{LAND}/biweekly/jobs/{job['job_id']}")
        self.assertEqual(unauth.status_code, 401)

        # Backfill still running: the job is on disk and GET must not 404.
        pending = client.get(
            f"/v1/lands/{LAND}/biweekly/jobs/{job['job_id']}", headers=headers
        )
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertEqual(pending.json()["job_id"], job["job_id"])
        self.assertEqual(self.report.call_count, 0)

        # Production 404 PERIOD_NOT_FOUND happens after S1/S2 are already terminal,
        # because GET of a dispatched job tries to finalize without a Smart catalog.
        self.statuses[S1] = "completed"
        self.statuses[S2] = "completed"
        fetched = client.get(
            f"/v1/lands/{LAND}/biweekly/jobs/{job['job_id']}", headers=headers
        )
        self.assertEqual(fetched.status_code, 200, fetched.text)
        body = fetched.json()
        self.assertNotEqual(body.get("error"), "PERIOD_NOT_FOUND")
        self.assertEqual(body["job_id"], job["job_id"])
        self.assertEqual(body["land_id"], LAND)
        self.assertEqual(body["period_id"], "1")
        self.assertEqual(body["window"]["start"], "2025-09-06")
        self.assertEqual(self.report.call_count, 1)

        other = client.get(
            f"/v1/lands/0000/biweekly/jobs/{job['job_id']}",
            headers={
                "Authorization": f"Bearer {biweekly_auth.mint_dev_jwt(subject='dev-user', tenant_id='dev-tenant', land_ids=['0000'])}"
            },
        )
        # land 0000 is not in the dev allowlist
        self.assertEqual(other.status_code, 403)

    def test_hook_without_smart_catalog_dispatches_once_after_both_sensors_complete(
        self,
    ):
        job = self._create_waiting("1", S1, S2)
        bare = build_trigger_service()
        self.assertFalse(bare.catalog._smart_has_plan(LAND))

        self.statuses[S2] = "completed"
        self.statuses[S1] = "running"
        self._hook(job["job_id"])
        self.assertEqual(self.report.call_count, 0)
        stored = self.service.get_job(job["job_id"])
        self.assertEqual(stored["status"], "dispatched")
        self.assertIsNone(
            ((stored.get("result") or {}).get("report") or {}).get("job_ids")
        )

        self.statuses[S1] = "completed"
        self._hook(job["job_id"])
        self.assertEqual(self.report.call_count, 1)
        window = self.report.call_args.args[1]
        self.assertEqual(window["date_from"], "2025-09-06")
        self.assertEqual(window["date_to_inclusive"], "2025-09-19")
        stored = self.service.get_job(job["job_id"])
        self.assertEqual((stored.get("result") or {}).get("stage"), "report")
        self.assertEqual(
            ((stored.get("result") or {}).get("report") or {}).get("job_type"),
            "biweekly_report",
        )

        # Retry or the other sensor completing again must not dispatch twice.
        self._hook(job["job_id"])
        self.assertEqual(self.report.call_count, 1)

    def test_concurrent_sensor_completions_dispatch_report_once(self):
        job = self._create_waiting("1", S1, S2)
        self.statuses[S1] = "completed"
        self.statuses[S2] = "completed"
        params = {
            "biweekly_job_id": job["job_id"],
            "biweekly_generate_after": True,
        }

        async def _both():
            await asyncio.gather(
                maybe_finalize_from_satellite_job(params, "completed"),
                maybe_finalize_from_satellite_job(params, "completed"),
            )

        asyncio.run(_both())
        self.assertEqual(self.report.call_count, 1)

    def test_season_one_click_finalizes_each_period_after_backfill(self):
        """POST /backfill-season (start_season_generate) then hook after each period."""

        async def fake_backfill(land_id, window, **kwargs):
            period_id = str(window["current_period_id"])
            job_ids = [S1, S2] if period_id == "1" else [S1_P2, S2_P2]
            return _backfill_dispatch(
                *job_ids,
                date_from=window["date_from"],
                date_to=window["date_to_inclusive"],
            )

        with patch(
            "app.services.biweekly_report_flow.dispatch_bounded_backfill",
            new=fake_backfill,
        ):
            season_job = asyncio.run(
                start_season_generate(self.service, land_id=LAND, season_id="2025/26")
            )
        self.assertEqual(season_job["kind"], "backfill_season")
        self.assertEqual(season_job["status"], "dispatched")
        children = (season_job.get("result") or {}).get("period_results") or []
        self.assertEqual(len(children), 2)
        self.assertTrue(all(item.get("status") == "dispatched" for item in children))
        self.assertEqual(self.report.call_count, 0)

        # First period: only S2 done — still waiting, no official report yet.
        self.statuses.update(
            {S2: "completed", S1: "running", S1_P2: "running", S2_P2: "running"}
        )
        self._hook(children[0]["generate_job_id"])
        self.assertEqual(self.report.call_count, 0)

        # First period both sensors complete — exactly one report dispatch.
        self.statuses[S1] = "completed"
        self._hook(children[0]["generate_job_id"])
        self.assertEqual(self.report.call_count, 1)
        first_window = self.report.call_args.args[1]
        self.assertEqual(first_window["date_from"], "2025-09-06")
        self.assertEqual(first_window["date_to_inclusive"], "2025-09-19")

        # Second period completes independently — second dispatch, still once each.
        self.statuses[S1_P2] = "completed"
        self.statuses[S2_P2] = "completed"
        self._hook(children[1]["generate_job_id"])
        self.assertEqual(self.report.call_count, 2)
        second_window = self.report.call_args.args[1]
        self.assertEqual(second_window["date_from"], "2025-09-20")
        self.assertEqual(second_window["date_to_inclusive"], "2025-10-03")

        # Replaying either hook is a no-op for dispatch.
        self._hook(children[0]["generate_job_id"])
        self._hook(children[1]["generate_job_id"])
        self.assertEqual(self.report.call_count, 2)
        for item in children:
            stored = self.service.get_job(item["generate_job_id"])
            self.assertEqual(
                ((stored.get("result") or {}).get("report") or {}).get("job_type"),
                "biweekly_report",
            )


if __name__ == "__main__":
    unittest.main()
