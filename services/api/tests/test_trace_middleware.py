"""TraceIdMiddleware binds incoming X-Trace-Id and echoes it on the response."""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from agric_satellite_analysis_common.trace import current_trace_id

from app.middleware.trace import TraceIdMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)

    @app.get("/ping")
    def ping() -> dict[str, str | None]:
        return {"trace_id": current_trace_id()}

    return app


class TraceMiddlewareTests(unittest.TestCase):
    def test_reuses_incoming_header(self) -> None:
        client = TestClient(_app())
        response = client.get("/ping", headers={"X-Trace-Id": "incoming-trace-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trace_id"], "incoming-trace-1")
        self.assertEqual(response.headers.get("X-Trace-Id"), "incoming-trace-1")

    def test_generates_when_missing(self) -> None:
        client = TestClient(_app())
        response = client.get("/ping")
        self.assertEqual(response.status_code, 200)
        generated = response.json()["trace_id"]
        self.assertTrue(generated)
        self.assertEqual(response.headers.get("X-Trace-Id"), generated)

    def test_rejects_unsafe_incoming_value(self) -> None:
        client = TestClient(_app())
        response = client.get("/ping", headers={"X-Trace-Id": "not a valid id"})
        self.assertEqual(response.status_code, 200)
        generated = response.json()["trace_id"]
        self.assertNotEqual(generated, "not a valid id")
        self.assertEqual(response.headers.get("X-Trace-Id"), generated)


if __name__ == "__main__":
    unittest.main()
