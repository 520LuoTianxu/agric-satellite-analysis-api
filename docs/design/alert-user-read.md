# 预警个人已读

## 已确定的业务规则

- 租户标识是 `land_parcels.base_id`。
- `alert_reads` 以 `(base_id, user_id, alert_id)` 为主键；无记录即未读。
- 用户 ID 是农业服务 `/getInfo` 返回的 `user.userId`，不使用匿名用户、token 或账号角色 ID 作为用户主键。
- 单条和一键已读只影响当前用户。开启/关闭仍是预警处理状态，与阅读状态独立。
- 一键已读包含当前租户所有未读预警（含已关闭、其他分页），不受列表筛选限制。语句执行之后新增的预警仍未读。
- 页面展示预警数量超过 99 时显示 `99+`，真实统计与分页总数不截断。
- 原遥感重算路径保留同日同规则仍触发的预警 ID，防止重复重算丢失阅读记录；新日期的预警重新提醒。

## 发布顺序

1. 在卫星业务 PostgreSQL 手工执行 `scripts/20260917_alert_user_reads.sql`。
2. 部署 API 和 ingest 改动，再部署前端。应用不会自行建表。
3. 用同基地的两个账号验证各自未读，再用另一基地验证隔离。

SQL 使用 `IF NOT EXISTS`，可重复执行。已有预警无需回填：没有个人阅读记录就显示未读。脚本只新增表、索引及注释，不修改预警处理状态，不删除历史数据。大表建立索引会占用数据库资源，应按正常数据库变更窗口执行。

## 身份与接口

前端所有预警请求携带 `Authorization: Bearer <agricToken>` 和 `Hr-Base-Id`，基地来自当前联合登录数据的 `certifiedExternalSystems[systemType=2].systemId`。

后端使用现有 `CDFINANCE_SOIL_BASE_URL` 配置调用农业服务 `/getInfo` 验证用户，并查询 `/agriculture/baseinfo/list` 验证基地授权。需要确保此地址与前端农业登录环境一致，且账号能访问自己的基地列表。地址不包含凭证；不配置独立用户密码，不存储 token，不接受浏览器指定用户 ID。上游不可用时返回明确错误，不退回匿名访问。

- `GET /v1/alerts`：返回 `is_read`、`read_at`，可用 `is_read=true/false` 筛选。
- `GET /v1/alerts/summary`：`unread_total` 是当前用户、当前基地的全部未读；`open_total/high/medium/low` 是当前基地的未关闭统计。
- `POST /v1/alerts/{id}/read`：幂等标记本人已读，返回预警及阅读状态。
- `POST /v1/alerts/read-all`：幂等批量插入阅读记录，返回实际新增记录数 `marked_count`。
- 地块预警列表及关闭/重新打开接口也使用同一基地范围校验。

本次仅为预警接口接入上述身份边界，其余历史匿名接口不在此次改动范围。

## 验证

API：`python -m unittest tests.test_alert_reads -v`。采用隔离的内存数据库执行真实查询及 INSERT SELECT，覆盖个人隔离、租户隔离、批量超过分页、重复请求、新预警、关闭独立、失效登录和上游故障。PostgreSQL 的批量计数 CTE 需在部署环境联调验证；未运行生产数据库脚本。

Ingest：`python -m unittest tests.test_alert_read_preservation -v`，验证同日重算保留预警、新日期生成新记录。

前端：类型检查、ESLint、i18n 检查，以及 `scripts/verify-alert-reading-ui.py` 的隔离浏览器交互验证。
