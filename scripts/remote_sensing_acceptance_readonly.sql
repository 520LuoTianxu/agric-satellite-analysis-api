-- 遥感测试环境只读验收查询；仅查看版本、媒体key覆盖率和索引状态。
-- 这些统计不能替代地面样本/官方参考产品对绝对定标和分类阈值的验证。
BEGIN READ ONLY;

-- 1. 检查报告支持的近十年内，各传感器算法版本与S1定标方法分布。
SELECT
    sensor,
    COALESCE(NULLIF(pixel_data->>'algorithm_version', ''), 'unknown') AS algorithm_version,
    CASE
        WHEN sensor = 'S1' THEN COALESCE(
            NULLIF(pixel_data->'radiometric_calibration'->>'method', ''),
            'unknown'
        )
        ELSE 'not_applicable'
    END AS calibration_method,
    COUNT(*) AS scene_count,
    MIN(date) AS first_scene_date,
    MAX(date) AS last_scene_date
FROM agric_satellite.parcel_scene_products
WHERE sensor IN ('S1', 'S2')
  AND date >= CURRENT_DATE - INTERVAL '10 years'
GROUP BY sensor, algorithm_version, calibration_method
ORDER BY sensor, algorithm_version, calibration_method;

-- 2. 按月份看S1旧定标/新Sigma0 LUT产品是否仍混在同一历史窗口中。
SELECT
    DATE_TRUNC('month', date)::date AS scene_month,
    COALESCE(NULLIF(pixel_data->>'algorithm_version', ''), 'unknown') AS algorithm_version,
    COALESCE(
        NULLIF(pixel_data->'radiometric_calibration'->>'method', ''),
        'unknown'
    ) AS calibration_method,
    COUNT(*) AS scene_count
FROM agric_satellite.parcel_scene_products
WHERE sensor = 'S1'
  AND date >= CURRENT_DATE - INTERVAL '3 years'
GROUP BY scene_month, algorithm_version, calibration_method
ORDER BY scene_month DESC, algorithm_version, calibration_method;

-- 3. 估算报告媒体旧JSON回退范围；无rgb_oss_key的行可能仍需要兼容读取。
SELECT
    sensor,
    COUNT(*) AS scene_count,
    COUNT(*) FILTER (WHERE NULLIF(BTRIM(rgb_oss_key), '') IS NOT NULL) AS keyed_scene_count,
    COUNT(*) FILTER (
        WHERE NULLIF(BTRIM(rgb_oss_key), '') IS NULL
          AND NULLIF(BTRIM(json_oss_key), '') IS NOT NULL
    ) AS legacy_json_fallback_count,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE NULLIF(BTRIM(rgb_oss_key), '') IS NOT NULL)
        / NULLIF(COUNT(*), 0),
        2
    ) AS rgb_key_coverage_pct
FROM agric_satellite.parcel_scene_products
WHERE sensor IN ('S1', 'S2')
  AND date >= CURRENT_DATE - INTERVAL '10 years'
GROUP BY sensor
ORDER BY sensor;

-- 4. 检查历史RasterLayer质量分方法的版本覆盖率；unknown需按旧口径兼容。
SELECT
    satellite,
    layer_type,
    COALESCE(NULLIF(provenance_json->>'quality_score_method', ''), 'unknown') AS quality_score_method,
    COUNT(*) AS layer_count,
    MIN(date) AS first_layer_date,
    MAX(date) AS last_layer_date
FROM agric_satellite.raster_layers
WHERE date >= CURRENT_DATE - INTERVAL '10 years'
GROUP BY satellite, layer_type, quality_score_method
ORDER BY satellite, layer_type, quality_score_method;

-- 5. 确认预警历史查询所需的复合索引存在、已就绪且有效。
SELECT
    idx.relname AS index_name,
    i.indisready AS is_ready,
    i.indisvalid AS is_valid,
    pg_get_indexdef(i.indexrelid) AS index_definition
FROM pg_class AS tbl
JOIN pg_namespace AS ns ON ns.oid = tbl.relnamespace
JOIN pg_index AS i ON i.indrelid = tbl.oid
JOIN pg_class AS idx ON idx.oid = i.indexrelid
WHERE ns.nspname = 'agric_satellite'
  AND tbl.relname = 'parcel_scene_products'
  AND idx.relname = 'idx_psp_land_sensor_date_monitoring';

-- 如需检查真实执行计划，替换为一个有较多S1历史的测试地块ID后单独运行：
-- EXPLAIN (ANALYZE, BUFFERS)
-- SELECT date, scene_id
-- FROM agric_satellite.parcel_scene_products
-- WHERE land_id = 'REPLACE_WITH_TEST_LAND_ID'
--   AND sensor = 'S1'
--   AND date >= CURRENT_DATE - INTERVAL '3 years'
-- ORDER BY date DESC, scene_id;

COMMIT;
