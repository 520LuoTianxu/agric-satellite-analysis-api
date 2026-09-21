-- 持久化 WorkItem 最近一次成功 claim 的下载机身份。
-- 请先执行本脚本，再部署 API；应用启动不会自动执行 DDL。
BEGIN;

ALTER TABLE agric_satellite.work_items
    ADD COLUMN IF NOT EXISTS last_claimed_by text;

-- 迁移时保留仍处于租约中的当前领取机；已完成历史任务的临时 lease_owner 已无法恢复。
UPDATE agric_satellite.work_items
SET last_claimed_by = lease_owner
WHERE last_claimed_by IS NULL
  AND lease_owner IS NOT NULL;

COMMENT ON COLUMN agric_satellite.work_items.last_claimed_by IS
    '最近一次成功领取该 WorkItem 的下载机；不随租约释放、完成或失败清除';

COMMIT;
