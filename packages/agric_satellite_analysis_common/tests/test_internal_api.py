"""Unit tests for agric_satellite_analysis_common.internal_api (httpx MockTransport)."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

import httpx
from agric_satellite_analysis_common import internal_api as ia


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



class ApiPathPrefixTests(unittest.TestCase):
    def test_default_prefix_is_v1(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("API_PATH_PREFIX", None)
            self.assertEqual(ia.api_path_prefix(), "/v1")
            self.assertEqual(ia.internal_api_path("/internal/work/claim"), "/v1/internal/work/claim")

    def test_empty_prefix_for_gateway(self) -> None:
        with patch.dict(os.environ, {"API_PATH_PREFIX": ""}, clear=False):
            self.assertEqual(ia.api_path_prefix(), "")
            self.assertEqual(ia.internal_api_path("/internal/work/claim"), "/internal/work/claim")

    def test_dash_means_empty(self) -> None:
        with patch.dict(os.environ, {"API_PATH_PREFIX": "-"}, clear=False):
            self.assertEqual(ia.api_path_prefix(), "")

    def test_custom_prefix(self) -> None:
        with patch.dict(os.environ, {"API_PATH_PREFIX": "v2"}, clear=False):
            self.assertEqual(ia.api_path_prefix(), "/v2")
            self.assertEqual(ia.internal_api_path("/internal/x"), "/v2/internal/x")


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

    def test_daily_satellite_discovery_passes_fixed_day(self):
        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/internal/schedule/daily-satellite")
            self.assertEqual(request.url.params["as_of"], "2026-09-16")
            return httpx.Response(200, json={"run_id": "run-1"})

        with httpx.Client(
            base_url="http://api.test", transport=httpx.MockTransport(handler)
        ) as client:
            self.assertEqual(
                ia.daily_satellite_prepare(as_of="2026-09-16", client=client)["run_id"],
                "run-1",
            )

    def test_daily_satellite_finalization_http_failure_is_visible(self):
        def handler(request):
            self.assertEqual(
                request.url.path, "/v1/internal/schedule/daily-satellite/run-1/finalize"
            )
            return httpx.Response(503, json={"detail": "database unavailable"})

        with httpx.Client(
            base_url="http://api.test", transport=httpx.MockTransport(handler)
        ) as client:
            with patch.object(ia, "http_retries", return_value=0):
                with self.assertRaises(ia.InternalApiError):
                    ia.daily_satellite_finalize("run-1", client=client)


    def test_resolve_land_empty_path_prefix(self) -> None:
        with patch.dict(os.environ, {"API_PATH_PREFIX": ""}, clear=False):
            def handler(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/internal/lands/resolve")
                return httpx.Response(200, json={"land_id": "L1"})

            with httpx.Client(
                base_url="http://api.test", transport=httpx.MockTransport(handler)
            ) as client:
                out = ia.resolve_land(land_id="L1", client=client)
                self.assertEqual(out["land_id"], "L1")

    def test_resolve_land(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/internal/lands/resolve")
            self.assertIn("Bearer tok", request.headers.get("Authorization", ""))
            return httpx.Response(200, json={"land_id": "L1", "tile_id": "tile-1"})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(
            base_url="http://api.test",
            transport=transport,
            headers={"Authorization": "Bearer tok"},
        )
        out = ia.resolve_land(land_id="L1", client=client)
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

    def test_weekly_index_prepare(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/internal/schedule/weekly-index")
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "land_id": "L1",
                            "job_id": "00000000-0000-0000-0000-000000000001",
                            "task_name": "app.tasks.ndvi.process_ndvi",
                            "countdown": 0,
                        }
                    ],
                    "lands_checked": 1,
                    "jobs_created": 1,
                },
            )

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.weekly_index_prepare(client=client)
        self.assertEqual(out["jobs_created"], 1)
        self.assertEqual(out["items"][0]["land_id"], "L1")

    def test_weather_land_ids(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/internal/schedule/weather-lands")
            return httpx.Response(
                200, json={"land_ids": ["L1", "L2"], "batch_size": 50}
            )

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.weather_land_ids(client=client)
        self.assertEqual(out["land_ids"], ["L1", "L2"])

    def test_refresh_overview_stats(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/internal/schedule/overview-refresh")
            return httpx.Response(200, json={"ok": True, "regions": 2})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.refresh_overview_stats(window_days=60, client=client)
        self.assertTrue(out["ok"])

    def test_ensure_admin_task_run(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/internal/admin/task-runs/ensure")
            payload = json.loads(request.content.decode())
            self.assertEqual(payload["task_key"], "overview-refresh")
            self.assertEqual(payload["execution_key"], "2026-09-19:60:")
            self.assertEqual(payload["params"]["window_days"], 60)
            return httpx.Response(
                200,
                json={
                    "run_id": "00000000-0000-0000-0000-000000000001",
                    "task_key": "overview-refresh",
                    "status": "queued",
                },
            )

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.ensure_admin_task_run(
            "overview-refresh",
            "app.tasks.overview_preagg.refresh_overview_stats",
            "2026-09-19:60:",
            params={"window_days": 60},
            client=client,
        )
        self.assertEqual(out["status"], "queued")

    def test_update_admin_task_run_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                request.url.path,
                "/v1/internal/admin/task-runs/00000000-0000-0000-0000-000000000001/status",
            )
            payload = json.loads(request.content.decode())
            self.assertEqual(payload["status"], "success")
            self.assertEqual(payload["celery_task_id"], "celery-1")
            self.assertEqual(payload["result"]["batches"], 3)
            return httpx.Response(200, json={"ok": True, "status": "success"})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.update_admin_task_run_status(
            "00000000-0000-0000-0000-000000000001",
            "success",
            celery_task_id="celery-1",
            result={"batches": 3},
            client=client,
        )
        self.assertTrue(out["ok"])


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

    def test_apply_results_sanitizes_non_finite_floats(self) -> None:
        received: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received.update(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.apply_results(
            {
                "kind": "soil_profile",
                "values": [float("nan"), float("inf"), float("-inf")],
                "nested": {"missing": float("nan"), "valid": 1.25},
            },
            client=client,
        )

        self.assertTrue(out["ok"])
        self.assertEqual(
            received["result"],
            {
                "kind": "soil_profile",
                "values": [None, None, None],
                "nested": {"missing": None, "valid": 1.25},
            },
        )

    def test_cache_results_client(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/internal/results/cache")
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result_id": "scene-cache-1",
                    "queued": True,
                    "ttl_seconds": 86400,
                },
            )

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        out = ia.cache_results(
            {"status": "success", "oss_urls": {"2026-09-16_S2": "https://oss.test/a"}},
            client=client,
        )
        self.assertTrue(out["queued"])
        self.assertEqual(out["ttl_seconds"], 86400)


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


class TraceHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        from agric_satellite_analysis_common.trace import bind_trace_id, clear_trace_id

        self._clear = clear_trace_id
        self.env = patch.dict(
            os.environ,
            {"API_BASE_URL": "http://api.test", "INTERNAL_API_TOKEN": "tok"},
            clear=False,
        )
        self.env.start()
        bind_trace_id("trace-http")

    def tearDown(self) -> None:
        self._clear()
        self.env.stop()

    def test_headers_include_trace_id(self) -> None:
        from agric_satellite_analysis_common.trace import TRACE_HEADER

        headers = ia._headers()
        self.assertEqual(headers[TRACE_HEADER], "trace-http")
        self.assertIn("Bearer tok", headers["Authorization"])


if __name__ == "__main__":
    unittest.main()


class HttpTimeoutConfigTests(unittest.TestCase):
    def test_connect_timeout_defaults_shorter_than_total(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INTERNAL_API_HTTP_TIMEOUT_SEC", None)
            os.environ.pop("INTERNAL_API_HTTP_CONNECT_TIMEOUT_SEC", None)
            t = ia.http_timeout()
        self.assertEqual(t.read, 30.0)
        self.assertEqual(t.connect, 5.0)
        self.assertEqual(t.pool, 5.0)

    def test_connect_timeout_clamped_to_total(self) -> None:
        with patch.dict(
            os.environ,
            {
                "INTERNAL_API_HTTP_TIMEOUT_SEC": "8",
                "INTERNAL_API_HTTP_CONNECT_TIMEOUT_SEC": "30",
            },
        ):
            t = ia.http_timeout()
        self.assertEqual(t.read, 8.0)
        self.assertEqual(t.connect, 8.0)


class RequestWithRetryTests(unittest.TestCase):
    def test_retries_connect_error_then_succeeds(self) -> None:
        from unittest.mock import MagicMock

        client = MagicMock()
        ok = MagicMock()
        ok.status_code = 200
        client.get.side_effect = [
            httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF"),
            httpx.ConnectTimeout("handshake timed out"),
            ok,
        ]
        sleeps: list[float] = []
        out = ia.request_with_retry(
            client,
            "GET",
            "/v1/internal/jobs/x",
            retries=3,
            backoff=0.5,
            sleep=sleeps.append,
        )
        self.assertIs(out, ok)
        self.assertEqual(client.get.call_count, 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_retries_503_then_succeeds(self) -> None:
        from unittest.mock import MagicMock

        client = MagicMock()
        bad = MagicMock(status_code=503)
        ok = MagicMock(status_code=200)
        client.post.side_effect = [bad, ok]
        sleeps: list[float] = []
        out = ia.request_with_retry(
            client,
            "POST",
            "/v1/internal/results/cache",
            retries=2,
            backoff=0.25,
            sleep=sleeps.append,
        )
        self.assertEqual(out.status_code, 200)
        self.assertEqual(sleeps, [0.25])

    def test_non_transient_does_not_retry(self) -> None:
        from unittest.mock import MagicMock

        client = MagicMock()
        client.get.side_effect = ValueError("boom")
        with self.assertRaises(ValueError):
            ia.request_with_retry(
                client, "GET", "/x", retries=5, backoff=0.1, sleep=lambda _s: None
            )
        self.assertEqual(client.get.call_count, 1)

    def test_is_transient_http_error_ssl_markers(self) -> None:
        self.assertTrue(ia.is_transient_http_error(httpx.ConnectError("SSL EOF")))
        self.assertTrue(
            ia.is_transient_http_error(
                httpx.ConnectError(
                    "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1011)"
                )
            )
        )
        self.assertTrue(
            ia.is_transient_http_error(httpx.RemoteProtocolError("Server disconnected"))
        )
        self.assertFalse(ia.is_transient_http_error(RuntimeError("business boom")))

    def test_get_job_retries_transport_error(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError(
                    "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF", request=request
                )
            return httpx.Response(200, json={"id": "job-1", "status": "queued"})

        transport = httpx.MockTransport(handler)
        client = httpx.Client(base_url="http://api.test", transport=transport)
        with patch.object(ia, "http_retries", return_value=3), patch.object(
            ia, "http_retry_backoff_sec", return_value=0.01
        ), patch.object(ia.time, "sleep", return_value=None):
            out = ia.get_job("job-1", client=client)
        self.assertEqual(out["id"], "job-1")
        self.assertEqual(calls["n"], 3)


class CachedClientReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            os.environ,
            {"API_BASE_URL": "http://api.test", "INTERNAL_API_TOKEN": "tok"},
            clear=False,
        )
        self.env.start()
        ia.close_cached_result_client()

    def tearDown(self) -> None:
        ia.close_cached_result_client()
        self.env.stop()

    def test_patch_job_reuses_cached_client(self) -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(200, json={"id": "job-1", "status": "running"})

        transport = httpx.MockTransport(handler)
        pooled = httpx.Client(base_url="http://api.test", transport=transport)
        with patch.object(ia, "_cached_result_client", return_value=pooled) as cached:
            out1 = ia.patch_job("job-1", {"status": "running"}, include_progress=False)
            out2 = ia.patch_job(
                "job-1",
                {"progress_json": {"scenes_done": 1}, "merge_progress": True},
                include_progress=False,
            )
            out3 = ia.get_job("job-1")
        self.assertEqual(out1["status"], "running")
        self.assertEqual(out2["status"], "running")
        self.assertEqual(out3["id"], "job-1")
        self.assertEqual(cached.call_count, 3)
        self.assertEqual(methods, ["PATCH", "PATCH", "GET"])

    def test_cached_client_rebuilds_after_transient(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectTimeout("handshake timed out")
            return httpx.Response(200, json={"id": "job-1", "status": "queued"})

        clients = []

        def make_client(*, timeout=30.0):
            c = httpx.Client(
                base_url="http://api.test",
                transport=httpx.MockTransport(handler),
            )
            clients.append(c)
            return c

        with patch.object(ia, "_cached_result_client", side_effect=make_client), patch.object(
            ia, "http_retries", return_value=0
        ), patch.object(ia, "close_cached_result_client") as closer:
            out = ia.get_job("job-1")
        self.assertEqual(out["id"], "job-1")
        self.assertEqual(len(clients), 2)
        closer.assert_called()
        for c in clients:
            c.close()
