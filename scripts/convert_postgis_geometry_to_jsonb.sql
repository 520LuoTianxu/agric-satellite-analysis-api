-- Convert leftover PostGIS geometry/geography columns to GeoJSON JSONB.
--
-- Apply this SQL outside the API container (DMS / psql / scripts/upgrade_schema.py).
-- API images only start uvicorn and must not run schema changes on startup.
--
-- Safe to re-run.
--
-- Typical leftover objects after the old 0025:
--   agric_satellite.fields.geom                      geometry -> jsonb
--   agric_satellite.scouting_observations.geom_point geometry -> jsonb
--   agric_satellite.detected_boundaries.geom         geometry -> jsonb
--   agric_satellite.land_parcels.geom                drop after backfill
--   GIST indexes on those columns
--   postgis / postgis_* extensions

BEGIN;

DO $upgrade$
DECLARE
    rec record;
    geom_expr text;
BEGIN
    -- 1) Backfill canonical parcel JSONB + bbox from leftover land_parcels.geom.
    IF to_regclass('agric_satellite.land_parcels') IS NOT NULL
       AND EXISTS (
            SELECT 1
            FROM pg_catalog.pg_attribute AS a
            JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
            WHERE n.nspname = 'agric_satellite'
              AND c.relname = 'land_parcels'
              AND a.attname = 'geom'
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND t.typname IN ('geometry', 'geography')
       )
       AND EXISTS (
            SELECT 1
            FROM pg_catalog.pg_proc AS p
            JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
            WHERE p.proname = 'st_asgeojson'
       )
    THEN
        EXECUTE $sql$
            UPDATE agric_satellite.land_parcels
            SET boundary_geojson = ST_AsGeoJSON(ST_Force2D(geom::geometry))::jsonb
            WHERE geom IS NOT NULL
              AND (
                    boundary_geojson IS NULL
                    OR jsonb_typeof(boundary_geojson) <> 'object'
                  )
        $sql$;

        IF EXISTS (
            SELECT 1
            FROM pg_catalog.pg_attribute AS a
            JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'agric_satellite'
              AND c.relname = 'land_parcels'
              AND a.attname IN ('min_lon', 'min_lat', 'max_lon', 'max_lat')
              AND a.attnum > 0
              AND NOT a.attisdropped
            GROUP BY c.oid
            HAVING count(*) = 4
        ) THEN
            EXECUTE $sql$
                UPDATE agric_satellite.land_parcels
                SET min_lon = ST_XMin(ST_Envelope(geom::geometry)::box3d),
                    min_lat = ST_YMin(ST_Envelope(geom::geometry)::box3d),
                    max_lon = ST_XMax(ST_Envelope(geom::geometry)::box3d),
                    max_lat = ST_YMax(ST_Envelope(geom::geometry)::box3d)
                WHERE geom IS NOT NULL
                  AND (
                        min_lon IS NULL
                        OR min_lat IS NULL
                        OR max_lon IS NULL
                        OR max_lat IS NULL
                      )
            $sql$;
        END IF;
    END IF;

    -- 2) Drop GIST / spatial indexes that would block ALTER TYPE.
    FOR rec IN
        SELECT DISTINCT pg_catalog.format('%I.%I', n.nspname, ic.relname) AS index_name
        FROM pg_catalog.pg_index AS i
        JOIN pg_catalog.pg_class AS tc ON tc.oid = i.indrelid
        JOIN pg_catalog.pg_class AS ic ON ic.oid = i.indexrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = tc.relnamespace
        JOIN pg_catalog.pg_attribute AS a
          ON a.attrelid = tc.oid
         AND a.attnum = ANY (i.indkey)
        JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
        WHERE n.nspname IN ('agric_satellite', 'public', 'agri')
          AND tc.relname NOT IN (
                'spatial_ref_sys',
                'geometry_columns',
                'geography_columns'
              )
          AND t.typname IN ('geometry', 'geography')
          AND a.attnum > 0
          AND NOT a.attisdropped
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS %s', rec.index_name);
        RAISE NOTICE 'dropped spatial index %', rec.index_name;
    END LOOP;

    -- 3) Convert remaining application geometry/geography columns to jsonb.
    FOR rec IN
        SELECT
            n.nspname AS schema_name,
            c.relname AS table_name,
            a.attname AS column_name
        FROM pg_catalog.pg_attribute AS a
        JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
        WHERE n.nspname IN ('agric_satellite', 'public', 'agri')
          AND c.relkind = 'r'
          AND c.relname NOT IN (
                'spatial_ref_sys',
                'geometry_columns',
                'geography_columns'
              )
          AND t.typname IN ('geometry', 'geography')
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY n.nspname, c.relname, a.attname
    LOOP
        IF rec.schema_name = 'agric_satellite'
           AND rec.table_name = 'land_parcels'
           AND rec.column_name = 'geom'
        THEN
            CONTINUE;
        END IF;

        geom_expr := format(
            'CASE WHEN %I IS NULL THEN NULL '
            'ELSE ST_AsGeoJSON(ST_Force2D(%I::geometry))::jsonb END',
            rec.column_name,
            rec.column_name
        );
        EXECUTE format(
            'ALTER TABLE %I.%I ALTER COLUMN %I TYPE jsonb USING %s',
            rec.schema_name,
            rec.table_name,
            rec.column_name,
            geom_expr
        );
        RAISE NOTICE 'converted %.%.% to jsonb',
            rec.schema_name, rec.table_name, rec.column_name;
    END LOOP;

    -- 4) Canonical parcels keep boundary_geojson only; drop leftover geom.
    IF to_regclass('agric_satellite.land_parcels') IS NOT NULL
       AND EXISTS (
            SELECT 1
            FROM pg_catalog.pg_attribute AS a
            JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'agric_satellite'
              AND c.relname = 'land_parcels'
              AND a.attname = 'geom'
              AND a.attnum > 0
              AND NOT a.attisdropped
       )
    THEN
        EXECUTE 'DROP INDEX IF EXISTS agric_satellite.land_parcels_geom_idx';
        EXECUTE 'ALTER TABLE agric_satellite.land_parcels DROP COLUMN geom';
        RAISE NOTICE 'dropped agric_satellite.land_parcels.geom';
    END IF;
END
$upgrade$;

COMMIT;

-- 5) Drop unused spatial extensions after application columns are JSONB.
--    Keep this outside the conversion transaction so a permission error does
--    not roll back the JSONB conversion.
DROP EXTENSION IF EXISTS postgis_tiger_geocoder;
DROP EXTENSION IF EXISTS postgis_topology;
DROP EXTENSION IF EXISTS postgis_raster;
DROP EXTENSION IF EXISTS postgis;

-- Remaining spatial columns should be empty after a successful run.
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    a.attname AS column_name,
    t.typname AS type_name
FROM pg_catalog.pg_attribute AS a
JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
WHERE n.nspname IN ('agric_satellite', 'public', 'agri')
  AND t.typname IN ('geometry', 'geography')
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY 1, 2, 3;
