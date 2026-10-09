-- 收获逐像元状态（0=未收获、1=疑似收获、2=已收获、无数据）。
-- 依赖 20261009_parcel_harvest_progress*.sql（含 v4_residue）先执行。可重复执行；只建表/加列。
-- 像元坐标集合每地块存一份（按内容哈希去重），每个观测行只存与坐标一一对应的状态字符串：
--   '0' 未收获、'1' 疑似收获、'2' 已收获（含待确认）、'.' 无数据（季外/非作物/本期云且此前未计入）。
-- 接口解码为整数，无数据 = 255。执行后需 harvest-progress/backfill 才会为历史行写入状态。
BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.parcel_harvest_pixel_sets (
    land_id     text        NOT NULL,
    set_hash    text        NOT NULL,
    pixel_count integer     NOT NULL CHECK (pixel_count >= 0),
    pixels      jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (land_id, set_hash)
);

COMMENT ON TABLE agric_satellite.parcel_harvest_pixel_sets IS
    '收获逐像元状态的像元坐标集合：pixels = {"format":"lonlat_index_v1","lon":[...],"lat":[...]}，顺序即状态字符串顺序';

ALTER TABLE agric_satellite.parcel_harvest_progress
    ADD COLUMN IF NOT EXISTS pixel_set_hash text,
    ADD COLUMN IF NOT EXISTS pixel_states text;

ALTER TABLE agric_satellite.parcel_harvest_progress
    DROP CONSTRAINT IF EXISTS parcel_harvest_progress_pixel_states_ck;
ALTER TABLE agric_satellite.parcel_harvest_progress
    ADD CONSTRAINT parcel_harvest_progress_pixel_states_ck
        CHECK (pixel_states IS NULL OR pixel_states ~ '^[012.]*$');

COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.pixel_set_hash IS
    '像元坐标集合（parcel_harvest_pixel_sets.set_hash）';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.pixel_states IS
    '逐像元状态，每像元一个字符：0 未收获 / 1 疑似收获 / 2 已收获 / . 无数据；季内单调';

COMMIT;
