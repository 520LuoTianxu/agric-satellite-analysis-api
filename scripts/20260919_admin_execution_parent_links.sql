-- 管理执行监控任务树关联优化。
-- 请先执行本脚本，再部署 API；应用启动不会自动执行 DDL。
BEGIN;

ALTER TABLE agric_satellite.jobs
    ADD COLUMN IF NOT EXISTS parent_job_id uuid;
ALTER TABLE agric_satellite.work_items
    ADD COLUMN IF NOT EXISTS parent_job_id uuid;

CREATE INDEX IF NOT EXISTS ix_jobs_parent_job_id
    ON agric_satellite.jobs (parent_job_id);
CREATE INDEX IF NOT EXISTS ix_work_items_parent_job_id
    ON agric_satellite.work_items (parent_job_id);
CREATE INDEX IF NOT EXISTS ix_jobs_status
    ON agric_satellite.jobs (status);
CREATE INDEX IF NOT EXISTS ix_work_items_status
    ON agric_satellite.work_items (status);

-- 历史 Job 的父子关系原先写在 params_json，先迁入索引列。
UPDATE agric_satellite.jobs AS child
SET parent_job_id = candidate.parent_id
FROM (
    SELECT source.id,
        CASE
            WHEN (source.params_json ->> 'overview_run_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN (source.params_json ->> 'overview_run_id')::uuid
            WHEN (source.params_json ->> 'parent_job_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN (source.params_json ->> 'parent_job_id')::uuid
            WHEN (source.params_json ->> 'parent_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN (source.params_json ->> 'parent_id')::uuid
            ELSE NULL
        END AS parent_id
    FROM agric_satellite.jobs AS source
    WHERE source.parent_job_id IS NULL
      AND source.params_json IS NOT NULL
) AS candidate
JOIN agric_satellite.jobs AS parent ON parent.id = candidate.parent_id
WHERE child.id = candidate.id
  AND child.id <> candidate.parent_id;

-- 历史 WorkItem 的父任务通常在 payload.extras.job_id，兼容旧载荷的多种位置。
UPDATE agric_satellite.work_items AS item
SET parent_job_id = candidate.parent_id
FROM (
    SELECT source.id, (candidate.value)::uuid AS parent_id
    FROM agric_satellite.work_items AS source
    CROSS JOIN LATERAL (
        SELECT candidate_values.value
        FROM (
            VALUES
                (source.payload_json ->> 'job_id'),
                (source.payload_json ->> 'parent_job_id'),
                (source.payload_json -> 'extras' ->> 'job_id'),
                (source.payload_json -> 'extras' ->> 'parent_job_id'),
                (source.payload_json -> 'extras' ->> 'sentinel_job_id'),
                (source.payload_json -> 'extras' ->> 'bridge_job_id')
        ) AS candidate_values(value)
        WHERE candidate_values.value ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        LIMIT 1
    ) AS candidate
    WHERE source.parent_job_id IS NULL
      AND source.payload_json IS NOT NULL
) AS candidate
JOIN agric_satellite.jobs AS parent ON parent.id = candidate.parent_id
WHERE item.id = candidate.id
  AND item.parent_job_id IS NULL;

COMMENT ON COLUMN agric_satellite.jobs.parent_job_id IS
    '任务树父 Job；历史数据由 params_json 迁移，管理查询按此列建树';
COMMENT ON COLUMN agric_satellite.work_items.parent_job_id IS
    'WorkItem 所属 Job；历史数据由 payload_json 迁移，管理查询按此列建树';

COMMIT;
