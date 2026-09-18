-- CloudAMQP outer task-bus result store (also applied via Alembic 0016).
CREATE SCHEMA IF NOT EXISTS agric_satellite;

CREATE TABLE IF NOT EXISTS agric_satellite.mq_task_results (
    task_id text PRIMARY KEY,
    status text NOT NULL,
    oss_urls jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    land_id text,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT mq_task_results_status_ck
        CHECK (status = ANY (ARRAY['success'::text, 'failed'::text]))
);

CREATE INDEX IF NOT EXISTS ix_mq_task_results_updated_at
    ON agric_satellite.mq_task_results (updated_at DESC);

COMMENT ON TABLE agric_satellite.mq_task_results IS
    'CloudAMQP outer bus results (OSS URLs + downloaded JSON payloads)';

-- Claim worker heartbeats and administrator-triggered Beat task history.
CREATE TABLE IF NOT EXISTS agric_satellite.download_workers (
    worker_id text PRIMARY KEY,
    mode varchar(20) NOT NULL DEFAULT 'claim',
    claim_types_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    poll_interval_seconds integer NOT NULL DEFAULT 4,
    last_claim_count integer NOT NULL DEFAULT 0,
    total_claims integer NOT NULL DEFAULT 0,
    queue_name varchar(128) NOT NULL DEFAULT 'cpu_compute',
    queue_depths_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    pending_queue_count integer,
    last_claim_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agric_satellite.admin_task_runs (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_key varchar(64) NOT NULL,
    task_name varchar(255) NOT NULL,
    celery_task_id text UNIQUE,
    status varchar(20) NOT NULL DEFAULT 'queued',
    params_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_json jsonb,
    error text,
    triggered_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_download_workers_last_claim_at
    ON agric_satellite.download_workers (last_claim_at DESC);
CREATE INDEX IF NOT EXISTS ix_admin_task_runs_created_at
    ON agric_satellite.admin_task_runs (created_at DESC);

ALTER TABLE agric_satellite.download_workers
    ADD COLUMN IF NOT EXISTS queue_name varchar(128) NOT NULL DEFAULT 'cpu_compute';
ALTER TABLE agric_satellite.download_workers
    ADD COLUMN IF NOT EXISTS pending_queue_count integer;
ALTER TABLE agric_satellite.download_workers
    ADD COLUMN IF NOT EXISTS queue_depths_json jsonb NOT NULL DEFAULT '{}'::jsonb;
