import io
import unittest
from dataclasses import replace
from pathlib import Path
import test_lifecycle as lifecycle
from fixtures import narrative
from pypdf import PdfReader
from agric_satellite_analysis_common.biweekly.models import JobOptions, ReportError
from agric_satellite_analysis_common.biweekly.narrative import template_narrative
from app.reports.biweekly.pdf_render import BiweeklyPdfRenderer
from app.reports.biweekly.worker import BiweeklyWorker


class FakeProvider:
    model_version = "offline-fake-model-v1"
    prompt_version = "biweekly-structured-v1"
    invalid = False

    def generate(self, payload):
        result = narrative()
        if self.invalid:
            result["claims"][0]["values"][0]["value"] = 99
        return result


class WorkerTests(unittest.TestCase):
    setUp = lifecycle.LifecycleTests.setUp
    tearDown = lifecycle.LifecycleTests.tearDown
    request = lifecycle.LifecycleTests.request

    def worker(self, provider=None, renderer=None):
        self.gateway.artifacts = self.artifacts
        return BiweeklyWorker(
            self.gateway,
            self.artifacts,
            renderer or BiweeklyPdfRenderer(),
            provider=provider,
            offline_enabled=True,
        )

    def test_full_pdf_roundtrip_is_deterministic_chinese_and_same_facts(self):
        jid = self.request()["job_id"]
        worker = self.worker()
        self.assertEqual(worker.run(jid), "ready")
        first = self.api.download(self.owner, jid)
        self.assertTrue(first.content.startswith(b"%PDF-"))
        reader = PdfReader(io.BytesIO(first.content))
        text = "\n".join(p.extract_text() for p in reader.pages)
        self.assertIn("种植双周报", text)
        self.assertIn("未调用AI", text)
        self.assertIn("0.64", text)
        self.assertNotIn("0.72", text)
        self.assertIn("FVC_UNCALIBRATED", text)
        self.assertEqual(worker.run(jid), "not_claimed")
        self.assertEqual(self.api.status(self.owner, jid)["attempts"], 1)
        row = self.repo.get(jid)
        snap = row["snapshot"]
        n = template_narrative(snap, row["options"])
        self.assertEqual(BiweeklyPdfRenderer().render(snap, n).content, first.content)
        self.assertEqual(row["artifact"]["snapshot_sha256"], snap.sha256)
        self.assertEqual(
            self.api.raw_page(self.owner, jid)["facts"]["rs.ndvi"]["value"], 0.64
        )
        self.api.invalidate(self.owner, jid, "SOURCE_CHANGED")
        with self.assertRaises(ReportError):
            self.api.download(self.owner, jid)

    def test_bad_ai_output_fails_then_explicit_retry_succeeds(self):
        opts = JobOptions(
            narrative_mode="injected", model_version="offline-fake-model-v1"
        )
        jid = self.request(opts)["job_id"]
        provider = FakeProvider()
        provider.invalid = True
        worker = self.worker(provider)
        self.assertEqual(worker.run(jid), "failed")
        self.assertEqual(
            self.api.status(self.owner, jid)["error_code"], "NARRATIVE_INVALID"
        )
        self.api.retry(self.owner, jid)
        provider.invalid = False
        self.assertEqual(worker.run(jid), "ready")
        self.assertEqual(self.api.status(self.owner, jid)["attempts"], 2)

    def test_font_missing_and_provider_missing_are_explicit_failures(self):
        jid = self.request()["job_id"]
        self.assertEqual(
            self.worker(
                renderer=BiweeklyPdfRenderer(font_path=self.root / "missing.ttf")
            ).run(jid),
            "failed",
        )
        self.assertEqual(
            self.api.status(self.owner, jid)["error_code"], "FONT_UNAVAILABLE"
        )
        self.api.retry(self.owner, jid)
        self.assertEqual(self.worker().run(jid), "ready")
        jid2 = self.request(
            JobOptions(
                narrative_mode="injected", model_version="approved-but-not-configured"
            )
        )["job_id"]
        self.assertEqual(self.worker().run(jid2), "failed")
        self.assertEqual(
            self.api.status(self.owner, jid2)["error_code"], "PROVIDER_UNAVAILABLE"
        )

    def test_corrupt_artifact_and_traversal_are_refused(self):
        jid = self.request()["job_id"]
        self.worker().run(jid)
        sha = self.api.status(self.owner, jid)["artifact"]["sha256"]
        (self.artifacts.root / (sha + ".pdf")).write_bytes(b"corrupt")
        with self.assertRaises(ReportError):
            self.api.download(self.owner, jid)
        for key in ["../../etc/passwd", "x" * 64, "https://unsafe.invalid/a.pdf"]:
            with self.assertRaises(ReportError):
                self.artifacts.get(key)

    def test_missing_evidence_still_renders_with_limits_not_fake_zero(self):
        self.source.value["evidence"] = []
        self.source.value["stages"] = []
        jid = self.request()["job_id"]
        self.assertEqual(self.worker().run(jid), "ready")
        text = "".join(
            p.extract_text()
            for p in PdfReader(
                io.BytesIO(self.api.download(self.owner, jid).content)
            ).pages
        )
        self.assertIn("STAGE_PLAN_MISSING", text)
        self.assertIn("MISSING_NDVI", text)

    def test_download_worker_has_no_db_or_environment_discovery(self):
        root = Path(__file__).resolve().parents[2]
        code = (root / "services/ingest/app/reports/biweekly/worker.py").read_text()
        for forbidden in [
            "sqlalchemy",
            "sqlite",
            "psycopg",
            "get_settings",
            "os.environ",
            "requests",
            "httpx",
            "celery",
        ]:
            self.assertNotIn(forbidden, code)

    def test_completion_cannot_change_snapshot_or_template_provenance(self):
        jid = self.request()["job_id"]
        self.worker().run(jid)
        row = self.repo.get(jid)
        other = self.request(JobOptions(template_version="new-template"))["job_id"]
        lease = self.gateway.claim(other)
        from agric_satellite_analysis_common.biweekly.models import Artifact

        forged = Artifact(**row["artifact"])
        forged = replace(forged, job_id=other)
        with self.assertRaises(ReportError):
            self.gateway.complete(
                lease, forged, template_narrative(lease.snapshot, lease.options)
            )

    def test_disabled_worker_and_no_network_on_explicit_offline_run(self):
        from unittest.mock import patch

        jid = self.request()["job_id"]
        self.assertEqual(
            BiweeklyWorker(self.gateway, self.artifacts, BiweeklyPdfRenderer()).run(
                jid
            ),
            "disabled",
        )
        self.assertEqual(self.api.status(self.owner, jid)["attempts"], 0)
        with patch(
            "socket.socket.connect", side_effect=AssertionError("network forbidden")
        ):
            self.assertEqual(self.worker().run(jid), "ready")

    def test_renderer_template_version_is_pinned_not_merely_relabelled(self):
        jid = self.request(JobOptions(template_version="not-this-renderer"))["job_id"]
        self.assertEqual(self.worker().run(jid), "failed")
        self.assertEqual(
            self.api.status(self.owner, jid)["error_code"], "PROVENANCE_MISMATCH"
        )

    def test_failed_artifact_write_does_not_publish_a_final_pdf(self):
        from unittest.mock import patch

        with patch("os.fsync", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.artifacts.put(b"%PDF-1.4\nsynthetic only")
        self.assertEqual(list(self.artifacts.root.glob("*.pdf")), [])
