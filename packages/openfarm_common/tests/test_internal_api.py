"""Unit tests for openfarm_common.internal_api (httpx MockTransport)."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import httpx
from openfarm_common import internal_api as ia


class InternalApiEnabledTests(unittest.TestCase):
    def test_disabled_without_base(self) -> None:
        with patch.dict(
            os.environ, {"API_BASE_URL": "", "INTERNAL_API_TOKEN": "t"}, clear=False
        ):
            self.assertFalse(ia.internal_api_enabled())

    def test_disabled_without_token(self) -> None:
        with patch.dict(
            os.environ,
            {"API_BASE_URL": "http://api:8000", "INTERNAL_API_TOKEN": ""},
            clear=False,
        ):
            self.assertFalse(ia.internal_api_enabled())

    def test_enabled(self) -> None:
        with patch.dict(
            os.environ,
            {"API_BASE_URL": "http://api:8000", "INTERNAL_API_TOKEN": "secret"},
            clear=False,
        ):
            self.assertTrue(ia.internal_api_enabled())


class InternalApiClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            os.environ,
            {"API_BASE_URL": "http://api.test", "INTERNAL_API_TOKEN": "tok"},
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def test_resolve_field(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/internal/fields/resolve")
            self.assertIn("Bearer tok", request.headers.get("Authorization", ""))
            return httpx.Response(
                200, json={"field_id": "f1", "land_id": "L1", "tags": ["agri:L1"]}
            )

        transport = httpx.MockTransport(handler)
        client = httpx.Client(
            base_url="http://api.test",
            transport=transport,
            headers={"Authorization": "Bearer tok"},
        )
        out = ia.resolve_field(field_id="f1", client=client)
        self.assertEqual(out["land_id"], "L1")

    def test_agri_scene_dates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/scenes/dates", request.url.path)
            self.assertEqual(request.url.params.get("sensor"), "S2")
            return httpx.Response(
                200,
                json={
                    "land_id": "L1",
                    "sensor": "S2",
                    "dates": ["2024-01-01", "2024-02-01"],
                },
            )

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        dates = ia.agri_scene_dates("L1", sensor="S2", client=client)
        self.assertEqual(dates, ["2024-01-01", "2024-02-01"])

    def test_get_job_404(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "job not found"})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        with self.assertRaises(ia.InternalApiError) as ctx:
            ia.get_job("00000000-0000-0000-0000-000000000001", client=client)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_patch_job(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "PATCH")
            import json

            payload = json.loads(request.content.decode())
            self.assertEqual(payload.get("status"), "running")
            return httpx.Response(
                200,
                json={
                    "id": "00000000-0000-0000-0000-000000000001",
                    "type": "ndvi",
                    "status": "running",
                },
            )

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.patch_job(
            "00000000-0000-0000-0000-000000000001",
            {"status": "running"},
            client=client,
        )
        self.assertEqual(out["status"], "running")


class HttpWritesTests(unittest.TestCase):
    def test_http_writes_requires_api(self) -> None:
        with patch.dict(
            os.environ,
            {"API_BASE_URL": "", "INTERNAL_API_TOKEN": "", "INGEST_PG_WRITES": "0"},
            clear=False,
        ):
            self.assertFalse(ia.http_writes_enabled())

    def test_apply_results_client(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/internal/results/apply")
            return httpx.Response(200, json={"ok": True, "stats": {"domain": {}}})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.apply_results({"kind": "weather_daily", "rows": []}, client=client)
        self.assertTrue(out["ok"])


class AssessmentBundleClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            os.environ,
            {"API_BASE_URL": "http://api.test", "INTERNAL_API_TOKEN": "tok"},
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def test_assessment_bundle(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("/assessment-bundle", request.url.path)
            return httpx.Response(
                200, json={"field": {"id": "f1"}, "indices": [], "soil": {}}
            )

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.assessment_bundle("f1", client=client)
        self.assertEqual(out["field"]["id"], "f1")

    def test_ingest_pg_reads_follows_writes(self) -> None:
        with patch.dict(
            os.environ, {"INGEST_PG_WRITES": "0", "INGEST_PG_READS": ""}, clear=False
        ):
            self.assertFalse(ia.ingest_pg_reads_allowed())
        with patch.dict(
            os.environ,
            {"INGEST_PG_WRITES": "0", "INGEST_PG_READS": "1"},
            clear=False,
        ):
            self.assertTrue(ia.ingest_pg_reads_allowed())


if __name__ == "__main__":
    unittest.main()
