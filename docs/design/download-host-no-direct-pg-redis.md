# 下载机去直连 PG / Redis 方案（主方案：无 MQ + HTTP Claim）

> 状态：**主方案已定为「无 MQ + Postgres work_items + HTTP claim」；D0–D4 scaffolding 已合入（prod 默认仍 legacy）**
> 切流手册：`docs/design/work-queue-cutover.md`  
> 日期：2026-09-14（修订：无 MQ 升为主路径）  
> 关联（降级为可选兼容）：`docs/design/cloudamqp-task-bus.md`  
> 目标：下载机不直连 Postgres；不直连 API 机 Redis；多 API / 多下载集群；下载机可无公网 IP（仅出站）。

---

## 0. 决策摘要（给评审）

| 项 | 选定 |
|----|------|
| **控制面（主）** | Postgres `work_items` + Internal HTTP `claim` / `progress` / `complete` |
| **数据面** | 仅 API 集群访问 PG；下载机零 `DATABASE_URL*` |
| **协调 Redis** | 下载机 **本机 Redis 仅 Celery**；不对 API Redis 出站 |
| **MQ** | **非必需**；CloudAMQP 仅作可选兼容/过渡，不作为新架构前提 |
| **网络** | 下载机出站拉 API（`API_BASE_URL`）；**不需要**下载机公网、**不需要** HTTP 长连接（短轮询默认） |
| **多集群** | API 多实例共享 PG + LB；下载多实例靠 `FOR UPDATE SKIP LOCKED` 领取 |

---

## 1. 背景与问题

### 1.1 当前拓扑（实测）

下载机：`ingest`、`decloud`、`mq_consumer`、`tiler`（偶发本地 `db`/`redis`）。

| 服务 | 直连 PG | 直连 Redis | 实际指向 | 用途摘要 |
|------|---------|------------|----------|----------|
| **ingest** | 是 | 是 | API 机 `:5432` / `:6379` | 读字段/景、upsert、改 jobs；Celery broker；进度 |
| **decloud** | 是 | 是 | 同上 | 去云队列 |
| **mq_consumer** | 是 | 是 | 同上 | canonical land_id；`celery send_task` |
| **tiler** | 通常无 | 无 | — | 出图 |
| **本地 db/redis** | 本机 | 本机 | 与远程库不是一套 | 易混淆；本地 PostgreSQL 应停，Redis 可留作 Celery |

### 1.2 要解决的点

1. 下载机 **不直连 Postgres**（含 API 机 `5432`）。  
2. 下载机 **不直连 API Redis**（含 `:6379`）。  
3. **无 MQ** 也能调度：API / 下载 **多集群**。  
4. 下载机 **无公网 IP** 仍可工作（仅出站）。  
5. `API_BASE_URL` 可配置（先 IP/内网，后公网域名）。

### 1.3 约束

- Celery **不能**用 HTTP 假装 Redis broker。  
- 大批量景写入不适合每像素同步 HTTP；`complete` 可带 OSS key，由 API 落库。  
- Internal API 不对浏览器开放；`INTERNAL_API_TOKEN` 鉴权。

---

## 2. 目标架构（主方案）

### 2.1 一句话

**API 集群是唯一数据面；下载集群是算力面；控制面 = PG 任务表 + HTTP Claim（出站短轮询）；本机 Redis 只给 Celery。**

### 2.2 逻辑图

```
                 ┌────────────────── API 集群 ×N ──────────────────┐
                 │  LB ←── API_BASE_URL（下载机只配这一个）            │
                 │  Postgres（唯一业务库 + work_items）               │
                 │  Redis（仅 API 自用；不对下载机开放）               │
                 │  api / web                                        │
                 │  Internal HTTP:                                   │
                 │    POST /v1/internal/work/claim                   │
                 │    POST /v1/internal/work/{id}/progress|complete  │
                 │    GET  /v1/internal/lands/resolve …              │
                 └──────────────────────▲────────────────────────────┘
                                        │ 出站 HTTPS/HTTP
                                        │ （下载机可无公网 IP）
┌───────────────────────────────────────┴────────────────────────────┐
│ 下载集群 ×M（可无公网）                                              │
│  worker-agent：短轮询 claim → 派本机 Celery / 直接执行               │
│  ingest / decloud / storage：下载·去云·OSS·回传 complete             │
│  本机 Redis：仅 Celery broker                                        │
│  无 DATABASE_URL*；无 API Redis                                       │
│  配置：API_BASE_URL + INTERNAL_API_TOKEN + 本机 REDIS_URL            │
└────────────────────────────────────────────────────────────────────┘
```

