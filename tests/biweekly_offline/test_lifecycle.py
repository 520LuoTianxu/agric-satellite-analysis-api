import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fixtures import source
from agric_satellite_analysis_common.biweekly.application import (
    BiweeklyService,
    OfflineWorkGateway,
)
from agric_satellite_analysis_common.biweekly.artifacts import LocalArtifactStore
from agric_satellite_analysis_common.biweekly.models import (
    JobOptions,
    Principal,
    ReportError,
)
from agric_satellite_analysis_common.biweekly.repository import OfflineSqliteRepository


class FakeSource:
    def __init__(self):
        self.value = source()
        self.calls = 0

    def load(self, *args):
        self.calls += 1
        return copy.deepcopy(self.value)


class Grants:
    allowed = True

    def allow(self, p, tenant, field, action):
        return (
            self.allowed
            and p.subject == "mock-owner"
            and p.tenant_id == tenant == "mock-tenant-a"
            and field == "MOCK-LAND-001"
        )


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = [1000.0]
        self.repo = OfflineSqliteRepository(
            self.root / "control.sqlite", clock=lambda: self.clock[0]
        )
        self.artifacts = LocalArtifactStore(self.root / "artifacts")
        self.source = FakeSource()
        self.grants = Grants()
        self.owner = Principal("mock-owner", "mock-tenant-a")
        self.api = BiweeklyService(
            self.repo,
            self.source,
            self.artifacts,
            authorizer=self.grants,
            offline_enabled=True,
        )
        self.gateway = OfflineWorkGateway(
            self.repo, offline_enabled=True, lease_seconds=10
        )

    def tearDown(self):
        self.temp.cleanup()

    def request(self, options=None):
        return self.api.request(
            self.owner,
            "MOCK-LAND-001",
            "mock-wheat-2027",
            "2027-03-15",
            "2027-03-29",
            options or JobOptions(),
        )

    def test_disabled_and_deny_by_default_before_loading_sources(self):
        with self.assertRaises(ReportError):
            BiweeklyService(self.repo, self.source, self.artifacts).request(
                self.owner, "MOCK-LAND-001", "s", "a", "b"
            )
        with self.assertRaises(ReportError):
            BiweeklyService(
                self.repo, self.source, self.artifacts, offline_enabled=True
            ).request(self.owner, "MOCK-LAND-001", "s", "a", "b")
        self.assertEqual(self.source.calls, 0)

    def test_concurrent_idempotency_and_versioned_jobs(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs = list(pool.map(lambda _: self.request(), range(10)))
        self.assertEqual(len({j["job_id"] for j in jobs}), 1)
        self.assertEqual(
            self.request(JobOptions(template_version="new-reviewed-template"))[
                "attempts"
            ],
            0,
        )
        self.source.value["source_revision"] = "synthetic-v2"
        self.assertNotEqual(self.request()["job_id"], jobs[0]["job_id"])

    def test_lease_exclusivity_expiry_retry_and_stale_fencing(self):
        jid = self.request()["job_id"]
        first = self.gateway.claim(jid)
        self.assertIsNotNone(first)
        self.assertIsNone(self.gateway.claim(jid))
        self.clock[0] = 1011
        self.api.retry(self.owner, jid)
        second = self.gateway.claim(jid)
        self.assertNotEqual(first.token, second.token)
        with self.assertRaises(ReportError):
            self.gateway.fail(first, "WORKER_FAILED")
        self.gateway.fail(second, "WORKER_FAILED")
        self.api.retry(self.owner, jid)
        last = self.gateway.claim(jid)
        self.gateway.fail(last, "WORKER_FAILED")
        with self.assertRaises(ReportError):
            self.api.retry(self.owner, jid)
        self.assertEqual(self.api.status(self.owner, jid)["attempts"], 3)

    def test_tenant_grants_are_rechecked_for_status_retry_snapshot_and_download(self):
        jid = self.request()["job_id"]
        self.grants.allowed = False
        for operation in [
            lambda: self.api.status(self.owner, jid),
            lambda: self.api.retry(self.owner, jid),
            lambda: self.api.raw_page(self.owner, jid),
            lambda: self.api.download(self.owner, jid),
        ]:
            with self.assertRaises(ReportError):
                operation()
        self.grants.allowed = True
        with self.assertRaises(ReportError):
            self.api.status(Principal("mock-owner", "other-tenant"), jid)

    def test_invalidation_blocks_claim_retry_and_download_without_deleting_snapshot(
        self,
    ):
        jid = self.request()["job_id"]
        self.api.invalidate(self.owner, jid, "SOURCE_CHANGED")
        self.assertTrue(self.api.status(self.owner, jid)["stale"])
        self.assertIsNone(self.gateway.claim(jid))
        with self.assertRaises(ReportError):
            self.api.retry(self.owner, jid)
        with self.assertRaises(ReportError):
            self.api.download(self.owner, jid)
        self.assertIn("facts", self.api.raw_page(self.owner, jid))

    def test_source_scope_mismatch_or_real_data_is_refused(self):
        for key, value in [
            ("field_id", "other"),
            ("tenant_id", "other"),
            ("data_kind", "real"),
        ]:
            self.source.value = source()
            self.source.value[key] = value
            with self.assertRaises(ReportError):
                self.request()

    def test_invalid_request_window_is_rejected_before_source_adapter(self):
        for start, end in [("not-a-date", "2027-03-29"), ("2027-03-15", "2027-03-30")]:
            with self.subTest(start=start, end=end), self.assertRaises(ReportError):
                self.api.request(
                    self.owner, "MOCK-LAND-001", "mock-wheat-2027", start, end
                )
        self.assertEqual(self.source.calls, 0)
