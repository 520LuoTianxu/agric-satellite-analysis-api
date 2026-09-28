-- VPA10 虚拟项目区增量结构。
--
-- 该脚本只新增字段、表和索引，不删除旧的 5×5 km 项目区及其数据。
-- 旧数据标记为 legacy-v1；新规划器写入 vpa10-greedy-v1，便于灰度和回滚。
-- 可重复执行，适用于 ABflow/.env.test 和后续发布环境。

ALTER TABLE agric_satellite.virtual_project_areas
    ADD COLUMN IF NOT EXISTS algorithm_version text,
    ADD COLUMN IF NOT EXISTS window_side_m double precision,
    ADD COLUMN IF NOT EXISTS window_shape text,
    ADD COLUMN IF NOT EXISTS planning_crs text,
    ADD COLUMN IF NOT EXISTS grid_crs text,
    ADD COLUMN IF NOT EXISTS center_x double precision,
    ADD COLUMN IF NOT EXISTS center_y double precision,
    ADD COLUMN IF NOT EXISTS status text,
    ADD COLUMN IF NOT EXISTS plan_hash text,
    ADD COLUMN IF NOT EXISTS data_from date,
    ADD COLUMN IF NOT EXISTS data_to date,
    ADD COLUMN IF NOT EXISTS manifest_oss_key text,
    ADD COLUMN IF NOT EXISTS manifest_sha256 text,
    ADD COLUMN IF NOT EXISTS data_ready_ratio double precision,
    ADD COLUMN IF NOT EXISTS last_planned_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_backfill_at timestamptz;

-- 现有项目区是旧源文件/5×5 km 数据，不能被新版本误认为已经具备 VPA10 缓存。
UPDATE agric_satellite.virtual_project_areas
SET algorithm_version = COALESCE(NULLIF(algorithm_version, ''), 'legacy-v1'),
    window_side_m = COALESCE(window_side_m, NULLIF(tile_width_m, 0), 5000),
    window_shape = COALESCE(NULLIF(window_shape, ''), 'square'),
    status = COALESCE(NULLIF(status, ''), 'legacy'),
    data_ready_ratio = COALESCE(data_ready_ratio, 0)
WHERE algorithm_version IS NULL
   OR window_side_m IS NULL
   OR window_shape IS NULL
   OR status IS NULL
   OR data_ready_ratio IS NULL;

ALTER TABLE agric_satellite.virtual_project_areas
    ALTER COLUMN algorithm_version SET DEFAULT 'vpa10-greedy-v1',
    ALTER COLUMN window_side_m SET DEFAULT 10000,
    ALTER COLUMN window_shape SET DEFAULT 'square',
    ALTER COLUMN status SET DEFAULT 'planning',
    ALTER COLUMN data_ready_ratio SET DEFAULT 0;

ALTER TABLE agric_satellite.virtual_project_area_lands
    ADD COLUMN IF NOT EXISTS algorithm_version text,
    ADD COLUMN IF NOT EXISTS assignment_status text,
    ADD COLUMN IF NOT EXISTS geometry_hash text,
    ADD COLUMN IF NOT EXISTS containment_verified boolean,
    ADD COLUMN IF NOT EXISTS assigned_by text,
    ADD COLUMN IF NOT EXISTS assigned_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_verified_at timestamptz;

UPDATE agric_satellite.virtual_project_area_lands
SET algorithm_version = COALESCE(NULLIF(algorithm_version, ''), 'legacy-v1'),
    assignment_status = COALESCE(NULLIF(assignment_status, ''), 'active'),
    containment_verified = COALESCE(containment_verified, false)
WHERE algorithm_version IS NULL
   OR assignment_status IS NULL
   OR containment_verified IS NULL;

ALTER TABLE agric_satellite.virtual_project_area_lands
    ALTER COLUMN algorithm_version SET DEFAULT 'vpa10-greedy-v1',
    ALTER COLUMN assignment_status SET DEFAULT 'active',
    ALTER COLUMN containment_verified SET DEFAULT false;

CREATE TABLE IF NOT EXISTS agric_satellite.virtual_project_area_assets (
    tile_id text NOT NULL,
    sensor text NOT NULL,
    scene_date date NOT NULL,
    scene_id text NOT NULL,
    asset_kind text NOT NULL,
    oss_key text NOT NULL,
    format text NOT NULL,
    compression text,
    grid_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    checksum text,
    byte_size bigint,
    status text NOT NULL DEFAULT 'pending',
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT virtual_project_area_assets_pkey
        PRIMARY KEY (tile_id, sensor, scene_date, scene_id, asset_kind),
    CONSTRAINT virtual_project_area_assets_tile_fk
        FOREIGN KEY (tile_id)
        REFERENCES agric_satellite.virtual_project_areas (tile_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT virtual_project_area_assets_sensor_ck
        CHECK (sensor IN ('S1', 'S2')),
    CONSTRAINT virtual_project_area_assets_status_ck
        CHECK (status IN ('pending', 'running', 'ready', 'partial', 'failed', 'stale')),
    CONSTRAINT virtual_project_area_assets_grid_object_ck
        CHECK (jsonb_typeof(grid_json) = 'object')
);

CREATE INDEX IF NOT EXISTS virtual_project_areas_algorithm_status_idx
    ON agric_satellite.virtual_project_areas (algorithm_version, status);

CREATE INDEX IF NOT EXISTS virtual_project_areas_plan_hash_idx
    ON agric_satellite.virtual_project_areas (plan_hash);

CREATE INDEX IF NOT EXISTS virtual_project_area_lands_land_version_idx
    ON agric_satellite.virtual_project_area_lands (land_id, algorithm_version);

CREATE UNIQUE INDEX IF NOT EXISTS virtual_project_area_lands_active_land_version_uq
    ON agric_satellite.virtual_project_area_lands (land_id, algorithm_version)
    WHERE assignment_status = 'active';

CREATE INDEX IF NOT EXISTS virtual_project_area_assets_status_idx
    ON agric_satellite.virtual_project_area_assets (status, sensor, scene_date);

CREATE INDEX IF NOT EXISTS virtual_project_area_assets_oss_key_idx
    ON agric_satellite.virtual_project_area_assets (oss_key);

COMMENT ON TABLE agric_satellite.virtual_project_area_assets IS
    'VPA10 项目区级影像/像素资产清单；地块结果从项目区资产裁剪派生';
COMMENT ON COLUMN agric_satellite.virtual_project_areas.algorithm_version IS
    '项目区规划算法版本；legacy-v1 为旧 5×5 km 数据，vpa10-greedy-v1 为新算法';
COMMENT ON COLUMN agric_satellite.virtual_project_areas.window_side_m IS
    '动态项目区正方形边长，单位为米；VPA10 为 10000';
COMMENT ON COLUMN agric_satellite.virtual_project_areas.manifest_oss_key IS
    '项目区历史资产 manifest 的 OSS key';
COMMENT ON COLUMN agric_satellite.virtual_project_area_assets.grid_json IS
    '该资产对应的 CRS、transform、分辨率、宽高和 nodata 网格元数据';
COMMENT ON COLUMN agric_satellite.virtual_project_area_lands.containment_verified IS
    '规划器是否验证项目区完整 covers 地块边界';
