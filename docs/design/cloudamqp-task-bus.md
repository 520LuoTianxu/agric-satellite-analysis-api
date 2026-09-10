# CloudAMQP 外层任务总线（Outer Task Bus）

> 状态：MVP + API 入队 + **两队列 rename/split**（download / process）  
> 关联仓库：`agric-satellite-analysis`  
> 日期：2026-09-10  
> 分支：`feat/cloudamqp-task-bus`

## 1. 目标

在 **不拆掉现有 Celery（Redis ingest/storage/beat）** 的前提下，增加一层 **CloudAMQP（RabbitMQ）调度**：

| 层 | 职责 | 载荷 |
|----|------|------|
| **Outer MQ**（CloudAMQP） | 任务接入 + 结果通知 | 元数据 / **inline JSON**（天气·土壤）/ **OSS URL**（遥感） |
| **Inner Celery**（现有） | 下载 / 计算 / 上传 | 大文件与栅格流水线 |

原则：

- **天气 / 土壤**：结果 JSON **inline** 放进 `ResultMessage.payload`（或别名 `data`），由 `mq_result_writer` 写入业务表。体积须安全低于 CloudAMQP 实用上限（约 **100KB**）；超限则上传 OSS 并把 key/url 放进 `oss_urls`，payload 仅留 stub。
- **遥感**：download/compute → 生成 **DB-ready lonlat_v1 JSON** → **上传 OSS** → `ResultMessage.oss_urls` → producer/`mq_result_writer` **拉取 OSS JSON** 写入 `agri.parcel_scene_products`。**不要**把 ListObjects + 晚采样当作主路径。

**UI 仍调用原 REST**；服务端 `publish_api_task`（`services/api/app/mq_publish.py`），由 `mq_consumer` 再 `send_task` 到现有 Celery。默认 **无 Celery 直发回退**（缺 `CLOUDAMQP_URL` → 503）；仅本地可设 `MQ_FALLBACK_CELERY=1`。

> 注：天气/土壤 JSON 通常不大；若未来确认稳定 &lt; CloudAMQP 实用上限，可继续以 inline 为主。遥感像素 JSON 走 OSS。

## 2. 两队列拓扑（download / process）

旧名 `openfarm_tasks` / `openfarm_results`（及文档里的 `test_queue` / `result_queue`）已替换为：

| Role | Queue name | Who |
|------|------------|-----|
| **Download queue** | `openfarm_download` | Page/API click 发布 `TaskMessage`。Download workers（`mq_consumer` + ingest/storage Celery）**只消费**此队列，做 download/compute/upload。 |
| **Process / write-DB queue** | `openfarm_process` | Download（或 weather/soil fetch）完成后发布 `ResultMessage`。`mq_result_writer` **只消费**此队列并写业务 DB。 |

```
Producer (API REST / script / POST /v1/mq/tasks)
    │  TaskMessage → CLOUDAMQP_DOWNLOAD_QUEUE  (openfarm_download)
    │  （Producer 不消费 download 队列）
    ▼
Download host: mq_consumer  ──send_task──▶  Redis ──▶ ingest / storage Celery
    │                                              │
    │                                 mq_task_id 钩子
    │                                              │
    │◀──── ResultMessage ──────────────────────────┘
    │         payload?  +  oss_urls?
    │         CLOUDAMQP_PROCESS_QUEUE  (openfarm_process)
    │  （Download host 若他机负责写库，则不要跑 mq_result_writer）
    ▼
Process host: mq_result_writer
    ├─ payload.kind=weather_daily / soil_profile → upsert 业务表
    ├─ oss_urls → GET JSON
    │     ├─ lonlat_v1 scene → agri.parcel_scene_products
    │     └─ weather/soil fallback body → 同上 upsert
    └─ 始终写入 agri.mq_task_results
```

### 部署角色约定

