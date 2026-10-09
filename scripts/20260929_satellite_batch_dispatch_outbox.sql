-- 通用卫星批任务与MQ派发意图必须同事务提交，供API后台安全重试。
BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.satellite_batch_dispatch_outbox (
    task_id uuid PRIMARY KEY
        REFERENCES agric_satellite.jobs(id) ON DELETE CASCADE,
    land_id text NOT NULL,
    extras_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    priority integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT satellite_batch_dispatch_outbox_status_ck
        CHECK (status IN ('pending', 'processing', 'completed', 'discarded')),
    CONSTRAINT satellite_batch_dispatch_outbox_attempts_ck
        CHECK (attempts >= 0),
    CONSTRAINT satellite_batch_dispatch_outbox_extras_ck
        CHECK (jsonb_typeof(extras_json) = 'object')
);

CREATE INDEX IF NOT EXISTS ix_satellite_batch_dispatch_outbox_pending
    ON agric_satellite.satellite_batch_dispatch_outbox (available_at, created_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ix_satellite_batch_dispatch_outbox_expired_lease
    ON agric_satellite.satellite_batch_dispatch_outbox (lease_until)
    WHERE status = 'processing';

COMMENT ON TABLE agric_satellite.satellite_batch_dispatch_outbox IS
    '通用卫星批任务的持久MQ派发意图，使用数据库租约和退避恢复提交后的派发';
COMMENT ON COLUMN agric_satellite.satellite_batch_dispatch_outbox.task_id IS
    '与一个卫星批次子Job一一对应的稳定幂等键';
COMMENT ON COLUMN agric_satellite.satellite_batch_dispatch_outbox.last_error IS
    '仅供服务端运维排障的脱敏派发错误，不应直接放入公开任务响应';

COMMIT;
