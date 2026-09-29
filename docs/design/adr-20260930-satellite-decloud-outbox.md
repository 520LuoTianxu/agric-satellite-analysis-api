# ADR：遥感迟到去云排程使用持久化 Outbox

- 状态：已采纳，待测试环境故障注入验收
- 日期：2026-09-30

## 背景

遥感批次在父任务封口后可能仍有 S2 worker 完成原始产品发布。迟到 worker 会尝试调用 `schedule_decloud_after_raw`。目前 Celery 发布失败最多按当前环境默认重试 3 次，异常随后仅记日志；原始产品已发布后，普通批次可能因日期已存在而跳过它，不能可靠恢复去云排程。软超时重入也需要重复处理同一恢复边界。

## 决策

在 API PostgreSQL 中增加 `satellite_decloud_schedule_outbox`，以稳定 `schedule_key` 保存每地块的一次排程意图，包括原始场景的最小质量元数据、日期窗口和作物季节。遥感 worker 通过受内部令牌保护的 API 写入意图，不直接连接数据库。

排程由 API 事务写入后再异步触发；下载机 Beat 每分钟扫描到期记录作为兜底。dispatcher 使用 `FOR UPDATE SKIP LOCKED` 租约领取，调用现有 `schedule_decloud_after_raw`，成功后确认，失败则记录错误并按指数退避重试。迟到单景和父线程批次快照分别生成稳定幂等键。

## 语义与权衡

- 提供至少一次排程；Celery 任务无法与 PostgreSQL 完成状态组成同一事务，worker 在成功投递后、确认 outbox 前崩溃时可能重复投递。
- 场景产品仍由现有地块/日期/传感器/场景唯一键保护入库幂等；重复去云计算的额外成本由测试环境监控，当前不承诺 exactly-once。
- 不把大段原始回执重新塞入 `jobs.progress_json`，避免增长和并发合并覆盖；不依赖有限次数的消息发布重试作为唯一恢复方式。
- 新迁移必须先于 API、ingest worker 和 Beat 部署。未部署迁移时 outbox 写入会失败，遥感主任务仍会保留现有直接排程的 best-effort 回退并显式记录错误。

## 验收

在测试环境注入以下故障：首次 Celery 派发失败、去云 planner 抛错、dispatcher 在队列投递后但确认前退出、租约过期回收；确认 pending 行最终完成、失败有可追溯记录、迟到 S2 能补排且重复排程不会重复污染正式产品。并观察重试队列积压和重复计算成本。