- **Producer（用户 API 机）**：只向 `openfarm_download` 发布；**不要**消费 download 队列。
- **Download host**：只消费 `openfarm_download`；完成后向 `openfarm_process` 发布 `ResultMessage`；若另一台机器负责写库，**不要**在本机跑 `mq_result_writer`。
- **Process host（`mq_result_writer`）**：消费 `openfarm_process` 并 upsert 业务 DB。
- **Weather / soil**：仍经 download 队列入队（页面点击）→ worker 拉取 → `ResultMessage`（inline payload）→ process 队列 → writer upsert。  
  **Follow-up**：ingest 天气/土壤任务目前可能仍直接写库（与 writer 双写）；以 result-writer 为单一真相源需另开小改，本轮不做大爆炸重写。

## 3. 消息约定

### TaskMessage → `CLOUDAMQP_DOWNLOAD_QUEUE`（`openfarm_download`）

```json
{
  "task_id": "uuid",
  "type": "satellite_analysis|agri_bridge|weather_backfill|soil_fetch|field_bootstrap",
  "field_id": "optional-openfarm-field-uuid",
  "parcel_id": "optional-agri-land_id",
  "land_id": "optional-same-as-parcel_id",
  "extras": {},
  "created_at": "ISO-8601"
}
```

### 支持的 type

| type | extras（常用） | Celery 派发 | ResultMessage |
|------|----------------|-------------|---------------|
| `satellite_analysis` | `months`（默认 **60**）、`force`（默认 **false**，补缺）、`allow_agri`, `sentinel_job_id`, `with_bridge`, `bridge_job_id`, … | `backfill_indices_for_field`；可选 bridge | bridge 完成后：`oss_urls`（scene JSON） |
| `agri_bridge` | `mode=bridge_only` | `bridge_field_stac_to_agri` | `oss_urls` |
| `weather_backfill` | `days?: int` | `backfill_weather_for_field(..., mq_task_id=)` | **inline** `payload.kind=weather_daily` |
| `soil_fetch` | `job_id?` | `fetch_soil_for_field(..., mq_task_id=)` | **inline** `payload.kind=soil_profile` |
| `field_bootstrap` | `skip_indices?`, `sentinel_job_id?` | fan-out weather + soil +（可选）indices | consumer 轻量 `phase=bootstrap_dispatched`（子任务各自带结果） |

### ResultMessage → `CLOUDAMQP_PROCESS_QUEUE`（`openfarm_process`）

```json
{
  "task_id": "uuid",
  "status": "success|failed",
  "oss_urls": { "2024-06-01_S2": "https://..." },
  "payload": { "kind": "weather_daily|soil_profile|...", "...": "..." },
  "data": null,
  "error": null,
  "field_id": "...",
  "land_id": "...",
  "finished_at": "ISO-8601",
  "extras": {}
}
```

- `payload` / `data`：二者等价（schema 互相同步）；天气/土壤优先用 inline。
- 遥感：bridge 将每景 DB-ready JSON 上传到  
  `{OSS_PREFIX}{land_id}/{date}_S2.json`（默认前缀 `s1s2_parcel/json/`），写入 `parcel_scene_products.json_oss_key`，并在 `oss_urls` 带上 URL。
- Inline 超限（默认 100KB）：上传 `mq_results/{kind}/{task_id}.json`，`oss_urls[kind]=url`，payload 变为 stub（`oss_fallback: true`）。

## 4. API → MQ 映射（UI 契约不变）

| REST | MQ type | 仍由 API 创建的 Job 哨兵 |
|------|---------|-------------------------|
| `POST /fields`（首次开通） | `field_bootstrap` | 非 agri 时 backfill sentinel |
| `POST /fields/{id}/backfill-indices` | `satellite_analysis` | backfill sentinel；agri 另建 `agri_bridge` Job |
| `POST /fields/{id}/weather/backfill` | `weather_backfill` | — |
| `POST /fields/{id}/soil/refresh` | `soil_fetch` | `soil_fetch` Job |
| `POST /v1/mq/tasks` | 上表类型白名单 | — |

> 天气/土壤 **保持** MQ 入队（不要改回 API 直发 Celery）。Celery 任务仍可本地写库；process 侧 `mq_result_writer` 按 payload/OSS 再 upsert，便于跨库/对账。

`GET .../backfill-status` 继续读 Job 行。

## 5. Compose profiles

