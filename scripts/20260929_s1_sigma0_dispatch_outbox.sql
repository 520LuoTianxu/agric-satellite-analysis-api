-- 持久化 S1 Sigma0 子任务派发意图，恢复 Job 提交与 MQ 确认之间的崩溃窗口。
BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.s1_sigma0_dispatch_outbox (
    job_id uuid PRIMARY KEY
        REFERENCES agric_satellite.jobs(id) ON DELETE CASCADE,
    land_id text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT s1_sigma0_dispatch_outbox_status_ck
        CHECK (status IN ('pending', 'processing', 'completed', 'discarded')),
    CONSTRAINT s1_sigma0_dispatch_outbox_attempts_ck
        CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS ix_s1_sigma0_dispatch_outbox_pending
    ON agric_satellite.s1_sigma0_dispatch_outbox (available_at, created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ix_s1_sigma0_dispatch_outbox_expired_lease
    ON agric_satellite.s1_sigma0_dispatch_outbox (lease_until)
    WHERE status = 'processing';

COMMENT ON TABLE agric_satellite.s1_sigma0_dispatch_outbox IS
    'S1 Sigma0 回算子任务的持久化MQ派发意图；使用租约和退避提供至少一次恢复';
COMMENT ON COLUMN agric_satellite.s1_sigma0_dispatch_outbox.job_id IS
    '与一个遥感批次子Job一一对应的稳定幂等键';

COMMIT;
