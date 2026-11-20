#!/usr/bin/env python3
"""Explicit offline API+worker harness, NOT a deployed download-host process.

No environment/settings discovery; no external sources, model, DB server or OSS.
Without --offline it does not even read the input or create an output directory.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Explicitly run a synthetic local workflow",
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.offline:
        print(
            json.dumps(
                {
                    "status": "disabled",
                    "reason": "Explicit --offline and synthetic input required",
                }
            )
        )
        return
    if args.input is None or args.output is None:
        parser.error("--input and --output are required for --offline")
    from agric_satellite_analysis_common.biweekly.application import (
        BiweeklyService,
        OfflineWorkGateway,
    )
    from agric_satellite_analysis_common.biweekly.artifacts import LocalArtifactStore
    from agric_satellite_analysis_common.biweekly.models import Principal, ReportError
    from agric_satellite_analysis_common.biweekly.repository import (
        OfflineSqliteRepository,
    )
    from app.reports.biweekly.pdf_render import BiweeklyPdfRenderer
    from app.reports.biweekly.worker import BiweeklyWorker

    raw = json.loads(args.input.read_text(encoding="utf-8"))
    if raw.get("data_kind") != "mock":
        raise ReportError("OFFLINE_MOCK_ONLY")

    class SyntheticSource:
        def load(self, *scope):
            return copy.deepcopy(raw)

    class SyntheticGrant:
        def allow(self, principal, tenant, field, action):
            return (
                principal.subject == "offline-demo-owner"
                and tenant == raw["tenant_id"]
                and principal.tenant_id == tenant
                and field == raw["field_id"]
            )

    principal = Principal("offline-demo-owner", raw["tenant_id"])
    root = args.output.resolve()
    repository = OfflineSqliteRepository(root / "offline-api-control.sqlite")
    artifacts = LocalArtifactStore(root / "artifacts")
    api = BiweeklyService(
        repository,
        SyntheticSource(),
        artifacts,
        authorizer=SyntheticGrant(),
        offline_enabled=True,
    )
    job = api.request(
        principal,
        raw["field_id"],
        raw["season_id"],
        raw["window"]["start"],
        raw["window"]["end"],
    )
    gateway = OfflineWorkGateway(repository, artifacts=artifacts, offline_enabled=True)
    worker = BiweeklyWorker(
        gateway, artifacts, BiweeklyPdfRenderer(), offline_enabled=True
    )
    worker.run(job["job_id"])
    job = api.status(principal, job["job_id"])
    row = repository.get(job["job_id"])
    snapshot_path = root / (row["snapshot_sha"] + ".snapshot.json")
    content = row["snapshot"].canonical_json
    if snapshot_path.exists() and snapshot_path.read_text(encoding="utf-8") != content:
        raise ReportError("SNAPSHOT_CORRUPT")
    if not snapshot_path.exists():
        snapshot_path.write_text(content, encoding="utf-8")
    if job["download_available"]:
        download_path = root / "report-MOCK.pdf"
        download_path.write_bytes(api.download(principal, job["job_id"]).content)
        job["download_file"] = str(download_path)
    (root / "job-manifest.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(job, ensure_ascii=False))


if __name__ == "__main__":
    main()
