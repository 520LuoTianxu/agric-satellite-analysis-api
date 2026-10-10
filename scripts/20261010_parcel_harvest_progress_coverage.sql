-- 收获占比落库门槛配套：已计算区间标记。
-- 依赖 20261009_parcel_harvest_*.sql 先执行。可重复执行；只建表，不改动现有结果行。
-- 只有合计占比（已收获+疑似）> HARVEST_PROGRESS_MIN_SAVE_PCT（默认 3）的观测日写入
-- parcel_harvest_progress；本表记录每个地块已按当前算法版本计算过的日期区间，
-- 使“已计算但全部低于门槛”与“从未计算”可区分，接口不再对这类地块反复现场计算/入队。
-- 执行后需 harvest-progress/backfill 重算：历史上低于门槛的旧行会在重算区间内被删除，
-- 并为每个地块写入区间标记。
BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.parcel_harvest_progress_coverage (
    land_id        text          NOT NULL,
    sensor         text          NOT NULL,
    method_version text          NOT NULL,
    computed_from  date          NOT NULL,
    computed_to    date          NOT NULL,
    min_save_pct   numeric(6, 2) NOT NULL,
    computed_rows  integer       NOT NULL DEFAULT 0 CHECK (computed_rows >= 0),
    saved_rows     integer       NOT NULL DEFAULT 0 CHECK (saved_rows >= 0),
    updated_at     timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (land_id, sensor, method_version),
    CONSTRAINT parcel_harvest_progress_coverage_range_ck CHECK (computed_from <= computed_to)
);

COMMENT ON TABLE agric_satellite.parcel_harvest_progress_coverage IS
    '收获占比已计算区间标记：区间内未落库的观测日即合计占比未超过 min_save_pct（或无有效观测）';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress_coverage.min_save_pct IS
    '计算时使用的落库门槛（%）；门槛变化后区间标记从新的计算区间重新开始';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress_coverage.computed_rows IS
    '最近一次重算区间内计算出的观测行数（含低于门槛未落库的行）';
COMMENT ON COLUMN agric_satellite.parcel_harvest_progress_coverage.saved_rows IS
    '最近一次重算区间内实际落库的观测行数';

COMMIT;

-- 可选（不必执行，backfill 重算会覆盖相同区间）：立即清理门槛以下的历史行。
-- DELETE FROM agric_satellite.parcel_harvest_progress
-- WHERE COALESCE(harvested_or_suspected_pct, harvested_pct, 0) <= 3;
