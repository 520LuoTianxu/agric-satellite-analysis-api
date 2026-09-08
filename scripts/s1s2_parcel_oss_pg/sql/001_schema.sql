-- S1/S2 地块场景产品：OSS JSON → PostgreSQL
-- 应用：psql "$PGDATABASE" -f sql/001_schema.sql
-- 或：python scripts/oss_to_pg.py --apply-schema ...

CREATE SCHEMA IF NOT EXISTS agri;

CREATE TABLE IF NOT EXISTS agri.parcel_scene_products (
  parcel_id               text        NOT NULL,
  tile_id                 text        NOT NULL,
  date                    date        NOT NULL,
  sensor                  text        NOT NULL,  -- S1 | S2
  scene_id                text        NOT NULL DEFAULT '',
  land_name               text,
  cloud_cover             double precision,
  cloud_cover_over_30     boolean,
  parcel_cloud_cover_pct  double precision,
  json_oss_key            text,
  json_url                text,
  rgb_url                 text,
  large_rgb_url           text,
  heatmap_url             text,
  s2_heatmap_url          text,
  vv_url                  text,
  vh_url                  text,
  pixel_count             integer,
  clear_pixel_count       integer,
  res_m                   double precision,
  epsg                    integer,
  payload                 jsonb       NOT NULL,  -- 完整 JSON（含 pixels）
  generated_at_shanghai   text,                  -- 产品侧上海时区字符串；亦可存 timestamptz
  ingested_at             timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (parcel_id, date, sensor, scene_id)
);

CREATE INDEX IF NOT EXISTS idx_psp_tile_date
  ON agri.parcel_scene_products (tile_id, date);
CREATE INDEX IF NOT EXISTS idx_psp_sensor
  ON agri.parcel_scene_products (sensor);
CREATE INDEX IF NOT EXISTS idx_psp_date
  ON agri.parcel_scene_products (date);
CREATE INDEX IF NOT EXISTS idx_psp_payload_gin
  ON agri.parcel_scene_products USING gin (payload);

-- 轻量视图：去掉重型 pixels，便于列表/元数据查询
CREATE OR REPLACE VIEW agri.v_parcel_scene_products_meta AS
SELECT
  parcel_id,
  tile_id,
  date,
  sensor,
  scene_id,
  land_name,
  cloud_cover,
  cloud_cover_over_30,
  parcel_cloud_cover_pct,
  json_oss_key,
  json_url,
  rgb_url,
  large_rgb_url,
  heatmap_url,
  s2_heatmap_url,
  vv_url,
  vh_url,
  pixel_count,
  clear_pixel_count,
  res_m,
  epsg,
  payload - 'pixels' AS payload_meta,
  generated_at_shanghai,
  ingested_at
FROM agri.parcel_scene_products;

-- 兼容：public 同名视图（指向 agri 表）
CREATE OR REPLACE VIEW public.v_parcel_scene_products_meta AS
SELECT * FROM agri.v_parcel_scene_products_meta;

-- 可选：入库运行日志
CREATE TABLE IF NOT EXISTS agri.ingest_runs (
  run_id        bigserial PRIMARY KEY,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  oss_prefix    text,
  oss_bucket    text,
  limit_n       integer,
  listed_n      integer,
  upserted_n    integer,
  error_n       integer,
  dry_run       boolean DEFAULT false,
  notes         text,
  status        text DEFAULT 'running'  -- running | ok | error
);

COMMENT ON TABLE agri.parcel_scene_products IS
  'Sentinel-1/2 地块场景产品（自 Aliyun OSS JSON 入库）';
COMMENT ON COLUMN agri.parcel_scene_products.payload IS
  '完整产品 JSON，含 pixels 数组；列表查询请用 v_parcel_scene_products_meta';
