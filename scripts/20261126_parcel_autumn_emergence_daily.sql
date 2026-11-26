-- 秋季出苗改挂在官方日指标 JSON 上：下载机计算，API 只写地块“本季已出苗”
-- 标记并提供读取。去掉独立出苗表、outbox 和请求表。
-- 可重复执行。不要在生产库自动跑；由操作员择机执行。
BEGIN;

ALTER TABLE agric_satellite.land_parcels
    ADD COLUMN IF NOT EXISTS autumn_emerged boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS autumn_emerged_season text,
    ADD COLUMN IF NOT EXISTS autumn_emerged_date date,
    ADD COLUMN IF NOT EXISTS autumn_emerged_at timestamptz,
    ADD COLUMN IF NOT EXISTS autumn_sowing_date date,
    ADD COLUMN IF NOT EXISTS autumn_sowing_season text;

COMMENT ON COLUMN agric_satellite.land_parcels.autumn_emerged IS
    '本季（autumn_emerged_season）是否已完成秋季出苗。冻结后同季不再重算。空表示未出苗或未计算。';
COMMENT ON COLUMN agric_satellite.land_parcels.autumn_emerged_season IS
    '已出苗标记所属播种年秋季，例如 2026-autumn。下一季换新的 season key。';
COMMENT ON COLUMN agric_satellite.land_parcels.autumn_emerged_date IS
    '第一次满足持续抬升规则的官方晴空产品日，不是农学出苗日。';
COMMENT ON COLUMN agric_satellite.land_parcels.autumn_emerged_at IS
    '地块被标记为本季已出苗的时间。不自动解冻。';
COMMENT ON COLUMN agric_satellite.land_parcels.autumn_sowing_date IS
    '人工拉取出苗时记下的播种日。观测窗口从该日（含）起算。不改 crop_type。';
COMMENT ON COLUMN agric_satellite.land_parcels.autumn_sowing_season IS
    'autumn_sowing_date 所属秋季。跨季后由新的人工触发覆盖。';

-- 把旧表里已冻结的结果迁到地块标记，避免切流量后丢掉已出苗状态。
DO $$
BEGIN
    IF to_regclass('agric_satellite.parcel_autumn_emergence') IS NOT NULL THEN
        UPDATE agric_satellite.land_parcels AS land
        SET autumn_emerged = true,
            autumn_emerged_season = src.season_key,
            autumn_emerged_date = src.first_emerged_date,
            autumn_emerged_at = COALESCE(src.frozen_at, src.computed_at, now()),
            autumn_sowing_date = COALESCE(land.autumn_sowing_date, src.sowing_date),
            autumn_sowing_season = COALESCE(
                land.autumn_sowing_season,
                CASE WHEN src.sowing_date IS NOT NULL THEN src.season_key END
            )
        FROM agric_satellite.parcel_autumn_emergence AS src
        WHERE land.land_id = src.land_id
          AND src.emerged_flag IS TRUE
          AND src.frozen IS TRUE
          AND land.autumn_emerged IS NOT TRUE;
    END IF;
    IF to_regclass('agric_satellite.parcel_autumn_emergence_requests') IS NOT NULL THEN
        UPDATE agric_satellite.land_parcels AS land
        SET autumn_sowing_date = COALESCE(land.autumn_sowing_date, src.sowing_date),
            autumn_sowing_season = COALESCE(land.autumn_sowing_season, src.season_key)
        FROM agric_satellite.parcel_autumn_emergence_requests AS src
        WHERE land.land_id = src.land_id
          AND src.sowing_date IS NOT NULL
          AND (
              land.autumn_sowing_date IS NULL
              OR land.autumn_sowing_season IS DISTINCT FROM src.season_key
          )
          AND COALESCE(land.autumn_emerged, false) = false;
    END IF;
END
$$;

DROP TABLE IF EXISTS agric_satellite.parcel_autumn_emergence_outbox;
DROP TABLE IF EXISTS agric_satellite.parcel_autumn_emergence_requests;
DROP TABLE IF EXISTS agric_satellite.parcel_autumn_emergence;

COMMIT;
