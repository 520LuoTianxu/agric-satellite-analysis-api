-- 持久化 S2 去云排程意图，避免迟到 worker 派发失败后无法恢复。
BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.satellite_decloud_schedule_outbox (
    schedule_key text PRIMARY KEY,
    job_id uuid NOT NULL
        REFERENCES agric_satellite.jobs(id) ON DELETE CASCADE,
    land_id text NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    raw_results jsonb NOT NULL,
    mq_task_id text,
    season_months smallint[],
    crop_type text,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT satellite_decloud_schedule_outbox_status_ck
        CHECK (status IN ('pending', 'processing', 'completed')),
    CONSTRAINT satellite_decloud_schedule_outbox_attempts_ck
        CHECK (attempts >= 0),
    CONSTRAINT satellite_decloud_schedule_outbox_dates_ck
        CHECK (date_from <= date_to),
    CONSTRAINT satellite_decloud_schedule_outbox_raw_results_ck
        CHECK (jsonb_typeof(raw_results) = 'array')
);

CREATE INDEX IF NOT EXISTS ix_satellite_decloud_schedule_outbox_pending
    ON agric_satellite.satellite_decloud_schedule_outbox (available_at, created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ix_satellite_decloud_schedule_outbox_expired_lease
    ON agric_satellite.satellite_decloud_schedule_outbox (lease_until)
    WHERE status = 'processing';

COMMENT ON TABLE agric_satellite.satellite_decloud_schedule_outbox IS
    'S2 原始产品完成后的持久化去云排程意图；租约和退避支持至少一次恢复';
COMMENT ON COLUMN agric_satellite.satellite_decloud_schedule_outbox.schedule_key IS
    '由任务、地块和场景快照生成的稳定幂等键';
COMMENT ON COLUMN agric_satellite.satellite_decloud_schedule_outbox.raw_results IS
    '去云规划所需的原始场景质量元数据，不包含像元数组或栅格内容';

COMMIT;
