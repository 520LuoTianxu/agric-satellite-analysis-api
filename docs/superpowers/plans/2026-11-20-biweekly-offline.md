# T6 Biweekly Offline Backend Implementation Plan

**Goal:** runnable offline API-control-plane → snapshot → leased worker → validated narrative → Chinese PDF → authorized download; frontend untouched.
**Architecture:** pure shared models/snapshot/narrative/ports, explicit local SQLite API-side repository and content-addressed local artifacts, API facade default disabled/DenyAll, worker via injected gateway with no DB connection. Existing ReportLab/font reused; no provider/credential discovery.
**Tech stack:** Python3.11+, stdlib unittest/SQLite, existing ReportLab dependency.

- [x] Red tests/fixtures in `tests/biweekly_offline/`: input validation/quality/phase mapping, immutable hashes, no invented FVC/LAI, narrative rejection, tenant/grant checks, lifecycle/idempotency/lease/retry/stale handling, PDF/font/download integrity.
- [x] Implement `packages/agric_satellite_analysis_common/agric_satellite_analysis_common/biweekly/{models,snapshot,narrative,ports}.py` without service imports/settings.
- [x] Implement explicit offline `repository.py`, `artifacts.py`, `application.py`; API-side factory under `services/api/app/services/biweekly_reports.py`, no router/main registration.
- [x] Implement `services/ingest/app/reports/biweekly/{worker,pdf_render}.py` and `scripts/biweekly_offline_demo.py`; source input/output only via ports. CLI runs once on synthetic fixture and stores artifacts locally, default no execution without explicit offline flag.
- [x] Verify unit/integration suites in temp dirs, Chinese PDF text/pagination/font failure, existing pure common regression, ruff/compile/diff. Run CLI twice to prove idempotency; inspect no network/credentials/production config changes.
- [x] Write runbook/contracts/unimplemented production adapters and scientific/auth blockers; no merge/deploy or frontend change.
- Remote publication: parent must verify direct/standing owner authorization before push/PR; not performed by this executor.

Verification: 29 new unittest cases; 6 existing phenology regressions; ruff format/check and compileall. Explicit CLI twice: ready, attempts=1, two-page embedded-Chinese PDF, SHA256 verified and first-page raster inspected.
