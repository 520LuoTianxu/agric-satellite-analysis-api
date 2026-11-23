# Official biweekly: OSS object reference, DB authority, registration retry

Status: implemented in branch `fix/biweekly-oss-complete-persist` for review.
Not applied to production. This file is not a claim that historical `/tmp`
jobs have been migrated, and it is not a claim that official period 15 failed.
Period 15 (`bj-ef25e6391ad7437b`) was already official `ready`; this change
closes the upload / persist / callback / download loop so official and
business status stay consistent after restart and lost complete responses.

## Why not reuse `agric_satellite.jobs` as the official row

`jobs.id` is a UUID. Official generate ids are `bj-` + 16 hex characters.
Forcing those into `jobs.id` would break the primary key and confuse the
business report job (also a UUID, `report_job_id`). The business Job remains
the place for worker progress; official status, period association, and the
artifact live in a new table:

`agric_satellite.biweekly_official_jobs`

Columns: `job_id`, `land_id`, `season_id`, `period_id`, `kind`, `status`,
optional `report_job_id` (FK to `jobs.id`), `artifact_json` (bucket/key/
size_bytes/sha256/content-type; never a signed URL), `job_json` (full
official document), `created_at`, `updated_at`.

The business Job `progress_json` is still patched after a successful
official registration with the same OSS reference. `jobs.status` is
`varchar(20)`, so `registration_pending` is stored as `running` plus
`progress_json.stage=registration_pending`.

## Protocol

New complete body: `oss.{bucket,key,size_bytes,sha256,content_type}` (flat
fields also accepted). The PDF is not sent as `content_base64`.

The allowed object key is the existing prefix, kept as-is:

`reports/default/{land_id}/biweekly-{job_id}.pdf` (or `.json` for evidence-limited).

The bucket must be the configured storage bucket. Arbitrary URLs, presigned
links, other buckets, and other lands’ keys are rejected before any storage
call. Validation is `exists` + `stat` (size) + first bytes (`%PDF-`) + sha256
over the API-to-OSS stream. The callback body never carries the PDF on the
new path.

Legacy machines may still POST `content_base64`. The API accepts that,
writes the local artifact, and also attempts an OSS upload so a later
restart can stream the object. Do not remove the old protocol in this
change.

## Status loop

- `ready` / `evidence_limited` cannot be overwritten by a later `failed`.
- The same official artifact may be completed repeatedly (idempotent).
- A `failed` job with no registered artifact can recover if a valid OSS
  object for that `bj` / land is later completed (period 12 style).
- Download machine: render once, upload once, persist `oss_artifact` on the
  business job, then register. Complete timeouts retry registration only,
  with bounded backoff (8 attempts, 15s × 2^(n-1), cap 300s) via Celery
  retry. After the bound: official/business stay pending, work item is not
  completed, and the official job is not marked `GENERATE_FAILED`.
- Business/work completion happens only after official registration
  succeeds.

## Download

Authenticated GET `/jobs/{id}/download` streams from the DB OSS reference
through the official API (same origin). No signed URL is placed in the job
DTO or in a redirect, so frontend PR25’s same-origin authenticated fetch
does not need a UI change. Cross-origin presign would be incompatible with
that client and is not used.

## Rollout (do not apply from this box)

1. Apply `scripts/20261123_biweekly_official_jobs.sql` in a controlled
   window. API startup does not auto-migrate.
2. Deploy the API. If the table is missing, the code falls back to
   work_dir files and marks `persistence=legacy_work_dir`, `migrated=false`.
   Any other database error is `OFFICIAL_STORE_UNAVAILABLE` (HTTP 503), not
   an empty period list.
3. Deploy new download machines. Old machines can keep sending base64
   during the roll.
4. Do not delete the legacy complete path in this release.
5. Historical file jobs are not backfilled by this migration.

## Tests (local only)

See `packages/agric_satellite_analysis_common/tests/test_biweekly_official_store.py`,
`services/api/tests/test_biweekly_oss_complete.py`,
`services/api/tests/test_internal_biweekly.py`,
`services/ingest/tests/test_biweekly_registration_retry.py`.
These use fixtures and fakes; they do not talk to production OSS or the
production database, and they do not apply this SQL.