| Profile | 服务 | 角色 |
|---------|------|------|
| `consumer` / `mq` | `mq_consumer` | 消费 **download** 队列 → 派 Celery |
| `producer` / `mq` | `mq_result_writer` | 消费 **process** 队列 → inline/OSS → 写 DB |

```bash
docker compose --profile mq up -d --build api ingest mq_consumer mq_result_writer
```

环境变量（见 `.env.example`，**勿提交真实 URL**）：

- `CLOUDAMQP_URL`
- `CLOUDAMQP_DOWNLOAD_QUEUE`（默认 `openfarm_download`）  
  - 一发兼容别名：`CLOUDAMQP_TASK_QUEUE`（若设置且未设 DOWNLOAD，则沿用）
- `CLOUDAMQP_PROCESS_QUEUE`（默认 `openfarm_process`）  
  - 一发兼容别名：`CLOUDAMQP_RESULT_QUEUE`
- `MQ_FALLBACK_CELERY`（可选，默认关闭）
- `OSS_PREFIX`（默认 `s1s2_parcel/json/`）

## 6. 本地冒烟

```bash
pip install -e packages/openfarm_common
python scripts/mq_publish_test.py --field-id <uuid>
python scripts/mq_publish_test.py --field-id <uuid> --type weather_backfill --days 30
python scripts/mq_publish_test.py --field-id <uuid> --type soil_fetch

docker compose --profile mq up -d --build api ingest mq_consumer mq_result_writer
# SELECT task_id, status, payload, oss_urls, updated_at FROM agri.mq_task_results ORDER BY updated_at DESC LIMIT 10;
```

> 共享 CloudAMQP 时注意：勿同时拉起多个 competing consumer；本机验证优先 code/compose，慎启 `mq_consumer` / `mq_result_writer`。

## 7. 关键文件

| 路径 | 说明 |
|------|------|
| `packages/openfarm_common/openfarm_common/settings.py` | `cloudamqp_download_queue` / `cloudamqp_process_queue` + 旧别名 |
| `packages/openfarm_common/openfarm_common/mq.py` | pika 连接 / publish→download / publish_result→process / consume |
| `packages/openfarm_common/openfarm_common/mq_schemas.py` | TaskMessage / ResultMessage（含 payload/data） |
| `packages/openfarm_common/openfarm_common/mq_results.py` | inline 限幅、scene JSON 上传、Result 发布 |
| `services/api/app/mq_publish.py` | API 侧 publish → download 队列 |
| `services/mq_consumer/` | download 队列消费者 |
| `services/mq_result_writer/` | process 队列写库（mq_task_results + weather/soil/scene） |
| `services/ingest/app/tasks/bridge_stac_cogs_to_agri_lonlat.py` | 每景上传 JSON + `json_oss_key` |
| `services/ingest/app/tasks/agri_bridge.py` | 完成后发布带 `oss_urls` 的 Result |
| `services/ingest/app/tasks/weather.py` / `soil.py` | 完成后发布 inline payload |
| `docs/design/cloudamqp-task-bus.md` | 本文 |

## 8. 已知限制 / Follow-ups

1. Full `satellite_analysis`（带 bridge）结果在 **bridge 完成** 后发布，耗时可能很长。  
2. `field_bootstrap` 仅保证「已入队」结果；子任务失败看 Celery / Job / 各自 Result。  
3. 365 天天气行可能超过 100KB → 自动 OSS fallback；短窗口（如 30 天）优先 inline。  
4. Celery 与 writer 双写同一库时依赖 upsert 幂等（天气/土壤尤甚；长期应以 process writer 为源）。  
5. S1 bridge 路径尚未统一上传 scene JSON（本 MVP 覆盖 STAC S2 lonlat bridge）。  
6. 失败重试：consumer 用 `x-retry-count` 头，默认最多 3 次后 ack。  
7. 改 `mq_consumer` / `mq_result_writer` / API / ingest 后需 `--build`。  
8. 旧队列名 `openfarm_tasks` / `openfarm_results` 上若仍有残留消息，需人工迁移或消费干净后再切流量。
