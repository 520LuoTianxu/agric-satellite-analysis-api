"""Official biweekly artifact references: bucket/key + checksum, never a signed URL.

The download machine uploads the PDF once to the configured bucket under the
existing prefix reports/default/{land_id}/biweekly-{job_id}.pdf (or .json for
evidence-limited briefs). The API stores that object reference and validates
the object with a head + magic-byte prefix, hashing only over the API-to-OSS
stream when a checksum is supplied. The HTTP complete body must not carry the
PDF bytes on the new path.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from .models import ReportError, digest

PDF_MAGIC = b"%PDF-"
MAX_PDF_BYTES = 20_000_000
MAX_BRIEF_BYTES = 2_000_000
OFFICIAL_KEY_PREFIX = "reports/default"
SAFE_LAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_JOB_ID = re.compile(r"^bj-[a-f0-9]{16}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

READY_STATUSES = frozenset({"ready", "evidence_limited"})
IN_FLIGHT_STATUSES = frozenset(
    {"queued", "running", "dispatched", "registration_pending"}
)
OFFICIAL_STATES = tuple(
    ("queued", "running", "dispatched", "registration_pending", "ready", "failed", "evidence_limited")
)


class OfficialStoreError(Exception):
    """Authoritative official-job store failed; must not be treated as 'no jobs'."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


class OfficialStoreUnavailable(OfficialStoreError):
    def __init__(self, message: str = "official job store unavailable"):
        super().__init__("OFFICIAL_STORE_UNAVAILABLE", message)


class OfficialStoreNotMigrated(OfficialStoreError):
    """The biweekly_official_jobs table has not been applied yet."""

    def __init__(self, message: str = "biweekly_official_jobs table is missing"):
        super().__init__("OFFICIAL_STORE_NOT_MIGRATED", message)


def official_object_key(land_id: str, job_id: str, *, kind: str = "official_visual_artifact") -> str:
    _require_ids(land_id, job_id)
    if kind == "evidence_limited_brief":
        return f"{OFFICIAL_KEY_PREFIX}/{land_id}/biweekly-{job_id}.json"
    if kind not in {"official_visual_artifact", "application/pdf"}:
        # Default official visual PDF; unknown kinds still cannot invent a path.
        if kind and kind not in {"official_visual_artifact"}:
            raise ReportError("ARTIFACT_KIND_INVALID")
    return f"{OFFICIAL_KEY_PREFIX}/{land_id}/biweekly-{job_id}.pdf"


def _require_ids(land_id: str, job_id: str) -> None:
    if not SAFE_LAND_ID.fullmatch(str(land_id or "")):
        raise ReportError("ARTIFACT_LAND_INVALID")
    if not SAFE_JOB_ID.fullmatch(str(job_id or "")):
        raise ReportError("ARTIFACT_JOB_INVALID")


def looks_like_url(value: str) -> bool:
    text = str(value or "").strip().lower()
    return bool(
        text.startswith(("http://", "https://", "oss://", "//"))
        or "://" in text
        or "\\" in text
    )


