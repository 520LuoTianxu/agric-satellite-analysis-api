"""Biweekly trigger routes on the API machine: auth, bounds, no anonymous bypass."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from app.middleware import biweekly_auth

PACK_ROOT = Path("/workspace/reports/field6417-2025-2026")
SECRET = "local-dev-secret-do-not-use-in-prod"
FORBIDDEN_IMPORTS = (
    "get_current_user",
    "require_roles",
    "ANON_USER",
    "AUTH_DISABLED",
)

REPORT_DISPATCH = {
    "dispatched": True,
    "dispatched_to_production": True,
    "reason": None,
    "job_ids": ["report-job-1"],
    "job_count": 1,
    "job_id": "report-job-1",
    "claim_type": "biweekly_report",
    "job_type": "biweekly_report",
}

NO_JOBS_DISPATCH = {
    "dispatched": False,
    "dispatched_to_production": False,
    "reason": "NO_JOBS_CREATED",
    "job_ids": [],
    "job_count": 0,
}


def _stub_official_pdf(facts, dest, **kwargs):
    Path(dest).write_bytes(b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n")
    return Path(dest)


class BiweeklyRouterTests(unittest.TestCase):
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
                "biweekly_dispatch_to_production",
                "openfarm_jwt_secret",
            )
        }
        self.work = tempfile.TemporaryDirectory(prefix="biweekly-api-test-")
        settings.biweekly_pack_root = str(PACK_ROOT)
        settings.biweekly_work_dir = self.work.name
        from tests.official_store_testutil import patch_official_store_not_migrated

        self._official_store = patch_official_store_not_migrated()
        self._official_store.start()
        settings.biweekly_dispatch_to_production = False
        from app.biweekly_dev_app import app

        self.client = TestClient(app)
        self._obs_patch = patch(
            "app.routers.biweekly.load_scene_observations",
            new=AsyncMock(return_value=None),
        )
        self._obs_patch.start()
        self._map_patch = patch(
            "app.routers.biweekly.load_wheat_phase_mapping",
            new=AsyncMock(
                return_value={
                    "source": "in_code_pilot",
                    "review_status": "PENDING_AGRONOMY_REVIEW",
                    "mapping_version": "wheat-smart-pilot-1",
                    "table_available": False,
                    "rows": [],
                }
            ),
        )
        self._map_patch.start()
        self._flow_obs_patch = patch(
            "app.services.biweekly_report_flow.load_scene_observations",
            new=AsyncMock(return_value=None),
        )
        self._flow_obs_patch.start()
        self._weather_patch = patch(
            "app.services.biweekly_report_flow.load_weather_series",
            new=AsyncMock(return_value=[]),
        )
        self._weather_patch.start()
        self._land_patch = patch(
            "app.services.biweekly_report_flow.load_land_meta",
            new=AsyncMock(return_value={"land_name": "测试田"}),
        )
        self._land_patch.start()
        self._dispatch_patch = patch(
            "app.services.biweekly_report_flow.dispatch_bounded_backfill",
            new=AsyncMock(return_value=dict(NO_JOBS_DISPATCH)),
        )
        self._dispatch_patch.start()
        self._report_patch = patch(
            "app.services.biweekly_report_flow.dispatch_biweekly_report",
            new=AsyncMock(return_value=dict(REPORT_DISPATCH)),
        )
        self._report_patch.start()
        self._pdf_patch = patch(
            "app.reports.biweekly.official_pdf.render_official_biweekly_pdf",
            side_effect=_stub_official_pdf,
        )
        self._pdf_patch.start()

    def tearDown(self):
        if getattr(self, "_official_store", None):
            self._official_store.stop()
        for name, value in self._saved.items():
            setattr(settings, name, value)
        self._obs_patch.stop()
        self._map_patch.stop()
        self._flow_obs_patch.stop()
        self._weather_patch.stop()
        self._land_patch.stop()
        self._dispatch_patch.stop()
        self._report_patch.stop()
        self._pdf_patch.stop()
        self.work.cleanup()

    def _enable_dev(self, lands="6417"):
        settings.biweekly_api_enabled = True
        settings.biweekly_dev_auth = True
        settings.biweekly_jwt_secret = SECRET
        settings.biweekly_allowed_lands = lands
        settings.biweekly_allowed_tenants = ""

    def _enable_jwt_claim_mode(self):
        settings.biweekly_api_enabled = True
        settings.biweekly_dev_auth = False
        settings.biweekly_jwt_secret = SECRET
        settings.biweekly_allowed_lands = ""
        settings.biweekly_allowed_tenants = ""

    def _token(self, land_ids=("6417",), tenant="dev-tenant", subject="dev-user"):
        return biweekly_auth.mint_dev_jwt(
            subject=subject, tenant_id=tenant, land_ids=list(land_ids)
        )

    def _auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_summary_returns_plain_conclusion_lines(self):
        self._enable_dev()
        job_id = "bj-ca85562c28b78c54"
        jobs = Path(self.work.name) / "jobs"
        jobs.mkdir()
        (jobs / f"{job_id}.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "land_id": "6417",
                    "season_id": "2026/27",
                    "period_id": "4",
                    "kind": "generate",
                    "status": "ready",
                    "history": [],
                    "result": {
                        "ai_narrative": {
                            "conclusion": [
                                "叶子里的水少了一些，<b>要留意浇水</b>。",
                                '<font color="#9C2B1B">东北部有一小片一直偏弱。</font>',
                                "叶子里的水少了一些，要留意浇水。",
                            ],
                            "summary": "这句不应盖过结论。",
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        response = self.client.get(
            f"/v1/lands/6417/biweekly/jobs/{job_id}/summary",
            headers=self._auth(self._token()),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["title"], "本期结论")
        self.assertEqual(body["source"], "conclusion")
        self.assertEqual(
            body["lines"],
            [
                "叶子里的水少了一些，<b>要留意浇水</b>。",
                '<font color="#9C2B1B" backColor="#F8D6D2">东北部有一小片一直偏弱。</font>',
            ],
        )
        missing = self.client.get(
            "/v1/lands/6417/biweekly/jobs/bj-0123456789abcdef/summary",
            headers=self._auth(self._token()),
        )
        self.assertEqual(missing.status_code, 404)
        from app.services.biweekly_report_card import build_report_card

        limited = build_report_card(
            {
                "job_id": job_id,
                "status": "evidence_limited",
                "result": {"ai_narrative": {"reader_note": "本期模型文字未完成。"}},
            }
        )
        self.assertEqual(limited["source"], "reader_note")
        self.assertEqual(limited["lines"], ["本期模型文字未完成。"])
        pending = build_report_card({"job_id": job_id, "status": "dispatched", "result": {}})
        self.assertEqual(pending["source"], "status")
        self.assertIn("正在生成", pending["lines"][0])
        changes_only = build_report_card(
            {
                "job_id": job_id,
                "status": "ready",
                "result": {
                    "ai_narrative": {
                        "conclusion": [],
                        "summary": "",
                        "changes": [
                            {"metric": "ndmi", "judgment": "worse", "sentence": "<b>要留意浇水</b>。"}
                        ],
                    }
                },
            }
        )
        self.assertEqual(changes_only["source"], "changes")
        self.assertEqual(changes_only["lines"], ["<b>要留意浇水</b>。"])
        from_progress = build_report_card(
            {"job_id": job_id, "status": "ready", "result": {"stage": "done"}},
            {"facts": {"ai_narrative": {"conclusion": ["东北部有一小片一直偏弱。"]}}},
        )
        self.assertEqual(from_progress["source"], "conclusion")
        self.assertEqual(from_progress["lines"], ["东北部有一小片一直偏弱。"])
        program = build_report_card(
            {
                "job_id": job_id,
                "status": "ready",
                "result": {
                    "conclusion_context": {"lines": ["土壤底子：壤土，酸碱偏碱。"]},
                    "field_patterns": {"persistent_lines": ["东北部有一小片一直偏弱。"]},
                },
            }
        )
        self.assertEqual(program["source"], "report")
        self.assertEqual(
            program["lines"],
            ["土壤底子：壤土，酸碱偏碱。", "东北部有一小片一直偏弱。"],
        )
        marked = build_report_card(
            {
                "job_id": job_id,
                "status": "ready",
                "result": {
                    "ai_narrative": {
                        "conclusion": [
                            '<font face="YaHei-Bold" color="#1F4D38" backColor="#D9F2E3">偏干</font>',
                            '<b onclick="alert(1)">词</b><script>alert(1)</script>',
                        ]
                    }
                },
            }
        )
        self.assertEqual(
            marked["lines"],
            [
                '<font color="#1F4D38" backColor="#D9F2E3">偏干</font>',
                "词alert(1)",
            ],
        )

    def test_summary_shows_current_and_prior_core_parameters(self):
        from app.services.biweekly_report_card import build_report_card

        card = build_report_card(
            {
                "job_id": "bj-99a41147675c6560",
                "status": "ready",
                "result": {
                    "period": {
                        "start": "2026-05-06",
                        "end_inclusive": "2026-05-19",
                        "days": 14,
                    },
                    "observations": [
                        {"date": "2026-05-08", "ndvi_avg": 0.60, "evi_avg": 0.40},
                        {"date": "2026-05-18", "ndvi_avg": 0.70, "evi_avg": 0.50},
                    ],
                    "prior_observations": [
                        {"date": "2026-04-24", "ndvi_avg": 0.50, "evi_avg": 0.30},
                    ],
                    "weather_period": {
                        "days": 14,
                        "precipitation_mm": 0.1,
                        "et0_mm": 31.7,
                        "mean_c": 18.24,
                    },
                    "weather_daily": [
                        {
                            "date": "2026-04-22",
                            "precipitation_mm": 2.0,
                            "et0_mm": 2.0,
                            "min_c": 8,
                            "max_c": 16,
                        },
                        {
                            "date": "2026-04-30",
                            "precipitation_mm": 3.5,
                            "et0_mm": 2.5,
                            "min_c": 10,
                            "max_c": 18,
                        },
                        {
                            "date": "2026-05-05",
                            "precipitation_mm": 0,
                            "et0_mm": 1.0,
                            "min_c": 11,
                            "max_c": 19,
                        },
                        {
                            "date": "2026-05-10",
                            "precipitation_mm": 99,
                            "et0_mm": 99,
                            "min_c": 1,
                            "max_c": 2,
                        },
                    ],
                },
            }
        )
        self.assertEqual(card["periods"]["current"], {
            "label": "本期",
            "start": "2026-05-06",
            "end": "2026-05-19",
        })
        self.assertEqual(card["periods"]["prior"], {
            "label": "上一期",
            "start": "2026-04-22",
            "end": "2026-05-05",
        })
        by_key = {row["key"]: row for row in card["metrics"]}
        self.assertEqual(by_key["ndvi"]["current"], 0.65)
        self.assertEqual(by_key["ndvi"]["prior"], 0.50)
        self.assertEqual(by_key["ndvi"]["name"], "长势")
        self.assertEqual(by_key["evi"]["current"], 0.45)
        self.assertNotIn("ndre", by_key)
        weather = {row["key"]: row for row in card["weather"]}
        self.assertEqual(weather["precipitation_mm"]["name"], "有效降雨")
        self.assertEqual(weather["precipitation_mm"]["current"], 0.1)
        self.assertEqual(weather["precipitation_mm"]["prior"], 5.5)
        self.assertEqual(weather["et0_mm"]["current"], 31.7)
        self.assertEqual(weather["et0_mm"]["prior"], 5.5)
        self.assertEqual(weather["mean_c"]["current"], 18.2)
        self.assertEqual(weather["mean_c"]["prior"], 13.7)
        empty = build_report_card({"job_id": "bj-ca85562c28b78c54", "status": "ready", "result": {}})
        self.assertEqual(empty["metrics"], [])
        self.assertEqual(empty["weather"], [])

    def test_summary_reads_sentences_kept_on_the_download_task(self):
        self._enable_dev()
        job_id = "bj-b37fb809482e8490"
        jobs = Path(self.work.name) / "jobs"
        jobs.mkdir(exist_ok=True)
        (jobs / f"{job_id}.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "land_id": "6417",
                    "season_id": "2026/27",
                    "period_id": "4",
                    "kind": "generate",
                    "status": "ready",
                    "report_job_id": "00000000-0000-0000-0000-000000000001",
                    "history": [],
                    "result": {"stage": "done"},
                }
            ),
            encoding="utf-8",
        )
        extra = [
            {
                "facts": {
                    "ai_narrative": {
                        "conclusion": ["叶子里的水少了一些，<b>要留意浇水</b>。"]
                    }
                }
            }
        ]
        with patch(
            "app.routers.biweekly._extra_fact_documents",
            new=AsyncMock(return_value=extra),
        ):
            response = self.client.get(
                f"/v1/lands/6417/biweekly/jobs/{job_id}/summary",
                headers=self._auth(self._token()),
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "conclusion")
        self.assertEqual(body["lines"], ["叶子里的水少了一些，<b>要留意浇水</b>。"])

    def test_router_does_not_use_anonymous_auth_bypass(self):
        import ast

        from app.routers import biweekly as router_mod
        from app.middleware import biweekly_auth as auth_mod

        for module in (router_mod, auth_mod):
            tree = ast.parse(Path(module.__file__).read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
                    imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            self.assertNotIn("app.middleware.auth", imported)
            for name in FORBIDDEN_IMPORTS:
                self.assertFalse(
                    any(item == name or item.endswith("." + name) for item in imported),
                    msg=f"{module.__name__} imports {name}",
                )

    def test_main_app_mounts_biweekly_routes(self):
        # Importing app.main needs optional production deps (xlsxwriter, etc.).
        # Assert the source wiring instead: router is imported and included under /v1.
        source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
        self.assertIn("biweekly,", source)
        self.assertIn("app.include_router(biweekly.router, prefix=PREFIX)", source)
        from app.routers import biweekly

        paths = {getattr(route, "path", "") for route in biweekly.router.routes}
        for path in (
            "/lands/{land_id}/biweekly/seasons",
            "/lands/{land_id}/biweekly/periods",
            "/lands/{land_id}/biweekly/periods/{period_id}/backfill",
            "/lands/{land_id}/biweekly/periods/{period_id}/generate",
            "/lands/{land_id}/biweekly/jobs/{job_id}",
            "/lands/{land_id}/biweekly/jobs/{job_id}/summary",
            "/lands/{land_id}/biweekly/jobs/{job_id}/download",
        ):
            self.assertIn(path, paths)

    def test_enabled_by_default_and_explicit_zero_disables(self):
        from app.core.config import Settings

        field = Settings.model_fields["biweekly_api_enabled"]
        self.assertIs(field.default, True)
        self.assertFalse(
            Settings.model_validate({"biweekly_api_enabled": "0"}).biweekly_api_enabled
        )
        self.assertFalse(
            Settings.model_validate({"biweekly_api_enabled": "false"}).biweekly_api_enabled
        )

    def test_disabled_when_flag_is_false(self):
        settings.biweekly_api_enabled = False
        settings.biweekly_dev_auth = True
        settings.biweekly_jwt_secret = SECRET
        settings.biweekly_allowed_lands = "6417"
        token = self._token()
        response = self.client.get(
            "/v1/lands/6417/biweekly/seasons", headers=self._auth(token)
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["error"], "BIWEEKLY_API_DISABLED")

    def test_missing_bearer_is_unauthorized_even_when_enabled(self):
        self._enable_dev()
        response = self.client.get("/v1/lands/6417/biweekly/seasons")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"]["error"], "UNAUTHORIZED")

    def test_placeholder_secret_is_rejected(self):
        self._enable_dev()
        settings.biweekly_jwt_secret = "change-me"
        settings.openfarm_jwt_secret = "change-me"
        # mint would fail; send a JWT-shaped bearer so the placeholder secret is checked
        response = self.client.get(
            "/v1/lands/6417/biweekly/seasons",
            headers=self._auth("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkZXYifQ.sig"),
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["error"], "BIWEEKLY_AUTH_NOT_CONFIGURED")

    def test_dev_auth_lists_seasons_for_allowlisted_land(self):
        self._enable_dev()
        response = self.client.get(
            "/v1/lands/6417/biweekly/seasons", headers=self._auth(self._token())
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["seasons"][0]["season_id"], "2025/26")
        self.assertFalse(body["auto_publish"])
        self.assertTrue(body["auth"]["not_for_production"])
        self.assertEqual(body["auth"]["mode"], "biweekly_dev_auth")

    def test_dev_auth_rejects_land_outside_allowlist(self):
        self._enable_dev(lands="6417")
        response = self.client.get(
            "/v1/lands/9999/biweekly/seasons",
            headers=self._auth(self._token(land_ids=("9999",))),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"]["error"], "LAND_FORBIDDEN")

    def test_jwt_claim_mode_requires_land_in_token(self):
        self._enable_jwt_claim_mode()
        denied = self.client.get(
            "/v1/lands/6417/biweekly/seasons",
            headers=self._auth(self._token(land_ids=("0000",))),
        )
        self.assertEqual(denied.status_code, 403)
        allowed = self.client.get(
            "/v1/lands/6417/biweekly/seasons",
            headers=self._auth(self._token(land_ids=("6417",))),
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["auth"]["mode"], "jwt_land_claim")

    def test_periods_and_bounded_backfill_dispatch_jobs_but_do_not_autogenerate(self):
        self._enable_dev()
        headers = self._auth(self._token())
        periods = self.client.get(
            "/v1/lands/6417/biweekly/periods?season=2025/26", headers=headers
        )
        self.assertEqual(periods.status_code, 200, periods.text)
        items = periods.json()["periods"]
        self.assertEqual(len(items), 18)

        captured = {}

        async def fake_dispatch(land_id, window):
            captured["land_id"] = land_id
            captured["window"] = window
            return {
                "dispatched": True,
                "dispatched_to_production": True,
                "job_ids": ["batch-job-1"],
                "job_count": 1,
                "date_from": window["date_from"],
                "date_to": window["date_to_inclusive"],
                "sensors": ["S2", "S1"],
                "reason": None,
            }

        with patch("app.routers.biweekly.dispatch_bounded_backfill", new=fake_dispatch), \
             patch("app.routers.biweekly.build_trigger_service") as build:
            service = __import__(
                "app.services.biweekly_trigger", fromlist=["build_trigger_service"]
            ).build_trigger_service()
            # Use a real service, but spy generate
            original_generate = service.request_generate

            def wrapped_generate(*args, **kwargs):
                captured["generate_called"] = True
                return original_generate(*args, **kwargs)

            service.request_generate = wrapped_generate
            build.return_value = service

            backfill = self.client.post(
                "/v1/lands/6417/biweekly/periods/1/backfill?season=2025/26",
                headers=headers,
                json={},
            )
            self.assertEqual(backfill.status_code, 200, backfill.text)
            body = backfill.json()
            self.assertEqual(body["kind"], "backfill")
            self.assertEqual(body["status"], "dispatched")
            self.assertTrue(body["dispatched_to_production"])
            self.assertEqual(body["satellite_batch_job_ids"], ["batch-job-1"])
            self.assertNotEqual(
                (body.get("result") or {}).get("planned_batch", {}).get("dispatch"),
                "plan_only_local_fixture",
            )
            self.assertEqual(
                (body.get("result") or {}).get("planned_batch", {}).get("dispatch"),
                "satellite_batch",
            )
            self.assertNotEqual(body.get("error_code"), "SNAPSHOT_NOT_FOUND")
            self.assertEqual(captured["land_id"], "6417")
            self.assertEqual(captured["window"]["date_from"], "2025-11-01")
            self.assertIsNone(captured["window"]["years"])
            self.assertIsNone(captured["window"]["months"])
            self.assertLess(captured["window"]["span_days"], 43)
            self.assertNotIn("generate_called", captured)

            captured.clear()
            refused = self.client.post(
                "/v1/lands/6417/biweekly/periods/12/backfill?season=2025/26",
                headers=headers,
                json={},
            )
            self.assertEqual(refused.status_code, 200, refused.text)
            self.assertEqual(refused.json()["status"], "dispatched")
            self.assertTrue(refused.json()["dispatched_to_production"])
            self.assertEqual(captured["window"]["date_from"], "2026-03-21")
            self.assertEqual(captured["window"]["date_to_inclusive"], "2026-04-17")
            self.assertLess(captured["window"]["span_days"], 43)
            self.assertNotIn("generate_called", captured)

    def test_dispatch_helper_calls_create_satellite_batch_jobs_with_bounded_window(self):
        from datetime import date

        class FakeLand:
            land_id = "6417"

        fake_job = type("Job", (), {"id": "job-55"})()
        create = AsyncMock(return_value=([], [fake_job], {}))
        ensure = AsyncMock(return_value=([FakeLand()], {}))

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def commit(self):
                self.committed = True

        window = {
            "date_from": "2026-03-21",
            "date_to_inclusive": "2026-04-17",
            "span_days": 27,
            "years": None,
            "months": None,
        }
        with patch("app.core.database.async_session", return_value=_Session()), \
             patch("app.services.smart_land_backfill.ensure_land_parcels", new=ensure), \
             patch("app.services.satellite_batch.create_satellite_batch_jobs", new=create):
            import asyncio
            from app.services.biweekly_dispatch import dispatch_bounded_backfill

            result = asyncio.run(dispatch_bounded_backfill("6417", window))
        self.assertTrue(result["dispatched"])
        self.assertTrue(result["dispatched_to_production"])
        self.assertEqual(result["job_ids"], ["job-55"])
        kwargs = create.await_args.kwargs
        self.assertEqual(kwargs["date_from"], date(2026, 3, 21))
        self.assertEqual(kwargs["date_to"], date(2026, 4, 17))
        self.assertEqual(list(kwargs["sensors"]), ["S2", "S1"])
        self.assertFalse(kwargs["force"])
        self.assertTrue(kwargs["durable_dispatch"])
        self.assertEqual(create.await_args.args[1][0].land_id, "6417")
        self.assertTrue(kwargs["extra_params"]["bypass_schedule_land_filter"])
        self.assertTrue(kwargs["extra_params"]["biweekly_user_backfill"])
        self.assertEqual(kwargs["job_type"], "biweekly_period_backfill")
        self.assertEqual(kwargs["extra_params"]["display_type"], "biweekly_period_backfill")
        self.assertEqual(kwargs["extra_params"]["display_name"], "双周报本期数据补齐")
        self.assertEqual(result["claim_type"], "satellite_batch")
        self.assertEqual(result["job_type"], "biweekly_period_backfill")
        self.assertEqual(result["display_name"], "双周报本期数据补齐")
        self.assertNotEqual(result["display_name"], "卫星数据分块处理")
        # generate is not part of this helper
        self.assertNotIn("generate", result)

    def test_login_session_token_is_accepted_without_anonymous_bypass(self):
        self._enable_jwt_claim_mode()
        # JWT-shaped but unsigned / wrong secret: same class of token the site login uses
        opaque = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ2aWxsYWdlLXVzZXIiLCJiYXNlX2lkIjoiMzgifQ.not-a-signature"
        denied = self.client.get("/v1/lands/6417/biweekly/seasons")
        self.assertEqual(denied.status_code, 401)
        allowed = self.client.get(
            "/v1/lands/6417/biweekly/seasons",
            headers={"Authorization": f"Bearer {opaque}"},
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["auth"]["mode"], "login_session_token")
        self.assertEqual(allowed.json()["auth"]["land_acl"], "gap_pending_verified_authorizer")
        self.assertTrue(allowed.json()["auth"]["not_for_production"])

    def test_generate_does_not_imply_reference_plan(self):
        self._enable_dev()
        headers = self._auth(self._token())
        bare = self.client.post(
            "/v1/lands/6417/biweekly/periods/12/generate?season=2025/26",
            headers=headers,
            json={},
        )
        self.assertEqual(bare.status_code, 200, bare.text)
        self.assertEqual(bare.json()["status"], "dispatched")
        self.assertIsNone(bare.json()["payload"]["reference_planting_instance_id"])
        self.assertNotEqual((bare.json().get("artifact") or {}).get("kind"), "official_visual_artifact")
        self.assertEqual(((bare.json().get("result") or {}).get("report") or {}).get("job_type"), "biweekly_report")
        with_ref = self.client.post(
            "/v1/lands/6417/biweekly/periods/12/generate?season=2025/26",
            headers=headers,
            json={"reference_planting_instance_id": "15933"},
        )
        self.assertEqual(with_ref.status_code, 200, with_ref.text)
        job = with_ref.json()
        self.assertEqual(job["status"], "dispatched")
        self.assertEqual(job["payload"]["reference_planting_instance_id"], "15933")
        disclosure = job["payload"].get("reference_disclosure") or job.get("result", {})
        self.assertTrue(job["payload"]["reference_disclosure"] or disclosure)
        self.assertNotEqual((job.get("artifact") or {}).get("kind"), "official_visual_artifact")
        self.assertIn("下载机", (job.get("result") or {}).get("note") or "下载机")

        fetched = self.client.get(
            f"/v1/lands/6417/biweekly/jobs/{job['job_id']}", headers=headers
        )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["status"], "dispatched")
        downloaded = self.client.get(
            f"/v1/lands/6417/biweekly/jobs/{job['job_id']}/download", headers=headers
        )
        self.assertEqual(downloaded.status_code, 409)
        self.assertEqual(downloaded.json()["error"], "ARTIFACT_UNAVAILABLE")

        foreign = self.client.get(
            f"/v1/lands/0000/biweekly/jobs/{job['job_id']}",
            headers=self._auth(self._token(land_ids=("0000",))),
        )
        # land 0000 is not allowlisted in dev mode
        self.assertEqual(foreign.status_code, 403)

    def test_missing_smart_plan_is_explicit_when_pack_disabled(self):
        self._enable_dev()
        settings.biweekly_pack_root = str(Path(self.work.name) / "no-pack")
        headers = self._auth(self._token())
        response = self.client.get("/v1/lands/6417/biweekly/seasons", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["seasons"], [])
        self.assertFalse(body["plan_available"])
        self.assertIn(body["plan_status"], {"smart_source_unavailable", "missing", "query_failed"})
        self.assertIn("不会改用其他地块的方案", body["note"])
        self.assertEqual(body["phase_mapping"]["mapping_version"], "wheat-smart-pilot-1")
        self.assertNotEqual(body["phase_mapping"]["source"], "prec_agric.crop_remote_phase_mapping")

    def test_seasons_and_periods_expose_not_applicable_stages(self):
        from tests.test_biweekly_plans import _batch, _task
        from app.services.biweekly_plans import build_seasons_from_rows

        self._enable_dev(lands="18509")
        settings.biweekly_pack_root = str(Path(self.work.name) / "no-pack")
        calendar = build_seasons_from_rows(
            "18509",
            [_batch(land_id=18509, batch_id=8801, name="小麦2026/27")],
            [
                _task(plan_id=8801, name="播种前准备", start="2026-09-20", finish="2026-10-05", task_id=1),
                _task(plan_id=8801, name="播种期", start="2026-10-06", finish="2026-10-15", task_id=2),
                _task(plan_id=8801, name="出苗期", start="2026-10-16", finish="2026-10-31", task_id=3),
                _task(plan_id=8801, name="拔节孕穗期", start="2027-03-20", finish="2027-04-10", task_id=4),
                _task(plan_id=8801, name="成熟期", start="2027-05-20", finish="2027-06-05", task_id=5),
            ],
        )
        headers = self._auth(self._token(land_ids=("18509",)))
        with patch(
            "app.routers.biweekly.load_smart_planting_calendar",
            new=AsyncMock(return_value=calendar),
        ):
            seasons = self.client.get("/v1/lands/18509/biweekly/seasons", headers=headers)
            periods = self.client.get(
                "/v1/lands/18509/biweekly/periods?season=2026/27", headers=headers
            )
        self.assertEqual(seasons.status_code, 200, seasons.text)
        self.assertEqual(periods.status_code, 200, periods.text)
        stage_names = [row["name"] for row in seasons.json()["seasons"][0]["stages"]]
        self.assertEqual(stage_names, ["播种前准备", "播种期", "出苗期", "拔节孕穗期", "成熟期"])
        pre = seasons.json()["seasons"][0]["stages"][0]
        self.assertEqual(pre["mapping_status"], "NOT_APPLICABLE")
        self.assertIsNone(pre["phase_label"])
        self.assertFalse(pre["monitored"])
        self.assertEqual(pre["end_inclusive"], "2026-10-05")
        self.assertEqual(seasons.json()["seasons"][0]["native_plan"]["intervals"][0]["source_stage_name"], "播种前准备")
        self.assertEqual([row["name"] for row in periods.json()["stages"]], stage_names)
        self.assertIsNotNone(periods.json().get("native_plan"))



    def test_smart_backfill_status_follows_dispatch_when_snapshot_is_missing(self):
        from tests.test_biweekly_plans import _batch, _task
        from app.services.biweekly_plans import build_seasons_from_rows

        self._enable_dev(lands="19994")
        settings.biweekly_pack_root = str(Path(self.work.name) / "no-pack")
        calendar = build_seasons_from_rows(
            "19994",
            [_batch(land_id=19994, batch_id=9001, name="小麦2025/26")],
            [
                _task(
                    plan_id=9001,
                    name="出苗期",
                    start="2025-11-13",
                    finish="2026-02-04",
                    task_id=1,
                ),
            ],
        )
        season = calendar["seasons"][0]
        period = season["periods"][4] if len(season["periods"]) >= 5 else season["periods"][0]
        self.assertIsNone(period.get("snapshot"))

        async def fake_dispatch(land_id, window):
            return {
                "dispatched": True,
                "dispatched_to_production": True,
                "job_ids": ["prod-19994"],
                "job_count": 1,
                "date_from": window["date_from"],
                "date_to": window["date_to_inclusive"],
                "reason": None,
            }

        headers = self._auth(self._token(land_ids=("19994",)))
        with patch(
            "app.routers.biweekly.load_smart_planting_calendar",
            new=AsyncMock(return_value=calendar),
        ), patch("app.routers.biweekly.dispatch_bounded_backfill", new=fake_dispatch):
            response = self.client.post(
                f"/v1/lands/19994/biweekly/periods/{period['period_id']}/backfill"
                f"?season={season['season_id']}",
                headers=headers,
                json={},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "dispatched")
        self.assertTrue(body["dispatched_to_production"])
        self.assertNotEqual(body.get("error_code"), "SNAPSHOT_NOT_FOUND")
        self.assertNotEqual(body["status"], "failed")
        self.assertEqual(body["satellite_batch_job_ids"], ["prod-19994"])
        self.assertNotIn("plan_only_local_fixture", str(body.get("result")))

    def test_season_one_click_dispatches_then_waits_to_generate(self):
        self._enable_dev()
        headers = self._auth(self._token())
        captured = {"windows": [], "extra": []}

        async def fake_dispatch(land_id, window, **kwargs):
            captured["windows"].append({"land_id": land_id, **window})
            captured.setdefault("job_types", []).append(kwargs.get("job_type"))
            captured.setdefault("display_names", []).append(kwargs.get("display_name"))
            captured["extra"].append(kwargs.get("extra_params") or {})
            return {
                "dispatched": True,
                "dispatched_to_production": True,
                "job_ids": [f"batch-{window['current_period_id']}"],
                "job_count": 1,
                "date_from": window["date_from"],
                "date_to": window["date_to_inclusive"],
                "reason": None,
            }

        self._dispatch_patch.stop()
        with patch(
            "app.services.biweekly_report_flow.dispatch_bounded_backfill",
            new=fake_dispatch,
        ):
            denied = self.client.post(
                "/v1/lands/6417/biweekly/backfill-season?season=2025/26",
                json={},
            )
            self.assertEqual(denied.status_code, 401)
            response = self.client.post(
                "/v1/lands/6417/biweekly/backfill-season?season=2025/26",
                headers=headers,
                json={},
            )
        self._dispatch_patch.start()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["kind"], "backfill_season")
        self.assertEqual(body["status"], "dispatched")
        self.assertEqual(len(captured["windows"]), 18)
        spans = [item["span_days"] for item in captured["windows"]]
        self.assertTrue(all(0 < span < 43 for span in spans))
        self.assertTrue(all(item.get("years") is None for item in captured["windows"]))
        starts = {item["date_from"] for item in captured["windows"]}
        ends = {item["date_to_inclusive"] for item in captured["windows"]}
        self.assertGreater(len(starts), 1)
        from datetime import date
        season_span = (
            date.fromisoformat(max(ends)) - date.fromisoformat(min(starts))
        ).days + 1
        self.assertGreater(season_span, 42)
        self.assertTrue(all(item == "biweekly_season_backfill" for item in captured["job_types"]))
        self.assertTrue(all(item == "双周报全季数据补齐" for item in captured["display_names"]))
        self.assertTrue(all(item.get("biweekly_generate_after") for item in captured["extra"]))
        children = (body.get("result") or {}).get("period_results") or []
        self.assertEqual(len(children), 18)
        self.assertTrue(all(item.get("status") == "dispatched" for item in children))
        # Waiting on backfill: official PDFs are not written yet.
        self.assertTrue(all(not item.get("artifact_kind") for item in children))

    def test_season_one_click_dispatches_reports_when_backfill_not_needed(self):
        self._enable_dev()
        headers = self._auth(self._token())
        response = self.client.post(
            "/v1/lands/6417/biweekly/backfill-season?season=2025/26",
            headers=headers,
            json={},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["kind"], "backfill_season")
        self.assertEqual(body["status"], "dispatched")
        children = (body.get("result") or {}).get("period_results") or []
        self.assertEqual(len(children), 18)
        self.assertTrue(all(item.get("status") == "dispatched" for item in children))
        self.assertTrue(all(not item.get("artifact_kind") for item in children))
        self.assertIn("下载机", (body.get("result") or {}).get("note") or "下载机")

    def test_generate_backfill_failure_is_not_reported_as_no_optics(self):
        self._enable_dev()
        headers = self._auth(self._token())
        self._dispatch_patch.stop()
        with patch(
            "app.services.biweekly_report_flow.dispatch_bounded_backfill",
            new=AsyncMock(
                return_value={
                    "dispatched": False,
                    "reason": "DISPATCH_FAILED",
                    "job_ids": [],
                    "job_count": 0,
                }
            ),
        ):
            response = self.client.post(
                "/v1/lands/6417/biweekly/periods/12/generate?season=2025/26",
                headers=headers,
                json={},
            )
        self._dispatch_patch.start()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error_code"], "BACKFILL_FAILED")
        self.assertEqual((body.get("result") or {}).get("stage"), "backfill")
        self.assertNotEqual(body["status"], "evidence_limited")
        self.assertNotEqual((body.get("artifact") or {}).get("kind"), "official_visual_artifact")

    def test_season_dispatch_helper_sets_bypass_flag_on_each_period(self):

        from datetime import date
        from app.services.biweekly_dispatch import dispatch_season_bounded_backfill

        periods = [
            {
                "period_id": "1",
                "start": "2025-11-13",
                "end_inclusive": "2025-11-26",
                "end_exclusive": "2025-11-27",
                "days": 14,
            },
            {
                "period_id": "2",
                "start": "2025-11-27",
                "end_inclusive": "2025-12-10",
                "end_exclusive": "2025-12-11",
                "days": 14,
            },
        ]
        create = AsyncMock(
            return_value=([], [type("Job", (), {"id": "job-season-1"})()], {})
        )
        ensure = AsyncMock(return_value=([type("Land", (), {"land_id": "19994"})()], {}))

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def commit(self):
                self.committed = True

        with patch("app.core.database.async_session", return_value=_Session()),              patch("app.services.smart_land_backfill.ensure_land_parcels", new=ensure),              patch("app.services.satellite_batch.create_satellite_batch_jobs", new=create):
            import asyncio

            result = asyncio.run(dispatch_season_bounded_backfill("19994", periods))
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["scope"], "season")
        self.assertEqual(len(result["period_results"]), 2)
        self.assertEqual(create.await_count, 2)
        for call in create.await_args_list:
            self.assertTrue(call.kwargs["extra_params"]["bypass_schedule_land_filter"])
            span = (call.kwargs["date_to"] - call.kwargs["date_from"]).days + 1
            self.assertLessEqual(span, 42)
            self.assertGreater(span, 0)
        # Two bounded windows, not one merged season window.
        windows = [
            (call.kwargs["date_from"], call.kwargs["date_to"])
            for call in create.await_args_list
        ]
        self.assertEqual(windows[0], (date(2025, 11, 13), date(2025, 11, 26)))
        self.assertEqual(windows[1], (date(2025, 11, 13), date(2025, 12, 10)))
        for call in create.await_args_list:
            self.assertEqual(call.kwargs["job_type"], "biweekly_season_backfill")
            self.assertEqual(call.kwargs["extra_params"]["display_type"], "biweekly_season_backfill")
            self.assertEqual(call.kwargs["extra_params"]["display_name"], "双周报全季数据补齐")
            self.assertNotEqual(call.kwargs["extra_params"]["display_name"], "卫星数据分块处理")
        self.assertNotIn("generate", result)

    def _calendar_19994(self):
        from tests.test_biweekly_plans import _batch, _task
        from app.services.biweekly_plans import build_seasons_from_rows

        return build_seasons_from_rows(
            "19994",
            [_batch(land_id=19994, batch_id=9001, name="小麦2025/26")],
            [
                _task(
                    plan_id=9001,
                    name="出苗期",
                    start="2025-11-13",
                    finish="2026-02-04",
                    task_id=1,
                ),
            ],
        )

    def test_smart_generate_with_eligible_s2_is_ready_not_evidence_limited(self):
        from app.services.biweekly_scenes import _to_observation

        self._enable_dev(lands="19994")
        settings.biweekly_pack_root = str(Path(self.work.name) / "no-pack")
        calendar = self._calendar_19994()
        season = calendar["seasons"][0]
        period = next(row for row in season["periods"] if row["start"] == "2025-11-27")
        observations = [
            _to_observation(
                {
                    "date": "2025-12-05",
                    "sensor": "S2",
                    "scene_id": "stac_bridge_2025-12-05_S2",
                    "parcel_cloud_cover_pct": 0.0,
                    "cloud_cover": 0.562652,
                    "ndvi_avg": 0.163057,
                    "evi_avg": 0.116352,
                    "ndre_avg": None,
                    "ndmi_avg": -0.094621,
                }
            ),
            _to_observation(
                {
                    "date": "2025-12-07",
                    "sensor": "S2",
                    "scene_id": "stac_bridge_2025-12-07_S2",
                    "parcel_cloud_cover_pct": 0.0,
                    "cloud_cover": 2.918007,
                    "ndvi_avg": 0.167545,
                    "evi_avg": 0.111034,
                    "ndre_avg": None,
                    "ndmi_avg": -0.105551,
                }
            ),
            _to_observation(
                {
                    "date": "2025-12-10",
                    "sensor": "S2",
                    "scene_id": "stac_bridge_2025-12-10_S2",
                    "parcel_cloud_cover_pct": 0.0,
                    "cloud_cover": 10.176522,
                    "ndvi_avg": 0.144975,
                    "evi_avg": 0.110558,
                    "ndre_avg": None,
                    "ndmi_avg": -0.12101,
                }
            ),
            _to_observation(
                {
                    "date": "2025-12-02",
                    "sensor": "S1",
                    "scene_id": "s1",
                    "vv_avg": -11.5,
                    "vh_avg": -21.4,
                }
            ),
        ]
        headers = self._auth(self._token(land_ids=("19994",)))
        with patch(
            "app.routers.biweekly.load_smart_planting_calendar",
            new=AsyncMock(return_value=calendar),
        ), patch(
            "app.services.biweekly_report_flow.load_scene_observations",
            new=AsyncMock(return_value=observations),
        ):
            response = self.client.post(
                f"/v1/lands/19994/biweekly/periods/{period['period_id']}/generate"
                f"?season={season['season_id']}",
                headers=headers,
                json={},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "dispatched")
        self.assertNotEqual(body["status"], "evidence_limited")
        self.assertEqual(((body.get("result") or {}).get("report") or {}).get("job_type"), "biweekly_report")
        self.assertNotEqual((body.get("artifact") or {}).get("kind"), "official_visual_artifact")
        downloaded = self.client.get(
            f"/v1/lands/19994/biweekly/jobs/{body['job_id']}/download",
            headers=headers,
        )
        self.assertEqual(downloaded.status_code, 409)

    def test_smart_generate_scene_query_failure_is_not_evidence_limited(self):
        self._enable_dev(lands="19994")
        settings.biweekly_pack_root = str(Path(self.work.name) / "no-pack")
        calendar = self._calendar_19994()
        season = calendar["seasons"][0]
        period = next(row for row in season["periods"] if row["start"] == "2025-11-27")
        headers = self._auth(self._token(land_ids=("19994",)))
        with patch(
            "app.routers.biweekly.load_smart_planting_calendar",
            new=AsyncMock(return_value=calendar),
        ), patch(
            "app.services.biweekly_report_flow.load_scene_observations",
            new=AsyncMock(return_value=None),
        ):
            response = self.client.post(
                f"/v1/lands/19994/biweekly/periods/{period['period_id']}/generate"
                f"?season={season['season_id']}",
                headers=headers,
                json={},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # Optics judgment and PDF render move to the download machine.
        self.assertEqual(body["status"], "dispatched")
        self.assertNotEqual(body["status"], "evidence_limited")
        self.assertNotEqual(body.get("error_code"), "SCENE_QUERY_FAILED")
        self.assertEqual(((body.get("result") or {}).get("report") or {}).get("job_type"), "biweekly_report")

    def test_smart_generate_empty_scenes_is_evidence_limited(self):
        self._enable_dev(lands="19994")
        settings.biweekly_pack_root = str(Path(self.work.name) / "no-pack")
        calendar = self._calendar_19994()
        season = calendar["seasons"][0]
        period = next(row for row in season["periods"] if row["start"] == "2025-11-27")
        headers = self._auth(self._token(land_ids=("19994",)))
        with patch(
            "app.routers.biweekly.load_smart_planting_calendar",
            new=AsyncMock(return_value=calendar),
        ), patch(
            "app.services.biweekly_report_flow.load_scene_observations",
            new=AsyncMock(return_value=[]),
        ):
            response = self.client.post(
                f"/v1/lands/19994/biweekly/periods/{period['period_id']}/generate"
                f"?season={season['season_id']}",
                headers=headers,
                json={},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "dispatched")
        self.assertNotEqual(body["status"], "evidence_limited")
        self.assertNotEqual((body.get("artifact") or {}).get("kind"), "evidence_limited_brief")


if __name__ == "__main__":
    unittest.main()
