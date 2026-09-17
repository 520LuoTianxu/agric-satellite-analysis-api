# 预警个人已读

## 已确定的业务规则

- 租户标识是 `land_parcels.base_id`。
- `alert_reads` 以 `(base_id, user_id, alert_id)` 为主键；无记录即未读。
- 用户 ID 取自 localStorage 的 `jointLoginData.accountRoleList[0].accountId`，不取 `accountRoleId`、当前选中角色或 token 中的值。
- 单条和一键已读只影响当前用户。开启/关闭仍是预警处理状态，与阅读状态独立。
- 一键已读包含当前租户所有未读预警（含已关闭、其他分页），不受列表筛选限制。语句执行之后新增的预警仍未读。
- 页面展示预警数量超过 99 时显示 `99+`，真实统计与分页总数不截断。
- 原遥感重算路径保留同日同规则仍触发的预警 ID，防止重复重算丢失阅读记录；新日期的预警重新提醒。

## 发布顺序

1. 在卫星业务 PostgreSQL 手工执行 `scripts/20260917_alert_user_reads.sql`。
2. 部署 API 和 ingest 改动，再部署前端。应用不会自行建表。
3. 用同基地的两个账号验证各自未读，再用另一基地验证隔离。

SQL 使用 `IF NOT EXISTS`，可重复执行。已有预警无需回填：没有个人阅读记录就显示未读。脚本只新增表、索引及注释，不修改预警处理状态，不删除历史数据。大表建立索引会占用数据库资源，应按正常数据库变更窗口执行。

改为使用 `accountId` 不改变表结构；已执行过建表脚本无需重新建表。旧版以农业 `userId` 写入的记录不会自动转换，若存在旧阅读记录，需核对账号映射后另行迁移。

## 身份与接口

前端所有预警请求携带 `Hr-Base-Id` 和 `X-Account-Id`，不传农业 token。基地来自当前联合登录数据的 `certifiedExternalSystems[systemType=2].systemId`，账号来自 `accountRoleList` 第一项的 `accountId`。例如基地为 `37`、第一项账号为 `209`，请求头即 `Hr-Base-Id: 37`、`X-Account-Id: 209`；无需上传整个登录对象及手机号等个人信息。

后端只校验两个标识是有效的正整数，然后按基地过滤预警、按账号记录个人已读。不解析或验证 token，不调用 `/getInfo`、`/agriculture/baseinfo/list`，预警接口不依赖 `CDFINANCE_SOIL_BASE_URL`。缺少或无效的标识返回 `400`，不会返回农业登录失效错误。此实现信任客户端传入的基地和账号，不验证真实身份或基地访问权限；需要的认证与授权应由入口或网关负责。

- `GET /v1/alerts`：返回 `is_read`、`read_at`，可用 `is_read=true/false` 筛选。
- `GET /v1/alerts/summary`：`unread_total` 是当前用户、当前基地的全部未读；`open_total/high/medium/low` 是当前基地的未关闭统计。
- `POST /v1/alerts/{id}/read`：幂等标记本人已读，返回预警及阅读状态。
- `POST /v1/alerts/read-all`：幂等批量插入阅读记录，返回实际新增记录数 `marked_count`。
- 地块预警列表及关闭/重新打开接口也使用同一基地范围校验。

未读统计缓存按 `(base_id, accountId)` 区分，农业 token 更新或角色变化不会改变个人已读归属。

## 验证

API：`python -m unittest tests.test_alert_reads -v`。采用隔离的内存数据库执行真实查询及 INSERT SELECT，覆盖个人隔离、基地数据范围、批量超过分页、重复请求、新预警、关闭独立，以及无需 token、忽略无效 token、缺失和无效标识。PostgreSQL 的批量计数 CTE 需在部署环境联调验证；未运行生产数据库脚本。

Ingest：`python -m unittest tests.test_alert_read_preservation -v`，验证同日重算保留预警、新日期生成新记录。

前端：类型检查、ESLint、i18n 检查，以及 `scripts/verify-alert-reading-ui.py` 的隔离浏览器交互验证。
