-- 收获报表列表加速用索引。每条独立、可重复执行；CONCURRENTLY 不能放在事务里（自动提交客户端可直接逐条执行）。
-- 每地块取最新 S2 景日期：(land_id, sensor, date) 让 LATERAL ... ORDER BY date DESC LIMIT 1 走 index-only 一次探测。
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_psp_land_sensor_date
    ON agric_satellite.parcel_scene_products (land_id, sensor, date DESC);
-- 收获结果按 (sensor, method_version, land_id, obs_date) 取区间行/区间前最后一行。
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_php_sensor_ver_land_date
    ON agric_satellite.parcel_harvest_progress (sensor, method_version, land_id, obs_date DESC);
