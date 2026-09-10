# CloudAMQP 外层任务总线（Outer Task Bus）

> 状态：MVP + API 入队改造已落地  
> 关联仓库：`agric-satellite-analysis`  
> 日期：2026-09-10  
> 分支：`feat/cloudamqp-task-bus`

## 1. 目标

在 **不拆掉现有 Celery（Redis ingest/storage/beat）** 的前提下，增加一层 **CloudAMQP（RabbitMQ）调度**：

| 层 | 职责 | 载荷 |
|----|------|------|
| **Outer MQ**（CloudAMQP） | 任务接入 + 结果通知 | 仅 JSON 元数据 / OSS URL |
| **Inner Celery**（现有） | 下载 / 计算 / 上传 | 大文件与栅格流水线 |

MQ **不传**大型像素 JSON；结果侧只带 `oss_url(s)`，由 producer 侧下载入库。

**UI 仍调用原 REST**；服务端改为 `publish_task`（`services/api/app/mq_publish.py`），由 `mq_consumer` 再 `send_task` 到现有 Celery。默认 **无 Celery 直发回退**（缺 `CLOUDAMQP_URL` → 503）；仅本地可设 `MQ_FALLBACK_CELERY=1`。

## 2. 拓扑

```
Producer (API REST / script / POST /v1/mq/tasks)
    │  TaskMessage → CLOUDAMQP_TASK_QUEUE
    ▼
mq_consumer  ──send_task──▶  Redis ──▶ ingest / storage Celery
    │                                      │
    │                         mq_task_id 钩子 / bootstrap 轻量结果
    │                                      │
    │◀──── ResultMessage (oss_urls) ───────┘
    │         CLOUDAMQP_RESULT_QUEUE
    ▼
mq_result_writer ──HTTP/OSS GET──▶ agri.mq_task_results
```

## 3. 消息约定

### TaskMessage → `CLOUDAMQP_TASK_QUEUE`

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

- `field_id`：OpenFarm `public.fields.id`  
- `parcel_id` / `land_id`：`agri.land_parcels.land_id`  
- 解析：有 `parcel_id` 无 `field_id` 时，查 `fields.tags_json` 含 `agri:<land_id>` 的田块。

### 支持的 type

| type | extras（常用） | Celery 派发 | ResultMessage |
|------|----------------|-------------|---------------|
| `satellite_analysis` | `months`, `force`, `allow_agri`, `sentinel_job_id`, `with_bridge`, `bridge_job_id`, `dispatch_alerts`, `mode` | `backfill_indices_for_field`；可选 `bridge_after_backfill` + alerts | 有 bridge 时由 agri_bridge 钩子发布；无 bridge 时 consumer 发 `phase=dispatched` 轻量成功 |
| `agri_bridge` | （强制 `mode=bridge_only`） | `bridge_field_stac_to_agri` | agri_bridge 钩子 |
| `weather_backfill` | `days?: int` | `backfill_weather_for_field(..., mq_task_id=)` | weather 任务结束钩子 |
| `soil_fetch` | `job_id?` | `fetch_soil_for_field(..., mq_task_id=)` | soil 任务结束钩子 |
| `field_bootstrap` | `skip_indices?`, `sentinel_job_id?` | **一条 MQ → fan-out** weather + soil +（可选）indices | consumer 在入队后发一条 `phase=bootstrap_dispatched` 轻量成功（子任务不再共用同一 `mq_task_id`，避免三份竞态结果） |

#### `field_bootstrap` 选型说明

优先 **一条 TaskMessage**，由 consumer 扇出到现有 Celery（与 `create_field` 原先三次 `send_task` 等价）。  
不在 bootstrap 路径上给三个子任务挂同一 `mq_task_id`（会写三条冲突 Result）。完整 weather/soil 结果请走独立 `weather_backfill` / `soil_fetch`。

### ResultMessage → `CLOUDAMQP_RESULT_QUEUE`

```json
{
  "task_id": "uuid",
  "status": "success|failed",
  "oss_urls": { "label": "https://..." },
  "error": null,
  "field_id": "...",
  "land_id": "...",
  "finished_at": "ISO-8601"
}
```

