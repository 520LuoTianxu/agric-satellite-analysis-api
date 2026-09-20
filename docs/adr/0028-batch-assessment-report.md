# ADR 0028：Smart 精确同步与批量选地报告共享遥感窗口

## 状态

已接受

## 背景

前端需要一次提交多个地块编号，并最终得到每个地块独立的选地分析报告。地块边界和部分业务属性以 Smart/MySQL 为源，不能让下载机直接访问 Smart 或 API PostgreSQL。逐地块拉取 Sentinel 数据会重复下载相同覆盖范围，增加等待时间和外部数据请求量。

## 决策

新增 `POST /v1/lands/assessment-reports/batch`，请求最多包含 1000 个地块编号，兼容 `landIdList`、`landIdlist` 和 `land_ids`。API 机先用参数化查询从 Smart 精确读取这些地块，并通过既有同步规范化逻辑 upsert 到 PostgreSQL；这一步不创建单地块遥感任务。

同步成功后，API 按地块边界用局部米制投影聚合 5×5 km 共享窗口，按传感器和日期分片创建 `satellite_batch` Job。每个地块另建一个 `assessment_report` Job，由 `land_bootstrap` 拉取天气、土壤后触发；报告通过内部数据接口检查共享遥感结果覆盖，因而每个地块仍生成自己的 PDF。

批次父 Job 保存分组、日期、传感器和子任务 ID，前端可通过 `GET /v1/lands/assessment-reports/batch/{batch_id}` 查询聚合状态和各地块报告状态。批次 ID、遥感 Job ID 和 follow-up task ID 使用稳定 UUID，重复提交同一请求不会创建重复批次。

## 结果与取舍

- 同一 5×5 km 窗口内的地块共享场景下载和解码，但仍逐地块掩膜、写入结果并生成报告。
- Smart 不存在、被调度规则过滤、边界无效或源库未启用时，批次不会部分创建，调用方得到明确的 404、422 或 503。
- 批量入口依赖 API 机配置 `MYSQL_SOURCE_ENABLED=true` 和 Smart 连接配置；下载机继续只通过内部 HTTP 读取任务与提交结果。
- `force=true`、日期窗口或传感器集合改变时会形成新的稳定批次 ID，调用方可以显式重新拉取。

## 未采用的方案

- 不在前端先读取地块边界再拼装请求：边界源数据和权限边界应由 API 机统一控制。
- 不为每个地块直接复用完整 `land_bootstrap` 遥感拉取：这会绕过共享 5×5 km 聚合并产生重复下载。
