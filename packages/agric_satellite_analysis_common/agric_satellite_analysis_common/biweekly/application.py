"""API-plane use cases. No HTTP router is registered by this module."""

from __future__ import annotations

from .models import Download, JobOptions, Principal, ReportError
from .narrative import validate_narrative
from .ports import ArtifactPort, Authorizer, DenyAll, SourcePort
from .snapshot import build_snapshot, validate_report_window


class BiweeklyService:
    def __init__(
        self,
        repository,
        source: SourcePort,
        artifacts: ArtifactPort,
        *,
        authorizer: Authorizer | None = None,
        offline_enabled=False,
    ):
        self.repository = repository
        self.source = source
        self.artifacts = artifacts
        self.authorizer = authorizer or DenyAll()
        self.offline_enabled = offline_enabled

    def _authorize(self, principal: Principal, tenant: str, field: str, action: str):
        if not self.offline_enabled:
            raise ReportError("FEATURE_DISABLED")
        if (
            not principal.subject
            or principal.tenant_id != tenant
            or not self.authorizer.allow(principal, tenant, field, action)
        ):
            raise ReportError("ACCESS_DENIED")

    def _owned(self, principal, job_id, action):
        if not self.offline_enabled:
            raise ReportError("FEATURE_DISABLED")
        try:
            row = self.repository.get(job_id)
        except ReportError:
            raise ReportError("ACCESS_DENIED") from None
        data = row["snapshot"].data
        self._authorize(principal, data["tenant_id"], data["field_id"], action)
        return row

    @staticmethod
    def _view(row):
        artifact = row["artifact"]
        return {
            "job_id": row["id"],
            "snapshot_id": row["snapshot"].snapshot_id,
            "snapshot_sha256": row["snapshot_sha"],
            "status": row["state"],
            "attempts": row["attempts"],
            "error_code": row["error"],
            "stale": bool(row["invalidated"]),
            "download_available": row["state"] == "ready"
            and artifact is not None
            and not row["invalidated"],
            "artifact": artifact,
            "provenance": row["options"].identity(),
        }

    def request(self, principal, field_id, season_id, start, end, options=None):
        self._authorize(principal, principal.tenant_id, field_id, "generate")
        validate_report_window(start, end)
        raw = self.source.load(principal.tenant_id, field_id, season_id, start, end)
        snapshot = build_snapshot(raw)
        data = snapshot.data
        if (
            data["tenant_id"] != principal.tenant_id
            or data["field_id"] != field_id
            or data["season_id"] != season_id
            or data["window"] != {"start": start, "end": end}
        ):
            raise ReportError("SOURCE_SCOPE_MISMATCH")
        if data["data_kind"] != "mock":
            raise ReportError("OFFLINE_MOCK_ONLY")
        return self._view(self.repository.create(snapshot, options or JobOptions()))

    def status(self, principal, job_id):
        return self._view(self._owned(principal, job_id, "read"))

    def raw_page(self, principal, job_id):
        return self._owned(principal, job_id, "read")["snapshot"].data

    def retry(self, principal, job_id):
        self._owned(principal, job_id, "retry")
        return self._view(self.repository.retry(job_id))

    def invalidate(self, principal, job_id, reason):
        self._owned(principal, job_id, "invalidate")
        self.repository.invalidate(job_id, reason)

    def download(self, principal, job_id):
        row = self._owned(principal, job_id, "read")
        if row["invalidated"]:
            raise ReportError("SNAPSHOT_STALE")
        if row["state"] != "ready" or row["artifact"] is None:
            raise ReportError("ARTIFACT_UNAVAILABLE")
        artifact = row["artifact"]
        content = self.artifacts.get(artifact["sha256"])
        if len(content) != artifact["size_bytes"]:
            raise ReportError("ARTIFACT_CORRUPT")
        return Download(f"biweekly-{row['snapshot_sha'][:16]}-MOCK.pdf", content)


class OfflineWorkGateway:
    """In-process API simulator only; production worker needs authenticated HTTP port."""

    def __init__(
        self, repository, *, artifacts=None, offline_enabled=False, lease_seconds=600
    ):
        self.repository = repository
        self.artifacts = artifacts
        self.enabled = offline_enabled
        self.lease_seconds = lease_seconds

    def _check(self):
        if not self.enabled:
            raise ReportError("FEATURE_DISABLED")

    def claim(self, job_id):
        self._check()
        return self.repository.claim(job_id, self.lease_seconds)

    def complete(self, lease, artifact, narrative):
        self._check()
        if self.artifacts is None:
            raise ReportError("ARTIFACT_STORE_REQUIRED")
        content = self.artifacts.get(artifact.sha256)
        if len(content) != artifact.size_bytes:
            raise ReportError("ARTIFACT_CORRUPT")
        # API控制面再次以权威快照校验文案；不只信任worker传来的“已验证”标记。
        row = self.repository.get(lease.job_id)
        validate_narrative(narrative.data, row["snapshot"], row["options"])
        self.repository.complete(lease, artifact, narrative)

    def fail(self, lease, error_code):
        self._check()
        self.repository.fail(lease, error_code)