def parse_oss_ref(
    body: Mapping[str, Any],
    *,
    land_id: str,
    job_id: str,
    kind: str,
    expected_bucket: str,
) -> dict[str, Any] | None:
    """Return a normalized OSS object ref from a complete body, or None if absent.

    Accepts either a nested ``oss`` object or flat ``oss_bucket`` / ``oss_key``
    fields. Rejects signed URLs, arbitrary hosts, cross-bucket and off-key
    objects before any storage call.
    """
    raw = body.get("oss")
    if raw is None and body.get("oss_key"):
        raw = {
            "bucket": body.get("oss_bucket") or body.get("bucket"),
            "key": body.get("oss_key") or body.get("key"),
            "size_bytes": body.get("size_bytes"),
            "sha256": body.get("sha256") or body.get("oss_sha256"),
            "content_type": body.get("content_type") or body.get("mime"),
        }
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ReportError("ARTIFACT_REF_INVALID")
    # Never persist or follow a caller-supplied URL.
    for field in ("url", "href", "signed_url", "presigned_url", "download_url"):
        if raw.get(field):
            raise ReportError("ARTIFACT_URL_FORBIDDEN")
    bucket = str(raw.get("bucket") or "").strip()
    key = str(raw.get("key") or raw.get("oss_key") or "").strip()
    if not bucket or not key:
        raise ReportError("ARTIFACT_REF_INVALID")
    if looks_like_url(bucket) or looks_like_url(key):
        raise ReportError("ARTIFACT_URL_FORBIDDEN")
    if bucket != expected_bucket:
        raise ReportError("ARTIFACT_BUCKET_MISMATCH")
    expected_key = official_object_key(land_id, job_id, kind=kind)
    if key != expected_key:
        raise ReportError("ARTIFACT_KEY_MISMATCH")
    try:
        size_bytes = int(raw.get("size_bytes"))
    except (TypeError, ValueError) as exc:
        raise ReportError("ARTIFACT_SIZE_INVALID") from exc
    sha256 = str(raw.get("sha256") or "").strip().lower()
    if not SHA256_RE.fullmatch(sha256):
        raise ReportError("ARTIFACT_CHECKSUM_INVALID")
    content_type = str(raw.get("content_type") or raw.get("mime") or "").strip() or None
    return {
        "storage": "oss",
        "bucket": bucket,
        "key": key,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "content_type": content_type,
        "kind": kind,
    }


def inspect_official_object(
    storage: Any,
    ref: Mapping[str, Any],
    *,
    expect_pdf: bool,
) -> dict[str, Any]:
    """Validate the object in the configured bucket with a minimal-read strategy.

    Uses exists + stat (size) + the first few bytes for a magic check. The
    checksum is computed over the API-to-OSS stream only, never over an HTTP
    callback body.
    """
    bucket = ref["bucket"]
    key = ref["key"]
    if getattr(storage, "bucket")() != bucket:
        raise ReportError("ARTIFACT_BUCKET_MISMATCH")
    if not storage.exists(key):
        raise ReportError("ARTIFACT_UNAVAILABLE")
    stat = storage.stat_object(key)
    size = int(stat.get("size_bytes") or 0)
    limit = MAX_PDF_BYTES if expect_pdf else MAX_BRIEF_BYTES
    if size <= 0 or size > limit:
        raise ReportError("ARTIFACT_INVALID")
    if int(ref["size_bytes"]) != size:
        raise ReportError("ARTIFACT_SIZE_MISMATCH")
    prefix_len = 8 if expect_pdf else min(64, size)
    prefix = storage.read_prefix(key, prefix_len)
    if expect_pdf and not prefix.startswith(PDF_MAGIC):
        raise ReportError("ARTIFACT_INVALID")
    hasher = hashlib.sha256()
    seen = 0
    for chunk in storage.iter_bytes(key):
        seen += len(chunk)
        if seen > limit:
            raise ReportError("ARTIFACT_INVALID")
        hasher.update(chunk)
    digest_hex = hasher.hexdigest()
    if digest_hex != ref["sha256"]:
        raise ReportError("ARTIFACT_CORRUPT")
    if seen != size:
        raise ReportError("ARTIFACT_SIZE_MISMATCH")
    return {
        **dict(ref),
        "size_bytes": size,
        "sha256": digest_hex,
        "content_type": ref.get("content_type") or stat.get("content_type"),
    }


def same_artifact(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> bool:
    if not left or not right:
        return False
    if left.get("sha256") and right.get("sha256") and left.get("sha256") != right.get("sha256"):
        return False
    if left.get("key") and right.get("key"):
        return (
            left.get("bucket") == right.get("bucket")
            and left.get("key") == right.get("key")
            and left.get("sha256") == right.get("sha256")
        )
    if left.get("sha256") and right.get("sha256"):
        return left["sha256"] == right["sha256"] and int(left.get("size_bytes") or 0) == int(
            right.get("size_bytes") or 0
        )
    return False


def public_artifact(artifact: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Strip any accidental URL before a job DTO leaves the API."""
    if not artifact:
        return None
    out = {
        key: value
        for key, value in artifact.items()
        if key not in {"url", "href", "signed_url", "presigned_url", "download_url"}
    }
    return out


def sha256_bytes(data: bytes) -> str:
    return digest(data)
