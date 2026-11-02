-- 示例查询（在 schema 应用后执行）

-- 1) 按瓦片 + 日期 + 传感器列出元数据（不含 pixels）
SELECT land_id, date, sensor, scene_id, land_name,
       cloud_cover, parcel_cloud_cover_pct, pixel_count,
       rgb_url, json_oss_key
FROM agric_satellite.v_parcel_scene_products_meta
WHERE tile_id = 'p4079_t00001_a15526'
  AND date BETWEEN '2025-06-01' AND '2025-06-30'
  AND sensor = 'S2'
ORDER BY date, land_id
LIMIT 100;

-- 2) 按传感器统计行数
SELECT sensor, count(*) AS n, min(date) AS d_min, max(date) AS d_max
FROM agric_satellite.parcel_scene_products
GROUP BY sensor
ORDER BY sensor;

-- 3) 按瓦片统计产品数
SELECT tile_id, sensor, count(*) AS n
FROM agric_satellite.parcel_scene_products
GROUP BY tile_id, sensor
ORDER BY n DESC
LIMIT 50;

-- 4) 按 pixel_data.format 统计（迁移前 inventory）
SELECT COALESCE(pixel_data->>'format', '(null)') AS fmt, count(*) AS n
FROM agric_satellite.parcel_scene_products
WHERE pixel_data IS NOT NULL
GROUP BY 1
ORDER BY n DESC;

-- 5) 抽取第一个像元指标（兼容 lonlat_v1 对象数组与 lonlat_columns_v1 列式）
SELECT land_id, date, scene_id,
       pixel_data->>'format' AS fmt,
       CASE
         WHEN pixel_data->>'format' = 'lonlat_columns_v1'
         THEN pixel_data #>> '{columns,NDVI,0}'
         ELSE pixel_data #>> '{pixels,0,NDVI}'
       END AS first_pixel_ndvi,
       CASE
         WHEN pixel_data->>'format' = 'lonlat_columns_v1'
         THEN pixel_data #>> '{columns,lon,0}'
         ELSE pixel_data #>> '{pixels,0,lon}'
       END AS first_pixel_lon,
       CASE
         WHEN pixel_data->>'format' = 'lonlat_columns_v1'
         THEN pixel_data #>> '{columns,lat,0}'
         ELSE pixel_data #>> '{pixels,0,lat}'
       END AS first_pixel_lat
FROM agric_satellite.parcel_scene_products
WHERE sensor = 'S2'
  AND land_id = '15526'
ORDER BY date DESC
LIMIT 20;

-- 6) S1：抽取第一个像元 VV_db / VH_db（双格式）
SELECT land_id, date, scene_id,
       pixel_data->>'format' AS fmt,
       CASE
         WHEN pixel_data->>'format' = 'lonlat_columns_v1'
         THEN pixel_data #>> '{columns,VV_db,0}'
         ELSE COALESCE(pixel_data #>> '{pixels,0,VV_db}', pixel_data #>> '{pixels,0,vv_db}')
       END AS first_vv_db,
       CASE
         WHEN pixel_data->>'format' = 'lonlat_columns_v1'
         THEN pixel_data #>> '{columns,VH_db,0}'
         ELSE COALESCE(pixel_data #>> '{pixels,0,VH_db}', pixel_data #>> '{pixels,0,vh_db}')
       END AS first_vh_db
FROM agric_satellite.parcel_scene_products
WHERE sensor = 'S1'
ORDER BY date DESC
LIMIT 20;

-- 7) 最近入库批次
SELECT * FROM agric_satellite.ingest_runs ORDER BY started_at DESC LIMIT 10;
