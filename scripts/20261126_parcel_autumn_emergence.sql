-- 冬小麦秋季出苗（相对本块土壤本底的持续抬升）。
-- 新表，不改 parcel_scene_products 的 NDVI 列，也不改 s1s2_parcel JSON。
-- 可重复执行。
BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.parcel_autumn_emergence (
    land_id text NOT NULL,
    season_key text NOT NULL,
    sowing_date date,
    status text NOT NULL,
    emerged_flag boolean NOT NULL DEFAULT false,
    frozen boolean NOT NULL DEFAULT false,
    baseline_start date,
    baseline_end date,
    baseline_statistic text NOT NULL,
    noise_floor double precision,
    noise_statistic text NOT NULL,
    as_of_date date,
    first_emerged_date date,
    method_version text NOT NULL,
    n_clear integer NOT NULL DEFAULT 0,
    n_pixels integer NOT NULL DEFAULT 0,
    median_increment double precision,
    share_above_noise double precision,
    share_sustained double precision,
    share_ndvi_ge_0_30 double precision,
    ndre_share_above_noise double precision,
    summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    timeseries jsonb NOT NULL DEFAULT '[]'::jsonb,
    pixel_oss_key text,
    computed_at timestamptz NOT NULL DEFAULT now(),
    frozen_at timestamptz,
    PRIMARY KEY (land_id, season_key),
    CONSTRAINT parcel_autumn_emergence_status_ck
        CHECK (status IN ('not_emerged', 'emerging', 'emerged', 'uncertain')),
    CONSTRAINT parcel_autumn_emergence_season_ck
        CHECK (season_key ~ '^[0-9]{4}-autumn$'),
    CONSTRAINT parcel_autumn_emergence_freeze_ck
        CHECK (emerged_flag = frozen),
    CONSTRAINT parcel_autumn_emergence_oss_key_ck
        CHECK (
            pixel_oss_key IS NULL
            OR pixel_oss_key LIKE 'emergence/autumn/%'
        )
);

COMMENT ON TABLE agric_satellite.parcel_autumn_emergence IS
    '冬小麦秋季出苗：地块×播种年一条。已出苗后 frozen，同季不再重算。不是 NDVI 统计列，也不是返青。';
COMMENT ON COLUMN agric_satellite.parcel_autumn_emergence.noise_floor IS
    '该地块本季实际使用的抬升阈值（平台残差绝对值 P99），不是全局常数，也不是 NDVI 0.30。';
COMMENT ON COLUMN agric_satellite.parcel_autumn_emergence.share_ndvi_ge_0_30 IS
    '截至日 NDVI≥0.30 的像元比例，只作产品参考刻度的对照，不参与出苗判定。';
COMMENT ON COLUMN agric_satellite.parcel_autumn_emergence.first_emerged_date IS
    '第一次满足持续抬升规则的晴空产品日，不是农学出苗日，也不是缺苗率或苗数。';
COMMENT ON COLUMN agric_satellite.parcel_autumn_emergence.pixel_oss_key IS
    '像元快照对象键，前缀 emergence/autumn/。禁止写入 s1s2_parcel/json。';
COMMENT ON COLUMN agric_satellite.parcel_autumn_emergence.frozen_at IS
    '判为已出苗并冻结的时间。不自动解冻；下一播种季是新的 season_key。';

-- 已执行过旧版建表脚本的库，补上播种日列。新库 CREATE 已包含该列。
ALTER TABLE agric_satellite.parcel_autumn_emergence
    ADD COLUMN IF NOT EXISTS sowing_date date;

COMMENT ON COLUMN agric_satellite.parcel_autumn_emergence.sowing_date IS
    '人工触发记下的播种日。观测和土壤本底从该日（含）的晴空 S2 起算，不用播种前的场景。自动季可为空。';

CREATE TABLE IF NOT EXISTS agric_satellite.parcel_autumn_emergence_outbox (
    land_id text PRIMARY KEY,
    scene_date date,
    status text NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT parcel_autumn_emergence_outbox_status_ck
        CHECK (status IN ('pending', 'processing', 'completed')),
    CONSTRAINT parcel_autumn_emergence_outbox_attempts_ck
        CHECK (attempts >= 0)
);

CREATE INDEX IF NOT EXISTS ix_parcel_autumn_emergence_outbox_pending
    ON agric_satellite.parcel_autumn_emergence_outbox (available_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS ix_parcel_autumn_emergence_outbox_expired_lease
    ON agric_satellite.parcel_autumn_emergence_outbox (lease_until)
    WHERE status = 'processing';

COMMENT ON TABLE agric_satellite.parcel_autumn_emergence_outbox IS
    '待重算秋季出苗的地块。下载机只入队场景结果；计算在 API 本机，不连下载机 Postgres。';

-- 人工触发在算出结果前先记下播种日。下载机不写这张表。
CREATE TABLE IF NOT EXISTS agric_satellite.parcel_autumn_emergence_requests (
    land_id text NOT NULL,
    season_key text NOT NULL,
    sowing_date date NOT NULL,
    status text NOT NULL DEFAULT 'scheduled',
    dispatch text NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    job_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (land_id, season_key),
    CONSTRAINT parcel_autumn_emergence_requests_season_ck
        CHECK (season_key ~ '^[0-9]{4}-autumn$'),
    CONSTRAINT parcel_autumn_emergence_requests_status_ck
        CHECK (status IN ('scheduled', 'ready')),
    CONSTRAINT parcel_autumn_emergence_requests_dispatch_ck
        CHECK (dispatch IN ('compute', 's2_download')),
    CONSTRAINT parcel_autumn_emergence_requests_window_ck
        CHECK (date_from <= date_to)
);

COMMENT ON TABLE agric_satellite.parcel_autumn_emergence_requests IS
    '前端按播种日手动触发的秋季出苗请求。API 只排期；缺晴空 S2 时派发现有 satellite_batch，算完后 status=ready。';
COMMENT ON COLUMN agric_satellite.parcel_autumn_emergence_requests.dispatch IS
    'compute：库里已有播种日之后的官方晴空 S2，只入 outbox。s2_download：缺景，只派发 S2，不派发 S1。';

COMMIT;
