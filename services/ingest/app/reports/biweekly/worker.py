"""One explicitly invoked computation using frozen input and injected ports only."""

from __future__ import annotations

from agric_satellite_analysis_common.biweekly.models import (
    Artifact,
    ReportError,
    digest,
)
from agric_satellite_analysis_common.biweekly.narrative import (
    llm_payload,
    template_narrative,
    validate_narrative,
)
from agric_satellite_analysis_common.biweekly.ports import (
    ArtifactPort,
    NarrativeProvider,
    PdfRenderer,
    WorkGateway,
)


class BiweeklyWorker:
    def __init__(
        self,
        gateway: WorkGateway,
        artifacts: ArtifactPort,
        renderer: PdfRenderer,
        *,
        provider: NarrativeProvider | None = None,
        offline_enabled=False,
    ):
        self.gateway = gateway
        self.artifacts = artifacts
        self.renderer = renderer
        self.provider = provider
        self.enabled = offline_enabled

    def run(self, job_id: str) -> str:
        if not self.enabled:
            return "disabled"
        lease = self.gateway.claim(job_id)
        if lease is None:
            return "not_claimed"
        try:
            if lease.snapshot.data["data_kind"] != "mock":
                raise ReportError("OFFLINE_MOCK_ONLY")
            options = lease.options
            if self.renderer.template_version != options.template_version:
                raise ReportError("PROVENANCE_MISMATCH")
            if options.narrative_mode == "template":
                narrative = template_narrative(lease.snapshot, options)
            else:
                if self.provider is None:
                    raise ReportError("PROVIDER_UNAVAILABLE")
                if (
                    self.provider.model_version != options.model_version
                    or self.provider.prompt_version != options.prompt_version
                ):
                    raise ReportError("PROVENANCE_MISMATCH")
                narrative = validate_narrative(
                    self.provider.generate(llm_payload(lease.snapshot)),
                    lease.snapshot,
                    options,
                )
            rendered = self.renderer.render(lease.snapshot, narrative)
            sha = self.artifacts.put(rendered.content)
            artifact = Artifact(
                job_id,
                lease.snapshot.sha256,
                sha,
                len(rendered.content),
                rendered.pages,
                options.template_version,
                options.model_version,
                options.prompt_version,
                digest(narrative.canonical_json),
            )
            self.gateway.complete(lease, artifact, narrative)
            return "ready"
        except Exception as error:
            code = error.code if isinstance(error, ReportError) else "WORKER_FAILED"
            # 不将模型原始响应、源异常或可能含敏感信息的错误正文回传给用户。
            try:
                self.gateway.fail(lease, code)
            except ReportError as fenced:
                if fenced.code in {"LEASE_STALE", "SNAPSHOT_STALE"}:
                    return "superseded"
                raise
            return "failed"
