-- Additive: scene-level NDTI avg/min/max for straw/residue (B11/B12).
-- Pixel values live in lonlat_columns_v1 columns.NDTI; these scalar columns mirror other indices.

ALTER TABLE agric_satellite.parcel_scene_products
    ADD COLUMN IF NOT EXISTS ndti_avg double precision;

ALTER TABLE agric_satellite.parcel_scene_products
    ADD COLUMN IF NOT EXISTS ndti_min double precision;

ALTER TABLE agric_satellite.parcel_scene_products
    ADD COLUMN IF NOT EXISTS ndti_max double precision;

COMMENT ON COLUMN agric_satellite.parcel_scene_products.ndti_avg IS 'NDTI 像元平均值 (B11-B12)/(B11+B12)';
COMMENT ON COLUMN agric_satellite.parcel_scene_products.ndti_min IS 'NDTI 像元最小值';
COMMENT ON COLUMN agric_satellite.parcel_scene_products.ndti_max IS 'NDTI 像元最大值';
