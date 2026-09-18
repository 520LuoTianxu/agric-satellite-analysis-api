-- 队列拆分：管理员心跳默认汇报 CPU/编排队列，旧 ingest 仅作为迁移期兼容队列。
ALTER TABLE agric_satellite.download_workers
    ALTER COLUMN queue_name SET DEFAULT 'cpu_compute';
