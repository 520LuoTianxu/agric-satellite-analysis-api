-- 示例查询（在 schema 应用后执行）

-- 1) 按瓦片 + 日期 + 传感器列出元数据（不含 pixels）
SELECT parcel_id, date, sensor, scene_id, land_name,
       cloud_cover, parcel_cloud_cover_pct, pixel_count, clear_pixel_count,
       rgb_url, heatmap_url, json_oss_key
FROM agri.v_parcel_scene_products_meta
WHERE tile_id = 'p4079_t00001_a15526'
  AND date BETWEEN '2025-06-01' AND '2025-06-30'
  AND sensor = 'S2'
ORDER BY date, parcel_id
LIMIT 100;

-- 2) 按传感器统计行数
SELECT sensor, count(*) AS n, min(date) AS d_min, max(date) AS d_max
FROM agri.parcel_scene_products
GROUP BY sensor
ORDER BY sensor;

-- 3) 按瓦片统计产品数
SELECT tile_id, sensor, count(*) AS n
FROM agri.parcel_scene_products
GROUP BY tile_id, sensor
ORDER BY n DESC
LIMIT 50;

-- 4) 从 jsonb 抽取单个像元指标（示例：第一个像素的 NDVI）
SELECT parcel_id, date, scene_id,
       payload #>> '{pixels,0,ndvi}' AS first_pixel_ndvi,
       payload #>> '{pixels,0,lon}'  AS first_pixel_lon,
       payload #>> '{pixels,0,lat}'  AS first_pixel_lat
FROM agri.parcel_scene_products
WHERE sensor = 'S2'
  AND parcel_id = '15526'
ORDER BY date DESC
LIMIT 20;

-- 5) S1：抽取第一个像元 VV_db
SELECT parcel_id, date, scene_id,
       payload #>> '{pixels,0,vv_db}' AS first_vv_db,
       payload #>> '{pixels,0,vh_db}' AS first_vh_db
FROM agri.parcel_scene_products
WHERE sensor = 'S1'
ORDER BY date DESC
LIMIT 20;

-- 6) 最近入库批次
SELECT * FROM agri.ingest_runs ORDER BY started_at DESC LIMIT 10;