### 2.3 职责切分

| 能力 | 位置 | 方式 |
|------|------|------|
| 业务数据存储 | API PG | — |
| 任务入队 | API | `INSERT work_items status=pending`（任意 API 节点） |
| 任务领取 | 下载机 → API | `POST …/claim`（`SKIP LOCKED`） |
| 进度 / 完成 / 失败 | 下载机 → API | `progress` / `complete` / `fail` |
| 只读解析、jobs、景列表等 | 下载机 → API | `GET /v1/internal/...` |
| 景/天气/土壤/报告元数据写入 | API | `complete` 时 upsert（大文件 OSS + key） |
| 机内并发 | 下载机 | 本机 Celery + 本机 Redis |

### 2.4 为何以无 MQ + claim 为主

| 候选 | 结论 |
|------|------|
| **PG work_items + HTTP claim（主）** | 无 MQ、多集群、无公网下载机、与「断直连 PG」一致 |
| CloudAMQP 外层总线 | 可选兼容；运维与账号依赖重，**不再作为前提** |
| 跨机共享 Redis 队列 | 与「断 API Redis」冲突 |
| HTTP 模拟 Redis 给 Celery | 不可行 |
| API 反连下载机推送 | 需要下载机公网 → **禁止** |

---

## 3. work_items 与 Claim 协议

### 3.1 表（示意）

`work_items`：

- `id` UUID PK  
- `type`（如 `agri_optical` / `decloud` / `assessment_report` / `season_growth_report` / …）  
- `payload_json` JSONB  
- `status`：`pending` | `leased` | `done` | `failed`  
- `priority` int  
- `lease_owner` text（worker id）  
- `lease_until` timestamptz  
- `attempts` int  
- `idempotency_key` text UNIQUE NULLS  
- `result_json` JSONB  
- `error` text  
- `created_at` / `updated_at`

索引：`(status, priority DESC, created_at)`；部分索引 `WHERE status='pending'`。

### 3.2 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/v1/internal/work/claim` | body: `{types?, limit?, worker_id, lease_seconds?}` → 原子领取 0..N 条 |
| POST | `/v1/internal/work/{id}/heartbeat` | 续租 |
| POST | `/v1/internal/work/{id}/progress` | 进度 JSON |
| POST | `/v1/internal/work/{id}/complete` | 成功；触发业务 upsert |
| POST | `/v1/internal/work/{id}/fail` | 失败；可按 attempts 再入队 |

领取 SQL 要点：`FOR UPDATE SKIP LOCKED`，多下载机不互抢。

### 3.3 入队来源

业务 REST（选地/长势/拉数等）在 API 侧 **写 `work_items`**（替代或并行于发 CloudAMQP）。  
前端无感：仍调现有 `/v1/lands/.../assessment-report` 等。

### 3.4 回收

定时任务：`status=leased AND lease_until < now()` → 改回 `pending`（或 `attempts++` 后 pending）。

---

