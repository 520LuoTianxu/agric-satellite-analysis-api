-- 项目监测查询优化：把场景筛选所需的元数据从大 JSONB 中提取为普通列。
-- 这样查询最新有效观测时无需反复解压 pixel_data，也可以使用复合索引。

ALTER TABLE agric_satellite.parcel_scene_products
    ADD COLUMN IF NOT EXISTS product_source text;

ALTER TABLE agric_satellite.parcel_scene_products
    ADD COLUMN IF NOT EXISTS decloud_quality text;

ALTER TABLE agric_satellite.parcel_scene_products
    ADD COLUMN IF NOT EXISTS parcel_cloud_source text;

COMMENT ON COLUMN agric_satellite.parcel_scene_products.product_source IS
    '场景产品来源；从 pixel_data.source 提取，供有效观测筛选使用';

COMMENT ON COLUMN agric_satellite.parcel_scene_products.decloud_quality IS
    '去云质量等级；从 pixel_data.decloud_quality 提取，供有效观测筛选使用';

COMMENT ON COLUMN agric_satellite.parcel_scene_products.parcel_cloud_source IS
    '地块云量计算来源；从 pixel_data.parcel_cloud_source 提取，供有效观测筛选使用';

-- 历史数据只在迁移时解析一次，后续请求直接读取标量列。
UPDATE agric_satellite.parcel_scene_products
SET product_source = NULLIF(pixel_data->>'source', ''),
    decloud_quality = NULLIF(pixel_data->>'decloud_quality', ''),
    parcel_cloud_source = NULLIF(pixel_data->>'parcel_cloud_source', '')
WHERE product_source IS DISTINCT FROM NULLIF(pixel_data->>'source', '')
   OR decloud_quality IS DISTINCT FROM NULLIF(pixel_data->>'decloud_quality', '')
   OR parcel_cloud_source IS DISTINCT FROM NULLIF(pixel_data->>'parcel_cloud_source', '');

-- equality 列放在前面、日期范围列放在后面，匹配地块最新场景的访问模式。
CREATE INDEX IF NOT EXISTS idx_psp_land_sensor_date_monitoring
    ON agric_satellite.parcel_scene_products (land_id, sensor, date DESC, scene_id);

ANALYZE agric_satellite.parcel_scene_products;
