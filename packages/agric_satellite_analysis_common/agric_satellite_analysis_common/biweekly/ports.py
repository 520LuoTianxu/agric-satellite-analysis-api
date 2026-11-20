"""Injected boundaries. Production adapters intentionally NOT wired."""

from __future__ import annotations

from typing import Protocol
from .models import Artifact, Lease, Narrative, Principal, RenderedPdf, Snapshot


class SourcePort(Protocol):
    def load(
        self, tenant_id: str, field_id: str, season_id: str, start: str, end: str
    ) -> dict: ...


class Authorizer(Protocol):
    def allow(
        self, principal: Principal, tenant_id: str, field_id: str, action: str
    ) -> bool: ...


class DenyAll:
    def allow(self, principal, tenant_id, field_id, action):
        return False


class NarrativeProvider(Protocol):
    model_version: str
    prompt_version: str

    def generate(self, compact_input: dict) -> dict: ...


class PdfRenderer(Protocol):
    template_version: str

    def render(self, snapshot: Snapshot, narrative: Narrative) -> RenderedPdf: ...


class ArtifactPort(Protocol):
    def put(self, content: bytes) -> str: ...
    def get(self, sha256: str) -> bytes: ...


class WorkGateway(Protocol):
    # 真实下载机未来只能通过鉴权Internal HTTP实现；禁止注入API的Postgres session。
    def claim(self, job_id: str) -> Lease | None: ...
    def complete(
        self, lease: Lease, artifact: Artifact, narrative: Narrative
    ) -> None: ...
    def fail(self, lease: Lease, error_code: str) -> None: ...