若 `parcel_scene_products.json_oss_key` 为空（STAC bridge 路径常为 NULL），会上传 `mq_results/{task_id}.json` 摘要作为 `oss_urls.summary`。

## 4. API → MQ 映射（UI 契约不变）

| REST | MQ type | 仍由 API 创建的 Job 哨兵 |
|------|---------|-------------------------|
| `POST /fields`（首次开通） | `field_bootstrap` | 非 agri 时 backfill sentinel |
| `POST /fields/{id}/backfill-indices` | `satellite_analysis` | backfill sentinel；agri 另建 `agri_bridge` Job |
| `POST /fields/{id}/weather/backfill` | `weather_backfill` | — |
| `POST /fields/{id}/soil/refresh` | `soil_fetch` | `soil_fetch` Job |
| `POST /v1/mq/tasks` | 上表类型白名单 | — |

`GET .../backfill-status` 继续读 Job 行；API 在 enqueue 前 `flush`/`commit` 哨兵，保证轮询立即可见。

## 5. Compose profiles

| Profile | 服务 | 角色 |
|---------|------|------|
| `consumer` / `mq` | `mq_consumer` | 消费任务队列 → 派 Celery |
| `producer` / `mq` | `mq_result_writer` | 消费结果队列 → 下载 OSS → 写 DB |

```bash
# 两端一起（改 consumer/API 后需 rebuild）
docker compose --profile mq up -d --build mq_consumer mq_result_writer api ingest
```

环境变量（见 `.env.example`，**勿提交真实 URL**）：

- `CLOUDAMQP_URL`
- `CLOUDAMQP_TASK_QUEUE`（默认 `test_queue`）
- `CLOUDAMQP_RESULT_QUEUE`（默认 `result_queue`）
- `MQ_FALLBACK_CELERY`（可选，默认关闭；`1` 时缺 URL 直发 Celery）

## 6. 本地冒烟

```bash
pip install -e packages/openfarm_common
python scripts/mq_publish_test.py --field-id <uuid>
python scripts/mq_publish_test.py --field-id <uuid> --type weather_backfill --days 30
python scripts/mq_publish_test.py --field-id <uuid> --type soil_fetch
python scripts/mq_publish_test.py --field-id <uuid> --type field_bootstrap

docker compose --profile mq up -d --build mq_consumer mq_result_writer
# SELECT task_id, status, extras, updated_at FROM agri.mq_task_results ORDER BY updated_at DESC LIMIT 10;
```

## 7. 关键文件

| 路径 | 说明 |
|------|------|
| `packages/openfarm_common/openfarm_common/mq.py` | pika 连接 / 声明 / publish / consume |
| `packages/openfarm_common/openfarm_common/mq_schemas.py` | TaskMessage / ResultMessage |
| `packages/openfarm_common/openfarm_common/mq_results.py` | OSS URL 收集 + Result 发布 |
| `services/api/app/mq_publish.py` | API 侧 publish 封装（503 / 可选 fallback） |
| `services/mq_consumer/` | 任务消费者 |
| `services/mq_result_writer/` | 结果写库 |
| `services/ingest/app/tasks/agri_bridge.py` | 完成后 `mq_task_id` → ResultMessage |
| `services/ingest/app/tasks/weather.py` / `soil.py` | 同上钩子 |
| `services/api/app/routers/mq_tasks.py` | `POST /v1/mq/tasks` |
| `services/api/alembic/versions/0016_mq_task_results.py` | `agri.mq_task_results` |
| `scripts/mq_publish_test.py` | 冒烟发布 |

## 8. 已知限制 / Follow-ups

1. Full `satellite_analysis`（带 bridge）结果在 **bridge 完成** 后发布，耗时可能很长。  
2. `field_bootstrap` 仅保证「已入队」结果；子任务失败需看 Celery / Job。  
3. Admin 批量接口（`backfill-all-fields` / `ensure-agri-soil-weather`）仍可直发 Celery（非 UI 主路径）。  
4. 失败重试：consumer 用 `x-retry-count` 头，默认最多 3 次后 ack。  
5. CI 不强制连通 CloudAMQP。改 `mq_consumer` / API 后需 `--build`。
