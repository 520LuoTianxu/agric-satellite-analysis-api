-- Official biweekly generate jobs (bj-*) and their OSS artifact references.
-- Forward-only. This file is NOT applied at API startup. Apply manually in a
-- controlled window before (recommended) or with the API release; the new
-- code falls back to the legacy work_dir JSON only when this table is absent
-- and treats any other database error as a failure, not as "no jobs".
-- Do not claim historical file-backed jobs have been migrated; they remain
-- explicit legacy rows until an operator backfills them.

BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.biweekly_official_jobs (
    job_id text PRIMARY KEY,
    land_id text NOT NULL,
    season_id text NOT NULL,
    period_id text NOT NULL,
    kind text NOT NULL DEFAULT 'generate',
    status text NOT NULL,
    report_job_id uuid NULL
        REFERENCES agric_satellite.jobs(id) ON DELETE SET NULL,
    artifact_json jsonb,
    job_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT biweekly_official_jobs_job_id_ck
        CHECK (job_id ~ '^bj-[a-f0-9]{16}$'),
    CONSTRAINT biweekly_official_jobs_status_ck
        CHECK (status IN (
            'queued',
            'running',
            'dispatched',
            'registration_pending',
            'ready',
            'failed',
            'evidence_limited'
        )),
    CONSTRAINT biweekly_official_jobs_kind_ck
        CHECK (kind IN ('generate', 'backfill', 'backfill_season')),
    CONSTRAINT biweekly_official_jobs_artifact_no_url_ck
        CHECK (
            artifact_json IS NULL
            OR (
                jsonb_typeof(artifact_json) = 'object'
                AND NOT (artifact_json ? 'url')
                AND NOT (artifact_json ? 'signed_url')
                AND NOT (artifact_json ? 'presigned_url')
                AND NOT (artifact_json ? 'download_url')
                AND NOT (artifact_json ? 'href')
            )
        )
);

CREATE INDEX IF NOT EXISTS ix_biweekly_official_jobs_period
    ON agric_satellite.biweekly_official_jobs (land_id, season_id, period_id, created_at);

CREATE INDEX IF NOT EXISTS ix_biweekly_official_jobs_report_job
    ON agric_satellite.biweekly_official_jobs (report_job_id)
    WHERE report_job_id IS NOT NULL;

COMMENT ON TABLE agric_satellite.biweekly_official_jobs IS
    'Authoritative official biweekly generate jobs and OSS object references; not a signed-URL cache and not a migration of historical /tmp job files.';
COMMENT ON COLUMN agric_satellite.biweekly_official_jobs.job_id IS
    'Official generate id (bj- + 16 hex). This is not agric_satellite.jobs.id, which is a UUID.';
COMMENT ON COLUMN agric_satellite.biweekly_official_jobs.artifact_json IS
    'Permanent OSS object reference (bucket, key, size_bytes, sha256, content-type). Never a presigned URL.';
COMMENT ON COLUMN agric_satellite.biweekly_official_jobs.job_json IS
    'Full official job document used by GET job / periods / download.';
COMMENT ON COLUMN agric_satellite.biweekly_official_jobs.report_job_id IS
    'Optional link to the business Job UUID in agric_satellite.jobs.';

COMMIT;
