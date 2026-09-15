-- OSS 场景产品导入工具的 schema 校验入口
--
-- 地块和场景产品的 DDL 只维护在 scripts/agri_seed/001_agri_schema.sql 以及
-- Alembic 迁移中。本文件故意不再 CREATE parcel_scene_products，避免这个工具
-- 重新创建一套旧场景产品结构，造成第二套地块身份和重复表设计。

DO $$
BEGIN
  IF to_regclass('agric_satellite.land_parcels') IS NULL THEN
    RAISE EXCEPTION
      'missing canonical table agric_satellite.land_parcels; apply the canonical schema first';
  END IF;

  IF to_regclass('agric_satellite.parcel_scene_products') IS NULL THEN
    RAISE EXCEPTION
      'missing canonical table agric_satellite.parcel_scene_products; apply the canonical schema first';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_attribute AS a
    JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = 'agric_satellite'
      AND c.relname = 'parcel_scene_products'
      AND a.attname = 'land_id'
      AND a.attnum > 0
      AND NOT a.attisdropped
  ) THEN
    RAISE EXCEPTION
      'canonical table agric_satellite.parcel_scene_products must use land_id';
  END IF;
END
$$;
