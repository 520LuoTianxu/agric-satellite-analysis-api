-- 只读执行后验收。不创建、不修改、不删除任何对象。
-- 用于确认表/约束/索引与迁移脚本一致，且未改动 jobs 结构、未写入签名 URL 列。

SELECT current_database() AS database,
       current_user AS current_user,
       current_setting('server_version') AS server_version,
       current_setting('lock_timeout') AS lock_timeout,
       current_setting('statement_timeout') AS statement_timeout;

-- 若在新会话中运行，lock_timeout / statement_timeout 应仍是库默认值
-- （迁移里的 SET LOCAL 不应泄漏到会话或全局）。

SELECT c.relname AS table_name,
       pg_get_userbyid(c.relowner) AS table_owner,
       c.reltuples::bigint AS planner_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'agric_satellite'
  AND c.relname = 'biweekly_official_jobs'
  AND c.relkind = 'r';

SELECT a.attname AS column_name,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null,
       pg_get_expr(ad.adbin, ad.adrelid) AS column_default
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE n.nspname = 'agric_satellite'
  AND c.relname = 'biweekly_official_jobs'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attnum;

SELECT conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'agric_satellite.biweekly_official_jobs'::regclass
ORDER BY conname;

SELECT c.relname AS index_name,
       pg_get_indexdef(c.oid) AS definition
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_index i ON i.indexrelid = c.oid
WHERE n.nspname = 'agric_satellite'
  AND c.relname IN (
      'biweekly_official_jobs_pkey',
      'ix_biweekly_official_jobs_period',
      'ix_biweekly_official_jobs_report_job'
  )
ORDER BY c.relname;

-- 验收期望：
-- 列：job_id/land_id/season_id/period_id/kind/status text，
--     report_job_id uuid 可空，artifact_json jsonb 可空，job_json jsonb 非空，
--     created_at/updated_at timestamptz 非空。
-- 主键：biweekly_official_jobs_pkey (job_id)
-- 外键：biweekly_official_jobs_report_job_id_fkey → jobs(id) ON DELETE SET NULL
-- 检查：job_id 格式、status、kind、artifact_json 禁止 url/signed_url/presigned_url/download_url/href
-- 索引：period (land_id, season_id, period_id, created_at)；
--       report_job 部分索引 WHERE report_job_id IS NOT NULL
-- 新表初始行数应为 0；本迁移不得在 jobs 上增删改列。

SELECT count(*) AS official_job_rows
FROM agric_satellite.biweekly_official_jobs;

SELECT a.attname
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'agric_satellite'
  AND c.relname = 'jobs'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attnum;

-- 当前角色在新表上的权限，以及新表已授予的角色（用于对照 API 账号，不要猜测名字）。
SELECT has_table_privilege(current_user, 'agric_satellite.biweekly_official_jobs', 'SELECT') AS can_select,
       has_table_privilege(current_user, 'agric_satellite.biweekly_official_jobs', 'INSERT') AS can_insert,
       has_table_privilege(current_user, 'agric_satellite.biweekly_official_jobs', 'UPDATE') AS can_update,
       has_table_privilege(current_user, 'agric_satellite.biweekly_official_jobs', 'DELETE') AS can_delete,
       has_table_privilege(current_user, 'agric_satellite.biweekly_official_jobs', 'TRUNCATE') AS can_truncate;

SELECT grantee, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'agric_satellite'
  AND table_name = 'biweekly_official_jobs'
ORDER BY grantee, privilege_type;

-- 模板（请将 <api_role> 换成上面与 jobs 对照后确认的 API 账号，由有权角色另行执行；本验收文件不授权）：
-- GRANT SELECT, INSERT, UPDATE, DELETE ON agric_satellite.biweekly_official_jobs TO <api_role>;
