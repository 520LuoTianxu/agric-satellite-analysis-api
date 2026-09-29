# S2 去云排程恢复运行说明

`satellite_decloud_schedule_outbox`持久保存已发布S2原始场景对应的去云规划意图。下载worker先经受内部令牌保护的API写入意图，再即时唤醒dispatcher；下载机Beat每分钟扫描一次，恢复broker唤醒失败、过期租约和暂时性排程异常。

## 部署顺序

1. 先在目标API数据库应用迁移，再升级API、共享包、ingest worker和Beat。不要在旧API尚未含Outbox接口时先启动新worker。
2. 下载机ingest需要配置`API_BASE_URL`与`INTERNAL_API_TOKEN`；Beat通过共享配置读取`SCHEDULE_DECLOUD_OUTBOX_ENABLED`。`docker-compose.download-machine.yml`默认开启该扫描，`.env`可显式覆盖。下载机仍只用本机Redis作Celery broker，不需要直连API数据库。
3. 保持单个Beat实例。即时派发由worker完成；Beat扫描开关只控制周期性恢复，不会关闭即时派发或停止写入新Outbox记录。

在测试环境应用迁移的示例：

```powershell
python scripts/upgrade_schema.py --env-file ABflow/.env.test --sql scripts/20260930_satellite_decloud_schedule_outbox.sql
```

正式运行前先用同一命令配合目标环境的受控环境文件核对数据库目标；不要把数据库连接串或内部令牌写进命令行历史、日志或代码仓库。

## 运行观测

- 内部接口`GET /v1/internal/decloud-schedules/health/pending`返回`pending`、`processing`、`completed`数量；请求需携带内部Bearer令牌。
- 查看Beat日志中的`decloud_schedule_outbox_sweep_finished`以及worker日志中的`decloud_schedule_outbox_completed`、`decloud_schedule_outbox_retry_scheduled`和`decloud_schedule_outbox_failure_report_failed`事件。
- 失败记录按5秒起步、指数增长、最多1小时的间隔重新开放领取；持续失败不会静默转成成功，需检查相应行的`last_error`、`attempts`和`available_at`。
- 该协议提供至少一次派发。worker可能在Celery任务成功入队后、确认Outbox前崩溃，重试会再次排程；监控去云队列积压及重复计算成本。下游结果已有场景唯一键保护，但不把消息派发宣称为exactly-once。

## 测试环境验收

分别注入即时Celery唤醒失败、planner异常、成功入队后确认请求失败和租约过期；确认任务最终被重试、Outbox状态有迹可查、重复执行没有重复污染正式场景产品。再抽查父批次快照、软超时和父任务封口后的迟到单景三条入口，确认S2去云规划均到达Outbox。还需记录去云worker队列积压、失败退避和重复计算情况。

## 回退

将`SCHEDULE_DECLOUD_OUTBOX_ENABLED`设为`false`可停止周期扫描，但不会撤销已经创建的行，也不会关闭即时唤醒。需要回退应用版本时先停止新worker和Beat，再回退API/ingest；Outbox表可保留，旧代码不会读取它。重新部署新版本前确认迁移仍在数据库中并重新开启扫描。
