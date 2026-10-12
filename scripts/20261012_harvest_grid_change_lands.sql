-- 只读报告：本季中途换过像元网格的地块（不写库）。
--
-- 背景：旧产品（algorithm_version 为空 / v3 / v4，经纬度网格）与
-- stac-optical-lonlat-v5（UTM 10m 网格）坐标完全不重合。收获像元图按坐标延续
-- 像元历史，同一季内混用两套网格时，新网格像元拿不到旧网格的返青/峰值历史，
-- 被判成非作物（255，热力图留白）。例：20308 在 09-24 前为旧网格 2809 像元，
-- 10-01 起为 v5 2690 像元。
-- API 端已加最近邻重映射兜底；根本修复是把旧网格日期按 v5 重下。
--
-- 判定：:season_from 起，同一地块 S2 景（剔除 UnCRtainTS 去云景）出现 ≥2 种
-- algorithm_version 或 ≥2 种 pixel_count；“旧网格”= algorithm_version 不等于
-- :latest_version 的景。
--
-- 用法：
--   psql "$DATABASE_URL" -v season_from="'2026-08-01'" \
--        -v latest_version="'stac-optical-lonlat-v5'" \
--        -f scripts/20261012_harvest_grid_change_lands.sql

\echo '== 1. 汇总 =='
WITH s AS (
    SELECT land_id, date, pixel_count,
           COALESCE(NULLIF(BTRIM(pixel_data->>'algorithm_version'), ''), '(legacy)') AS ver
    FROM agric_satellite.parcel_scene_products
    WHERE sensor = 'S2'
      AND date >= :season_from::date
      AND pixel_data->>'format' = 'lonlat_v1'
      AND COALESCE(pixel_data->>'algorithm_version', '') NOT LIKE 'uncrtaints%'
      AND scene_id NOT LIKE '%\_decloud' ESCAPE '\'
), lands AS (
    SELECT land_id
    FROM s
    GROUP BY land_id
    HAVING count(DISTINCT ver) > 1 OR count(DISTINCT pixel_count) > 1
)
SELECT count(*) AS affected_lands FROM lands;

\echo '== 2. 明细：land_id 与需重下的旧网格日期范围 =='
WITH s AS (
    SELECT land_id, date, pixel_count,
           COALESCE(NULLIF(BTRIM(pixel_data->>'algorithm_version'), ''), '(legacy)') AS ver
    FROM agric_satellite.parcel_scene_products
    WHERE sensor = 'S2'
      AND date >= :season_from::date
      AND pixel_data->>'format' = 'lonlat_v1'
      AND COALESCE(pixel_data->>'algorithm_version', '') NOT LIKE 'uncrtaints%'
      AND scene_id NOT LIKE '%\_decloud' ESCAPE '\'
)
SELECT land_id,
       array_agg(DISTINCT ver ORDER BY ver)                         AS versions,
       array_agg(DISTINCT pixel_count ORDER BY pixel_count)         AS pixel_counts,
       min(date) FILTER (WHERE ver <> :latest_version)              AS redownload_from,
       max(date) FILTER (WHERE ver <> :latest_version)              AS redownload_to,
       count(*)  FILTER (WHERE ver <> :latest_version)              AS old_grid_scenes,
       min(date) FILTER (WHERE ver = :latest_version)               AS first_latest_date
FROM s
GROUP BY land_id
HAVING count(DISTINCT ver) > 1 OR count(DISTINCT pixel_count) > 1
ORDER BY old_grid_scenes DESC NULLS LAST, land_id;
