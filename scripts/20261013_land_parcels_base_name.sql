-- land_parcels 新增租户/基地名称（来自 MySQL agriculture_baseinfo.base_name）。
-- 生产已手工加列并刷数；本脚本幂等，可重复执行。
ALTER TABLE agric_satellite.land_parcels
    ADD COLUMN IF NOT EXISTS base_name TEXT;
