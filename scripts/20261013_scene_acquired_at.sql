-- 20261013 parcel_scene_products 增加影像拍摄时间 acquired_at（精确到毫秒，带时区）
--
-- 执行顺序（必须）：
--   1) 先执行本文件第 1 步 ALTER（加列，秒级，只改元数据，不重写表）；
--   2) 再部署 API（含 mq_result_writer）与下载机/ingest；
--      新代码 SELECT/INSERT 都引用 acquired_at，未加列就部署会报错。
--   3) 部署后任意时间执行第 2 步历史回填（可多次执行，可分批）。
--
-- 每条语句都是独立的，客户端自动提交也没问题，重复执行无副作用。

-- ===== 第 1 步：加列 =====
ALTER TABLE agric_satellite.parcel_scene_products
    ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ;

COMMENT ON COLUMN agric_satellite.parcel_scene_products.acquired_at IS
    '影像拍摄时间，来自 STAC properties.datetime（UTC）。历史行回填为 date 的 UTC 00:00:00。';

-- ===== 第 2 步：历史回填（旧数据用 date 的 UTC 00:00:00）=====
-- 选 UTC 00:00 而不是北京时间 00:00：
--   * 北京 00:00 存成 UTC 是前一天 16:00Z，前端/接口若直接截取 ISO 字符串前 10 位就会显示成前一天；
--   * UTC 00:00 在北京显示为当天 08:00，在 UTC 显示为当天 00:00，两种取法都不会跨日；
--   * date 本身就是按 STAC UTC 时间取的日期，口径一致。
-- 只更新 acquired_at IS NULL 的行，新代码写入的真实拍摄时间不会被覆盖。

-- 写法 A：数据量不大时一条执行完
UPDATE agric_satellite.parcel_scene_products
SET acquired_at = (date::timestamp AT TIME ZONE 'UTC')
WHERE acquired_at IS NULL;

-- 写法 B：数据量大时分批执行（推荐）。每次最多更新 50000 行，
-- 反复执行同一条语句，直到返回 UPDATE 0 为止。
UPDATE agric_satellite.parcel_scene_products p
SET acquired_at = (p.date::timestamp AT TIME ZONE 'UTC')
FROM (
    SELECT land_id, date, sensor, scene_id
    FROM agric_satellite.parcel_scene_products
    WHERE acquired_at IS NULL
    LIMIT 50000
) b
WHERE p.land_id = b.land_id
  AND p.date = b.date
  AND p.sensor = b.sensor
  AND p.scene_id = b.scene_id
  AND p.acquired_at IS NULL;

-- 写法 C：按 land_id 范围分批（land_id 是文本，按首位数字切分；各条独立，顺序随意）
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL AND land_id LIKE '1%';
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL AND land_id LIKE '2%';
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL AND land_id LIKE '3%';
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL AND land_id LIKE '4%';
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL AND land_id LIKE '5%';
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL AND land_id LIKE '6%';
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL AND land_id LIKE '7%';
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL AND land_id LIKE '8%';
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL AND land_id LIKE '9%';
-- 兜底：上面没覆盖到的（非数字开头等）
UPDATE agric_satellite.parcel_scene_products SET acquired_at = (date::timestamp AT TIME ZONE 'UTC') WHERE acquired_at IS NULL;

-- ===== 核对（只读）=====
SELECT sensor,
       count(*) AS total,
       count(*) FILTER (WHERE acquired_at IS NULL) AS still_null,
       count(*) FILTER (WHERE acquired_at <> (date::timestamp AT TIME ZONE 'UTC')) AS real_stac_time
FROM agric_satellite.parcel_scene_products
GROUP BY sensor;
