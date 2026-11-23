-- 只读执行前检查。不创建、不修改、不删除任何对象。
-- 在即将执行 20261123_biweekly_official_jobs.sql 的同一数据库上运行。
-- 用于确认：库/schema/jobs.id、权限、是否已有冲突表、当前超时设置。
-- 本文件不含口令，也不猜测 API 账号名。

SELECT current_database() AS database,
       current_user AS current_user,
       session_user AS session_user,
       current_setting('server_version') AS server_version,
       current_setting('server_version_num') AS server_version_num,
       current_setting('lock_timeout') AS lock_timeout,
       current_setting('statement_timeout') AS statement_timeout,
       current_setting('search_path') AS search_path;

SELECT nspname AS schema_name,
       pg_get_userbyid(nspowner) AS schema_owner,
       has_schema_privilege(current_user, 'agric_satellite', 'USAGE') AS current_user_usage,
       has_schema_privilege(current_user, 'agric_satellite', 'CREATE') AS current_user_create
FROM pg_namespace
WHERE nspname = 'agric_satellite';

-- 若上面没有行：schema 不存在，迁移脚本会显式失败，不要先在生产建 schema。

SELECT c.relname AS table_name,
       pg_get_userbyid(c.relowner) AS table_owner,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS id_type,
       has_table_privilege(current_user, 'agric_satellite.jobs', 'REFERENCES') AS current_user_references,
       has_table_privilege(current_user, 'agric_satellite.jobs', 'SELECT') AS current_user_select
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attribute a
  ON a.attrelid = c.oid AND a.attname = 'id' AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = 'agric_satellite'
  AND c.relname = 'jobs'
  AND c.relkind = 'r';

-- 若 jobs 不存在或 id 不是 uuid，迁移脚本会显式失败。
-- 创建带外键的新表需要当前角色对 jobs 具有 REFERENCES，或为表/库的所有者。

SELECT EXISTS (
    SELECT 1
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'agric_satellite'
      AND c.relname = 'biweekly_official_jobs'
      AND c.relkind = 'r'
) AS official_table_exists;

SELECT a.attname AS column_name,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull AS not_null
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'agric_satellite'
  AND c.relname = 'biweekly_official_jobs'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY a.attnum;

SELECT conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = (
    SELECT c.oid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'agric_satellite' AND c.relname = 'biweekly_official_jobs'
)
ORDER BY conname;

-- 对照 API 业务账号：列出已经在 jobs 上拥有 DML 的角色（不要猜测名字）。
-- 迁移成功后，通常需要由有权角色给同一个（或确认后的）账号授予新表 DML。
-- 模板（请替换 <api_role>，不要未核对就执行）：
--   GRANT SELECT, INSERT, UPDATE, DELETE ON agric_satellite.biweekly_official_jobs TO <api_role>;
SELECT grantee, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'agric_satellite'
  AND table_name = 'jobs'
  AND privilege_type IN ('SELECT', 'INSERT', 'UPDATE', 'DELETE')
ORDER BY grantee, privilege_type;
