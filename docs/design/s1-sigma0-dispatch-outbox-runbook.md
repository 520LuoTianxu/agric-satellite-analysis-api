# S1 Sigma0 回算派发恢复说明

`s1_sigma0_dispatch_outbox`与S1回算子Job在同一PostgreSQL事务中创建。API后台dispatcher使用`FOR UPDATE SKIP LOCKED`领取派发意图；MQ发布异常按5秒起步、指数增长且最多1小时重试，进程异常退出后由过期租约恢复。每轮扫描会先把已进入`running`的Job协调为已派发，并丢弃已终结Job的待派发意图，降低MQ确认状态不确定时的重复发送。该流程覆盖legacy/dual模式；claim模式继续在Job事务中原子创建WorkItem。

## 部署顺序

1. 先在目标API数据库应用Outbox SQL，再升级API代码；否则legacy/dual API的后台扫描会记录表缺失错误，S1回算也不能使用持久派发流程。
2. 多API实例可以同时运行dispatcher，数据库行锁和租约负责分发协调，不需要额外限制为单API副本。
3. 保持`WORK_QUEUE_MODE`在API实例间一致。claim模式不读取此Outbox；legacy/dual模式由API Outbox继续发布既有MQ消息格式。

在测试环境应用结构变更的示例：

```powershell
python scripts/upgrade_schema.py --env-file ABflow/.env.test --sql scripts/20260929_s1_sigma0_dispatch_outbox.sql
```

应用前确认环境文件对应目标数据库；不要把数据库连接串或MQ凭据写入命令行历史、日志或代码仓库。

## 派发语义与观测

- 通过PostgreSQL查询表中的`status`、`attempts`、`available_at`和`last_error`观察积压与退避；成功确认记为`completed`，派发前已终结的Job记为`discarded`。
- 日志事件包括`s1_sigma0_dispatch_completed`、`s1_sigma0_dispatch_retry_scheduled`、`s1_sigma0_dispatch_retry_lease_lost`及`s1_sigma0_dispatch_outbox_scan_failed`。
- MQ publisher confirm结果不确定、且Worker尚未将Job更新为`running`时，租约到期后仍可能再次发布同一Job ID，因此语义仍是至少一次派发；重复投递可能增加下载/计算开销。场景结果按地块、日期、传感器和场景唯一键冲突更新，能避免重复插入，但尚未通过测试环境故障注入证明重复worker不会造成额外副作用。
- Outbox不会自动将多次发布异常变为遥感Job失败，而是持续退避重试；需按`last_error`排查broker或配置问题。Job保持pending时也表示worker尚未开始执行。

## 验收与回退

测试环境应分别检查成功发布、MQ暂时不可用后的退避恢复、dispatcher进程被终止后的租约重领、多API实例并发领取，以及MQ已确认但Outbox确认被中断时的重复投递影响。当前静态检查不能代替这些故障注入与实际遥感数据验收。

回退代码前先停止新API；表可保留，旧API不会读取它。回退到不含Outbox实现的版本会恢复原有提交后逐条发布逻辑，故障恢复能力随之失效。
