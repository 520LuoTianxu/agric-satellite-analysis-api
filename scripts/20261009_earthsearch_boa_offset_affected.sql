-- 只读报告：受 Earth Search sentinel-2-l2a BOA 偏移重复扣除影响的 S2 景（不写库）。
--
-- 背景：Earth Search `sentinel-2-l2a` COG 已扣除 BOA 偏移
-- （item.properties."earthsearch:boa_offset_applied" = true），但 raster:bands 仍声明
-- offset=-0.1；stac-optical-lonlat-v3/v4 又扣了一次，导致 NDVI 饱和（≈1.0）、
-- 裸土 NDVI 偏高。v5（本 PR）起不再重复扣除。
-- 判定：algorithm_version ∈ {v3, v4} 且 stac_item_id 形如 S2B_50SLJ_20260915_0_L2A
-- （Earth Search l2a 命名；PC / sentinel-2-c1-l2a 的 id 不同，且确实需要 -0.1）。
-- 衍生的 UnCRtainTS 去云景（相邻景作为输入）一并列出，建议同批重算。
--
-- 用法：psql "$DATABASE_URL" -f scripts/20261009_earthsearch_boa_offset_affected.sql

\echo '== 1. 受影响的光学景汇总 =='
SELECT count(*)                AS affected_scenes,
       count(DISTINCT land_id) AS affected_lands,
       min(date)               AS first_date,
       max(date)               AS last_date
FROM agric_satellite.parcel_scene_products
WHERE sensor = 'S2'
  AND pixel_data->>'algorithm_version' IN ('stac-optical-lonlat-v3', 'stac-optical-lonlat-v4')
  AND pixel_data->>'stac_item_id' ~ '^S2[A-D]_[0-9]{1,2}[A-Z]{3}_[0-9]{8}_[0-9]+_L2A$';

\echo '== 2. 按地块：受影响景数、日期范围、同地块去云景数（用于组 backfill 请求） =='
WITH affected AS (
    SELECT land_id, date
    FROM agric_satellite.parcel_scene_products
    WHERE sensor = 'S2'
      AND pixel_data->>'algorithm_version' IN ('stac-optical-lonlat-v3', 'stac-optical-lonlat-v4')
      AND pixel_data->>'stac_item_id' ~ '^S2[A-D]_[0-9]{1,2}[A-Z]{3}_[0-9]{8}_[0-9]+_L2A$'
), per_land AS (
    SELECT land_id, count(*) AS scenes, min(date) AS d0, max(date) AS d1
    FROM affected GROUP BY land_id
)
SELECT p.land_id, p.scenes, p.d0, p.d1,
       (SELECT count(*) FROM agric_satellite.parcel_scene_products s
         WHERE s.land_id = p.land_id AND s.sensor = 'S2'
           AND s.pixel_data->>'algorithm_version' ILIKE 'uncrtaints%'
           AND s.date BETWEEN p.d0 AND p.d1) AS decloud_scenes_in_range
FROM per_land p
ORDER BY p.scenes DESC, p.land_id;

\echo '== 3. 按年月分布 =='
SELECT to_char(date, 'YYYY-MM') AS month, count(*) AS scenes, count(DISTINCT land_id) AS lands
FROM agric_satellite.parcel_scene_products
WHERE sensor = 'S2'
  AND pixel_data->>'algorithm_version' IN ('stac-optical-lonlat-v3', 'stac-optical-lonlat-v4')
  AND pixel_data->>'stac_item_id' ~ '^S2[A-D]_[0-9]{1,2}[A-Z]{3}_[0-9]{8}_[0-9]+_L2A$'
GROUP BY 1 ORDER BY 1;
