-- farms.id / land_parcels.farm_id 从 UUID 改为文本，才能写入业务 group_id。
-- 应用启动不会执行本文件；由 scripts/remap_farms_from_group_csv.py 或
-- python scripts/upgrade_schema.py --sql scripts/20260917_farm_id_to_text.sql 执行。
BEGIN;

DO $$
DECLARE
    farm_id_type text;
    parcel_farm_id_type text;
    fk_name text;
BEGIN
    SELECT t.typname
      INTO farm_id_type
      FROM pg_catalog.pg_attribute AS a
      JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
      JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
     WHERE n.nspname = 'agric_satellite'
       AND c.relname = 'farms'
       AND a.attname = 'id'
       AND a.attnum > 0
       AND NOT a.attisdropped;

    SELECT t.typname
      INTO parcel_farm_id_type
      FROM pg_catalog.pg_attribute AS a
      JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
      JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
      JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
     WHERE n.nspname = 'agric_satellite'
       AND c.relname = 'land_parcels'
       AND a.attname = 'farm_id'
       AND a.attnum > 0
       AND NOT a.attisdropped;

    IF farm_id_type IS NULL THEN
        RAISE EXCEPTION 'missing agric_satellite.farms.id';
    END IF;

    IF farm_id_type = 'uuid' OR parcel_farm_id_type = 'uuid' THEN
        FOR fk_name IN
            SELECT con.conname
              FROM pg_catalog.pg_constraint AS con
              JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'agric_satellite'
               AND c.relname = 'land_parcels'
               AND con.contype = 'f'
               AND pg_catalog.pg_get_constraintdef(con.oid) ILIKE '%farm_id%'
        LOOP
            EXECUTE format(
                'ALTER TABLE agric_satellite.land_parcels DROP CONSTRAINT %I',
                fk_name
            );
        END LOOP;

        IF farm_id_type = 'uuid' THEN
            ALTER TABLE agric_satellite.farms
                ALTER COLUMN id TYPE varchar(64) USING id::text;
        END IF;

        IF parcel_farm_id_type = 'uuid' THEN
            ALTER TABLE agric_satellite.land_parcels
                ALTER COLUMN farm_id TYPE varchar(64) USING farm_id::text;
        END IF;
    END IF;

    IF NOT EXISTS (
        SELECT 1
          FROM pg_catalog.pg_constraint AS con
          JOIN pg_catalog.pg_class AS c ON c.oid = con.conrelid
          JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
         WHERE n.nspname = 'agric_satellite'
           AND c.relname = 'land_parcels'
           AND con.contype = 'f'
           AND pg_catalog.pg_get_constraintdef(con.oid) ILIKE '%farm_id%'
    ) THEN
        ALTER TABLE agric_satellite.land_parcels
            ADD CONSTRAINT land_parcels_farm_id_fkey
            FOREIGN KEY (farm_id)
            REFERENCES agric_satellite.farms(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

COMMENT ON COLUMN agric_satellite.farms.id IS '农场/种植项目容器主键；业务导入使用 group_id 文本，API 新建仍可用 UUID 文本';
COMMENT ON COLUMN agric_satellite.land_parcels.farm_id IS '可选归属 farms.id；CSV 导入后等于种植项目 group_id';

COMMIT;
