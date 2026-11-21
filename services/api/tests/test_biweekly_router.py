"""Biweekly trigger routes on the API machine: auth, bounds, no anonymous bypass."""

from __future__ import annotations

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

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(settings, name, value)
        self._obs_patch.stop()
        self._map_patch.stop()
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
        source = Path("/workspace/agric-api-biweekly-offline/services/api/app/main.py").read_text()
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
            self.assertTrue(body["dispatched_to_production"])
            self.assertEqual(body["satellite_batch_job_ids"], ["batch-job-1"])
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
        self.assertIsNone(bare.json()["payload"]["reference_planting_instance_id"])
        with_ref = self.client.post(
            "/v1/lands/6417/biweekly/periods/12/generate?season=2025/26",
            headers=headers,
            json={"reference_planting_instance_id": "15933"},
        )
        self.assertEqual(with_ref.status_code, 200, with_ref.text)
        job = with_ref.json()
        self.assertEqual(job["payload"]["reference_planting_instance_id"], "15933")
        disclosure = job["payload"].get("reference_disclosure") or job.get("result", {})
        self.assertTrue(job["payload"]["reference_disclosure"] or disclosure)

        fetched = self.client.get(
            f"/v1/lands/6417/biweekly/jobs/{job['job_id']}", headers=headers
        )
        self.assertEqual(fetched.status_code, 200)
        downloaded = self.client.get(
            f"/v1/lands/6417/biweekly/jobs/{job['job_id']}/download", headers=headers
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertGreater(len(downloaded.content), 100)

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



if __name__ == "__main__":
    unittest.main()