## 4. 只读 Internal HTTP（配合断 PG）

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/v1/internal/lands/resolve` | canonical land metadata |
| GET | `/v1/internal/lands/{land_id}/geom` | 边界等 |
| GET | `/v1/internal/jobs/{id}` | 读 job |
| PATCH | `/v1/internal/jobs/{id}` | 兼容旧进度（可与 work progress 合并） |
| GET | `/v1/internal/agri/lands/{id}/scenes` | skip-existing / 报告 |
| GET | `/v1/internal/lands/{land_id}/soil` 等 | 按需 |

Header：`Authorization: Bearer <INTERNAL_API_TOKEN>`。

---

## 5. 配置

### 下载机

| 变量 | 含义 |
|------|------|
| `API_BASE_URL` | API LB，如 `http://81.71.128.251:8000` → 后改公网 |
| `INTERNAL_API_TOKEN` | 与 API 一致 |
| `REDIS_URL` | **仅本机** `redis://127.0.0.1:6379/0` |
| `WORKER_ID` | 领取租约标识（默认 hostname） |
| `WORK_CLAIM_INTERVAL_SEC` | 短轮询间隔，默认 3–5 |
| ~~`DATABASE_URL*`~~ | **删除** |
| ~~连 API 的 Redis~~ | **禁止** |

### API 机

| 变量 | 含义 |
|------|------|
| `INTERNAL_API_TOKEN` | 校验 |
| `DATABASE_URL*` | 唯一业务库 |
| `REDIS_URL` | 仅 API 自用（限流等），不对下载机 |

---

## 6. 网络：无公网下载机

```
下载机（无公网）──出站──▶ API LB（有地址）
         ◀── claim / 只读 / complete
```

- **不需要**下载机公网 IP，**不需要**入站映射。  
- **默认短轮询 claim**；可选长轮询；WebSocket 仅优化且仍为出站客户端。  
- **禁止** API 反连下载机。

---

## 7. Redis / Celery

| 阶段 | 做法 |
|------|------|
| **交付默认** | 下载机本机 Redis = Celery broker；切断 API Redis |
| **后续可选** | 机内也不跑 Celery，claim 后进程内执行 → 可去本机 Redis |

禁止：跨机 Celery 共用 API Redis。

---

## 8. 与 CloudAMQP 文档关系

- `cloudamqp-task-bus.md`：**降级为可选兼容说明**，新部署默认 **不依赖** MQ。  
- 若环境已有 MQ：可短期双写（REST 入队同时发 MQ），切流完成后下线 MQ 消费者。  
- 新功能优先只实现 claim 路径。

---

## 9. 分阶段开发（按此执行）

| 阶段 | 内容 | 成功标准 |
|------|------|----------|
| **D0** | 文档/配置约定；下载机 Redis 改本机；停误起本地 PostgreSQL | 无通往 API `:6379` 的连接 |
| **D1** | 迁移 `work_items`；实现 claim/heartbeat/progress/complete/fail；API 入队接线（先 1–2 类任务，如 assessment / season_growth） | 多 worker 不重复领；无公网出站可跑通 |
| **D2** | Internal resolve + jobs get/patch + agri scene dates；mq_consumer/ingest 热读改 HTTP（`API_BASE_URL` 未设则 DB 回退） | 配置 HTTP 后下载机热读不经 PG；写路径仍 DB（D3） |
| **D3** | complete / `results/apply` 落库（复用 mq_result_writer 逻辑）；ingest 报告 job 可走 HTTP PATCH；`INGEST_PG_WRITES` 开关；**不**一键切断 prod PG | 代码+flag；cutover 见下文 |
| **D4** | 切流手册；扩展 claim 类型（bootstrap/satellite/weather/soil）；双发防护；重 PG 路径 D4.1 延期清单 | scaffolding 合入；prod 默认不翻 claim / PG=0 |

每阶段：回滚开关、冒烟（claim → 执行 → complete → UI 可见）。

---

## 10. 安全与运维

- Internal 仅 token + 建议限制下载机出口 IP。  
- API 对公网关闭 `5432`/`6379`。  
- 下载机启动 fail-fast：检测到 `DATABASE_URL` 或 API Redis host → 拒绝启动（可 `ALLOW_LEGACY_DB=1` 临时）。  
- 监控：pending 堆积、lease 回收次数、claim 空转率、complete 失败率。

---

## 11. 非目标

