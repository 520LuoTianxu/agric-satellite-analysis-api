import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agric_satellite_analysis_common.biweekly.catalog import LocalPackCatalog
from agric_satellite_analysis_common.biweekly.local_api import serve
from agric_satellite_analysis_common.biweekly.trigger import TriggerService


class LocalApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = os.environ.copy()
        os.environ["BIWEEKLY_LOCAL_API"] = "1"
        os.environ["BIWEEKLY_DEV_TOKEN"] = "local-test-token-6417"
        def _stub_pdf(facts, dest):
            Path(dest).write_bytes(b"%PDF-1.4\n" + b"official-test-pdf\n" * 80 + b"%%EOF\n")

        service = TriggerService(
            Path(self.tmp.name),
            LocalPackCatalog(),
            pdf_renderer=_stub_pdf,
        )
        self.httpd = serve("127.0.0.1", 0, service)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()
        for key in ("BIWEEKLY_LOCAL_API", "BIWEEKLY_DEV_TOKEN"):
            if key in self.env:
                os.environ[key] = self.env[key]
            else:
                os.environ.pop(key, None)

    def _open(self, path, method="GET", body=None, token="local-test-token-6417"):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Biweekly-Dev-Token"] = token
        req = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health_and_auth_and_issue12_flow(self):
        status, payload = self._open("/health", token=None)
        self.assertEqual(status, 200)
        self.assertFalse(payload["production_ready"])
        denied, body = self._open("/v1/lands/6417/biweekly/seasons", token="wrong")
        self.assertEqual(denied, 403)
        ok, seasons = self._open("/v1/lands/6417/biweekly/seasons")
        self.assertEqual(ok, 200)
        self.assertEqual(seasons["seasons"][0]["season_id"], "2025/26")
        ok, periods = self._open("/v1/lands/6417/biweekly/periods?season=2025/26")
        self.assertEqual(len(periods["periods"]), 18)
        self.assertEqual(periods["periods"][11]["start"], "2026-04-04")
        st, job = self._open(
            "/v1/lands/6417/biweekly/periods/12/generate?season=2025/26",
            method="POST",
            body={"reference_planting_instance_id": "15933"},
        )
        self.assertEqual(st, 200)
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["result"]["phase"]["display_phase"], "S2")
        st, fetched = self._open(f"/v1/lands/6417/biweekly/jobs/{job['job_id']}")
        self.assertEqual(fetched["job_id"], job["job_id"])
