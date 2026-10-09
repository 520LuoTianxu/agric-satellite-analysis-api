-- 按观测日期记录地块已收获面积占比，并用持久化 outbox 承接新影像入库后的重算。
BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.parcel_harvest_progress (
    land_id text NOT NULL,
    obs_date date NOT NULL,
    sensor text NOT NULL DEFAULT 'S2',
    method_version text NOT NULL,
    scene_id text,
    status text NOT NULL,
    harvested_pct numeric(5, 1) NOT NULL,
    newly_harvested_pct numeric(5, 1) NOT NULL DEFAULT 0,
    harvested_area_mu numeric(14, 2),
    parcel_area_mu numeric(14, 2),
    valid_pct numeric(5, 1) NOT NULL,
    valid_pixel_count integer NOT NULL,
    harvested_pixel_count integer NOT NULL,
    total_pixel_count integer NOT NULL,
    crop_pixel_count integer,
    mean_ndvi real,
    greenness real,
    peak_greenness real,
    peak_date date,
    season_start date,
    vegetation_index text,
    confirmed boolean NOT NULL DEFAULT true,
    official boolean NOT NULL DEFAULT true,
    params jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (land_id, obs_date, sensor, method_version),
    CONSTRAINT parcel_harvest_progress_pct_ck
        CHECK (harvested_pct BETWEEN 0 AND 100
               AND newly_harvested_pct BETWEEN 0 AND 100
               AND valid_pct BETWEEN 0 AND 100)
);

-- 已按 v1（s2_ndvi_peak_drop_v1）建过表的环境：补齐 v2 列、替换状态约束、清掉 v1 结果。
-- 本脚本可重复执行。
ALTER TABLE agric_satellite.parcel_harvest_progress
    ADD COLUMN IF NOT EXISTS crop_pixel_count integer,
    ADD COLUMN IF NOT EXISTS greenness real,
    ADD COLUMN IF NOT EXISTS peak_greenness real,
    ADD COLUMN IF NOT EXISTS season_start date,
    ADD COLUMN IF NOT EXISTS vegetation_index text,
    ADD COLUMN IF NOT EXISTS confirmed boolean NOT NULL DEFAULT true;
ALTER TABLE agric_satellite.parcel_harvest_progress
    DROP COLUMN IF EXISTS peak_ndvi;
ALTER TABLE agric_satellite.parcel_harvest_progress
    DROP CONSTRAINT IF EXISTS parcel_harvest_progress_status_ck;
DELETE FROM agric_satellite.parcel_harvest_progress
WHERE method_version <> 's2_season_monotonic_v2';
ALTER TABLE agric_satellite.parcel_harvest_progress
    ADD CONSTRAINT parcel_harvest_progress_status_ck
        CHECK (status IN ('off_season', 'growing', 'harvesting', 'harvested'));

CREATE INDEX IF NOT EXISTS ix_parcel_harvest_progress_date
    ON agric_satellite.parcel_harvest_progress (obs_date);

COMMENT ON TABLE agric_satellite.parcel_harvest_progress IS
    '地块逐观测日已收获面积占比（S2 像元分季粘滞启发式，method_version 区分算法版本，表内只保留当前版本）';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.harvested_pct IS
    '本季已收获作物像元 / 本季作物像元 ×100，季内单调不减；季外为 0';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.newly_harvested_pct IS
    '较同季上一有效观测日新增的已收获占比，不为负';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.season_start IS
    '所属生长季的返青日期（季标识）；季外为空';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.greenness IS
    '有效像元统一绿度中位数（按产品选择 NDVI/EVI 线性映射，0≈裸土 1≈茂密植被）';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.confirmed IS
    'false 表示含尚待下一期影像确认的候选像元';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.params IS
    '计算时使用的阈值快照，便于追溯与重算';

CREATE TABLE IF NOT EXISTS agric_satellite.parcel_harvest_progress_outbox (
    land_id text PRIMARY KEY,
    date_from date,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT parcel_harvest_progress_outbox_status_ck
        CHECK (status IN ('pending', 'processing', 'completed')),
    CONSTRAINT parcel_harvest_progress_outbox_attempts_ck
        CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS ix_parcel_harvest_progress_outbox_pending
    ON agric_satellite.parcel_harvest_progress_outbox (available_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ix_parcel_harvest_progress_outbox_expired_lease
    ON agric_satellite.parcel_harvest_progress_outbox (lease_until)
    WHERE status = 'processing';

COMMENT ON TABLE agric_satellite.parcel_harvest_progress_outbox IS
    '待重算收获占比的地块；同一地块多次入队合并为一条，date_from 取最早日期（NULL 表示近期窗口）';

COMMIT;