- 不重写全部遥感算法。  
- 不把 tiler/OSS CDN 策略绑进本文。  
- 不以「必须上 MQ」为验收条件。

---

## 12. 验收清单

- [ ] 下载机无 `DATABASE_URL` / `DATABASE_URL_SYNC`  
- [ ] 下载机不连 API `5432` / `6379`  
- [ ] `API_BASE_URL` 可切换且功能正常  
- [ ] 多下载实例 claim 无重复领取  
- [ ] 下载机无公网时仅出站可完成任务  
- [ ] 短轮询可工作（不依赖长连接）  
- [ ] 选地 / 长势 / 拉数主路径经 work_items 可追踪  
- [ ] 回归 PDF 与拉数成功  

---

## 13. 评审结论

**主方案 = 无 MQ + Postgres work_items + HTTP Claim + 本机 Celery Redis + 出站短轮询。**  
CloudAMQP 为可选兼容。开发按 **D0 → D4** 推进。

---

## D3 cutover（写路径）

**已落地（代码）**

- `agric_satellite_analysis_common.result_apply`：与 mq_result_writer 同一套 assessment/season job 更新 + weather/soil/scene upsert。
- `POST /v1/internal/work/{id}/complete` → `apply_complete_result`。
- `POST /v1/internal/results/apply`：无 work_item 时的域写入。
- ingest：`http_writes_enabled()` 时 assessment / season_growth 走 `PATCH /internal/jobs`；带 `work_item_id` 时任务结束再 `complete`（claim agent 只 progress=dispatched）。
- 默认 **`INGEST_PG_WRITES=1`**、`WORK_QUEUE_MODE=legacy`：行为与切流前兼容。

## D4 scaffolding（安全切流，默认不翻 prod）

**手册：** `docs/design/work-queue-cutover.md`（dual → claim → `INGEST_HTTP_WRITES=1` → `INGEST_PG_WRITES=0` → 可选 MQ teardown；含验证与回滚）。

**已落地（代码）**

- Claimable types：`assessment_report` / `season_growth_report` / `land_bootstrap` / `satellite_analysis` / `agri_bridge` / `weather_backfill` / `soil_fetch`（光学/S1 chunk 仍为 Celery 子任务，挂在 satellite/bootstrap 下）。
- `publish_api_task` 在 `dual|claim` 时同步入队 `work_items`（idempotent）；`claim` 跳过 MQ。
- 一键 `pull_data`：只入队/发布 `land_bootstrap`(+followup)，避免 claim 下裸 PDF 抢跑。
- 双发防护：`should_run_claim_agent()` 仅 `claim`；`dual` 下载机只跑 MQ。
- mq_consumer：`WORK_QUEUE_MODE=claim` 时仅 claim agent。

**D4.1（本阶段）** — assessment / season-growth / 关键 ingest 读：

- `GET /v1/internal/lands/{land_id}/assessment-bundle`
- `GET /v1/internal/lands/{land_id}/season-growth-inputs`
- `GET /v1/internal/lands/{land_id}/data-readiness`
- ingest `load_land_bundle` / season `build_season_facts` 优先 Internal HTTP；`INGEST_PG_READS` 跟随 `INGEST_PG_WRITES`（可显式覆盖）
- weather/soil：centroid 直接读取 canonical land；`INGEST_PG_WRITES=0` 时结果 `results/apply`

**仍延期**

- raster_layers / field_stats 大批量写入的 internal HTTP。
- backfill `pg_advisory_*` 锁。

**切流顺序（摘要；细节见手册）**

1. API `WORK_QUEUE_MODE=dual`（下载仍 legacy）→ 确认 work_items 入队。
2. 下载 `INGEST_HTTP_WRITES=1` canary。
3. 停 MQ 或 API→`claim` 后，下载 `WORK_QUEUE_MODE=claim`（**禁止** dual API + claim download 同时消费同类型）。
4. 验证后再 `INGEST_PG_WRITES=0`；可选下线 MQ。
5. **禁止**在未跑手册验证前把 prod download 默认改成 claim 或 PG=0。
