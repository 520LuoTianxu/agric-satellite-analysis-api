-- CloudAMQP outer task-bus result store (also applied via Alembic 0016).
CREATE SCHEMA IF NOT EXISTS agri;

CREATE TABLE IF NOT EXISTS agri.mq_task_results (
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
    ON agri.mq_task_results (updated_at DESC);

COMMENT ON TABLE agri.mq_task_results IS
    'CloudAMQP outer bus results (OSS URLs + downloaded JSON payloads)';
