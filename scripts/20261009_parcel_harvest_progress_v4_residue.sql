-- 收获占比 v4（s2s1_residue_monotonic_v4）：秸秆残茬判据 + “疑似收获”档。
-- 依赖 20261009_parcel_harvest_progress.sql、20261009_parcel_harvest_progress_v3.sql（先执行）。
-- 可重复执行；只加列与约束，不删数据。v3 结果保留，接口只读当前版本（v4），
-- 未重算的地块会现场计算并自动入队；建议执行后跑一次 harvest-progress/backfill。
BEGIN;

ALTER TABLE agric_satellite.parcel_harvest_progress
    ADD COLUMN IF NOT EXISTS suspected_harvest_pct real,
    ADD COLUMN IF NOT EXISTS harvested_or_suspected_pct real,
    ADD COLUMN IF NOT EXISTS suspected_pixel_count integer,
    ADD COLUMN IF NOT EXISTS residue_harvested_pct real,
    ADD COLUMN IF NOT EXISTS residue_pixel_count integer;

ALTER TABLE agric_satellite.parcel_harvest_progress
    DROP CONSTRAINT IF EXISTS parcel_harvest_progress_residue_ck;
ALTER TABLE agric_satellite.parcel_harvest_progress
    ADD CONSTRAINT parcel_harvest_progress_residue_ck
        CHECK ((suspected_harvest_pct IS NULL OR suspected_harvest_pct BETWEEN 0 AND 100)
               AND (harvested_or_suspected_pct IS NULL
                    OR (harvested_or_suspected_pct BETWEEN 0 AND 100
                        AND harvested_or_suspected_pct >= harvested_pct - 0.05))
               AND (suspected_pixel_count IS NULL OR suspected_pixel_count >= 0)
               AND (residue_harvested_pct IS NULL OR residue_harvested_pct BETWEEN 0 AND 100)
               AND (residue_pixel_count IS NULL OR residue_pixel_count >= 0));

COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.suspected_harvest_pct IS
    '疑似收获占比 0–100：峰值后秸秆残茬样（绿度≤峰值×0.55、NDMI≤0、估算红光≥0.085、下一期不回绿），与枯熟站秆难区分；不含在 harvested_pct 内，确认后转入 harvested_pct';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.harvested_or_suspected_pct IS
    '已收获 + 疑似收获，0–100，季内单调不减';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.suspected_pixel_count IS
    '疑似收获作物像元数';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.residue_harvested_pct IS
    'harvested_pct 中先经留茬判据检出、后经裸土级/突变/S1 确认晋升的部分';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.residue_pixel_count IS
    '经留茬判据检出的作物像元数（疑似 + 已晋升）';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress.confidence_reasons IS
    '原因码数组：low_valid_pct、few_pixels、long_gap、small_margin、unconfirmed、s1_confirmed、s1_agree、s1_disagree、residue_signature、suspected_harvest、promoted_bare、promoted_abrupt、promoted_s1';

COMMIT;
