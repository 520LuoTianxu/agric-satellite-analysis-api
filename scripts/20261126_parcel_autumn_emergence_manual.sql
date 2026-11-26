-- 人工秋季出苗触发：给已执行过 20261126_parcel_autumn_emergence.sql 的库补列和新表。
-- 可重复执行。API 只排期和接收官方 S2 回执，不在这张脚本里下载影像。
BEGIN;

ALTER TABLE agric_satellite.parcel_autumn_emergence
    ADD COLUMN IF NOT EXISTS sowing_date date;

COMMENT ON COLUMN agric_satellite.parcel_autumn_emergence.sowing_date IS
    '人工触发记下的播种日。观测和土壤本底从该日（含）的晴空 S2 起算，不用播种前的场景。自动季可为空。';

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
