-- 将遥感任务发布回执从 jobs.progress_json 拆到有唯一键的关系表。
-- 这样每景增量只写新增行，不再反复重写同一任务不断变大的 JSONB。
BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.satellite_job_product_receipts (
    job_id uuid NOT NULL
        REFERENCES agric_satellite.jobs(id) ON DELETE CASCADE,
    land_id text NOT NULL,
    product_date date NOT NULL,
    sensor text NOT NULL,
    scene_id text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT satellite_job_product_receipts_pkey
        PRIMARY KEY (job_id, land_id, product_date, sensor, scene_id),
    CONSTRAINT satellite_job_product_receipts_sensor_ck
        CHECK (sensor IN ('S1', 'S2'))
);

COMMENT ON TABLE agric_satellite.satellite_job_product_receipts IS
    '遥感任务已发布的地块场景回执；以任务和场景复合键保证重试幂等';
COMMENT ON COLUMN agric_satellite.satellite_job_product_receipts.scene_id IS
    'STAC 场景标识；空字符串仅用于兼容缺少场景号的历史 worker 回执';

COMMIT;
