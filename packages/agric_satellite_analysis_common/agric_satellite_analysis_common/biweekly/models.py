"""Immutable values and stable public failure codes; no settings or network imports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class ReportError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: str | bytes) -> str:
    return hashlib.sha256(
        value.encode() if isinstance(value, str) else value
    ).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    canonical_json: str
    sha256: str

    def __post_init__(self):
        if digest(self.canonical_json) != self.sha256:
            raise ReportError("SNAPSHOT_CORRUPT")

    @property
    def data(self) -> dict:
        # 不向调用方暴露可变的共享字典，历史快照只能新增版本，不能原位修改。
        return json.loads(self.canonical_json)

    @property
    def snapshot_id(self) -> str:
        return "bwr-" + self.sha256


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str


@dataclass(frozen=True)
class JobOptions:
    template_version: str = "biweekly-pdf-offline-v1"
    prompt_version: str = "biweekly-structured-v1"
    model_version: str = "none"
    narrative_mode: str = "template"
    language: str = "zh-CN"
    max_attempts: int = 3

    def __post_init__(self):
        if self.language != "zh-CN" or self.narrative_mode not in {
            "template",
            "injected",
        }:
            raise ReportError("OPTIONS_UNSUPPORTED")
        if self.narrative_mode == "template" and self.model_version != "none":
            raise ReportError("MODEL_MODE_MISMATCH")
        if self.narrative_mode == "injected" and self.model_version == "none":
            raise ReportError("MODEL_VERSION_REQUIRED")
        if not 1 <= self.max_attempts <= 5:
            raise ReportError("ATTEMPT_LIMIT_INVALID")
        for text in (self.template_version, self.prompt_version, self.model_version):
            if not isinstance(text, str) or not text or len(text) > 128:
                raise ReportError("VERSION_REQUIRED")

    def identity(self) -> dict:
        return {
            k: getattr(self, k)
            for k in (
                "template_version",
                "prompt_version",
                "model_version",
                "narrative_mode",
                "language",
            )
        }


@dataclass(frozen=True)
class Lease:
    job_id: str
    token: str
    snapshot: Snapshot
    options: JobOptions


@dataclass(frozen=True)
class Narrative:
    canonical_json: str
    mode: str
    model_version: str
    prompt_version: str

    @property
    def data(self):
        return json.loads(self.canonical_json)


@dataclass(frozen=True)
class Artifact:
    job_id: str
    snapshot_sha256: str
    sha256: str
    size_bytes: int
    pages: int
    template_version: str
    model_version: str
    prompt_version: str
    narrative_sha256: str
    mime: str = "application/pdf"


@dataclass(frozen=True)
class RenderedPdf:
    content: bytes
    pages: int


@dataclass(frozen=True)
class Download:
    filename: str
    content: bytes
    mime: str = "application/pdf"
