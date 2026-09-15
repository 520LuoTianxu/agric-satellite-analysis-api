-- CloudAMQP outer task-bus result store (also applied via Alembic 0016).
CREATE SCHEMA IF NOT EXISTS agric_satellite;

CREATE TABLE IF NOT EXISTS agric_satellite.mq_task_results (
    task_id text PRIMARY KEY,
    status text NOT NULL,
    oss_urls jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    field_id text,
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
