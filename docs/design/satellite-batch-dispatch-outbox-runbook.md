# 通用卫星批任务派发恢复说明

`satellite_batch_dispatch_outbox`与通用`satellite_batch`子Job在同一个PostgreSQL事务中创建。legacy/dual模式下，API后台dispatcher通过`FOR UPDATE SKIP LOCKED`领取派发意图；MQ发布失败按5秒起步、指数增长且最多1小时重试，API进程异常退出后由过期租约恢复。每轮扫描先将已进入`running`的Job协调为已派发，并丢弃已终结Job的未派发意图，降低MQ确认状态不确定时的重复发送。claim模式不读取此表，而是在创建Job的事务中批量写入WorkItem。由外部Beat读取任务清单的`/internal/schedule`接口显式关闭API Outbox，避免同一批Job被两条调度链路重复投递。

## 部署顺序

1. 先在目标API数据库应用`20260929_satellite_batch_dispatch_outbox.sql`，再部署包含dispatcher的API；缺表时legacy/dual任务创建事务会失败。
2. 多API实例可同时运行dispatcher；行锁和租约负责领取协调。各API实例必须使用一致的`WORK_QUEUE_MODE`。
3. claim模式无需启动Outbox扫描；如果之后切换到legacy/dual，必须在切换前确认该表已经创建。

在测试环境应用结构变更的示例：

```powershell
python scripts/upgrade_schema.py --env-file ABflow/.env.test --sql scripts/20260929_satellite_batch_dispatch_outbox.sql
```

先确认环境文件对应目标数据库；不要把数据库连接串或MQ凭据写进命令行历史、日志或代码仓库。

## 派发语义与观测

- 查询表中的`status`、`attempts`、`available_at`和`last_error`可观察积压、退避与最终派发状态。`last_error`是服务端诊断内容，不应暴露到普通任务详情。
- 主要日志事件为`satellite_batch_dispatch_completed`、`satellite_batch_dispatch_retry_scheduled`、`satellite_batch_dispatch_retry_lease_lost`和`satellite_batch_dispatch_outbox_scan_failed`。
- MQ确认结果不确定且Worker尚未把Job改为`running`时仍可能再次发布同一个Job ID。因此语义是至少一次；下载/计算可能重复，结果入库幂等性和并发副作用仍需故障注入确认。
- Outbox持续退避重试，不因暂时的MQ错误把Job标成计算失败。Job进度会显示`retrying`、派发尝试数与下次派发时间；原始错误只保存在Outbox。
- 迁移前已存在、没有Outbox记录的legacy/dual Job不会被自动扫描或重发，避免重复处理历史消息；需要人工依据任务状态核对。

## 测试环境验收与回退

测试环境应核验正常派发、MQ暂不可用后的恢复、dispatcher进程中断后的租约重领、多API实例并发领取，以及“MQ已确认但Outbox尚未完成”时重复投递对入库结果的影响。静态检查不能代替这些故障注入和实际遥感数据验收。

回退代码前先停止新API；Outbox表可保留，旧API不会读取它。回退到不包含此实现的API后，legacy/dual会恢复为提交后逐条发布，数据库提交到MQ确认之间的崩溃窗口也会重新出现。
