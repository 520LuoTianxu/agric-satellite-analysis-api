-- 收获占比 v3（s2s1_season_monotonic_v3）：逐观测置信度、S1 佐证、阈值来源。
-- 依赖 20261009_parcel_harvest_progress.sql（先执行）。可重复执行；只加列与约束，不删数据。
-- 旧版本（v2）结果保留到按地块重算时由服务替换；接口只读当前版本，
-- 未重算的地块会现场计算并自动入队。
BEGIN;

ALTER TABLE agric_satellite.parcel_harvest_progress
    ADD COLUMN IF NOT EXISTS confidence real,
    ADD COLUMN IF NOT EXISTS confidence_level text,
    ADD COLUMN IF NOT EXISTS confidence_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS confirmed_by text,
    ADD COLUMN IF NOT EXISTS gap_days integer,
    ADD COLUMN IF NOT EXISTS s1_date date,
    ADD COLUMN IF NOT EXISTS s1_delta_vh_db real,
    ADD COLUMN IF NOT EXISTS s1_delta_ratio_db real,
    ADD COLUMN IF NOT EXISTS s1_agreement text,
    ADD COLUMN IF NOT EXISTS threshold_source text;

ALTER TABLE agric_satellite.parcel_harvest_progress
    DROP CONSTRAINT IF EXISTS parcel_harvest_progress_confidence_ck;
ALTER TABLE agric_satellite.parcel_harvest_progress
    ADD CONSTRAINT parcel_harvest_progress_confidence_ck
        CHECK ((confidence IS NULL OR confidence BETWEEN 0 AND 1)
               AND (confidence_level IS NULL
                    OR confidence_level IN ('high', 'medium', 'low'))
               AND (confirmed_by IS NULL OR confirmed_by IN ('s2', 's1'))
               AND (s1_agreement IS NULL
                    OR s1_agreement IN ('agree', 'disagree', 'ambiguous'))
               AND jsonb_typeof(confidence_reasons) = 'array');

COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.confidence IS
    '本期置信度 0–1：数据量/间隔/阈值余量/确认/S1 一致性的加权平均（见 ADR 20261009）';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.confidence_level IS
    'high ≥0.75，medium ≥0.50，其余 low；待确认 ≥25% 或 S1 矛盾时最高 medium';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.confidence_reasons IS
    '原因码数组：low_valid_pct、few_pixels、long_gap、small_margin、unconfirmed、s1_confirmed、s1_agree、s1_disagree';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.confirmed_by IS
    '已收获像元的确认依据：s2 下一期光学 / s1 Sentinel-1 佐证；无已收获像元或未确认为空';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.s1_delta_ratio_db IS
    '同轨道同版本 S1 地块中位 VH−VV 相对本季峰值期的变化（dB），仅峰值之后填写';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.threshold_source IS
    '阈值来源：profile:<作物@省份|作物|@省份> | adaptive（地块多季历史）| default';

COMMIT;
