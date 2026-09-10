# CloudAMQP 外层任务总线（Outer Task Bus）

> 状态：MVP 已落地  
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

## 2. 拓扑

```
Producer (API / script)
    │  TaskMessage → CLOUDAMQP_TASK_QUEUE
    ▼
mq_consumer  ──send_task──▶  Redis ──▶ ingest / storage Celery
    │                                      │
    │                         bridge 完成 / OSS 就绪
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
  "type": "satellite_analysis",
  "field_id": "optional-openfarm-field-uuid",
  "parcel_id": "optional-agri-land_id",
  "land_id": "optional-same-as-parcel_id",
  "extras": { "months": 6, "mode": "full|bridge_only", "force": false },
  "created_at": "ISO-8601"
}
```

- `field_id`：OpenFarm `public.fields.id`  
- `parcel_id` / `land_id`：`agri.land_parcels.land_id`（用户侧常称 field_id 时映射到此）  
- 解析：有 `parcel_id` 无 `field_id` 时，查 `fields.tags_json` 含 `agri:<land_id>` 的田块。

支持 `type`：

| type | 行为 |
|------|------|
| `satellite_analysis` | 默认 `full`：`backfill_indices_for_field` + `bridge_after_backfill`（带 `mq_task_id`） |
| `agri_bridge` | `bridge_only`：`bridge_field_stac_to_agri` |

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

## 4. Compose profiles

| Profile | 服务 | 角色 |
|---------|------|------|
| `consumer` / `mq` | `mq_consumer` | 消费任务队列 → 派 Celery |
| `producer` / `mq` | `mq_result_writer` | 消费结果队列 → 下载 OSS → 写 DB |

```bash
# Consumer 侧（机房 / worker 节点）
docker compose --profile consumer up -d mq_consumer ingest storage

# Producer 侧（业务 / API 节点）
docker compose --profile producer up -d mq_result_writer api

# 两端一起
docker compose --profile mq up -d mq_consumer mq_result_writer
```

环境变量（见 `.env.example`，**勿提交真实 URL**）：

- `CLOUDAMQP_URL`
- `CLOUDAMQP_TASK_QUEUE`（默认 `test_queue`）
- `CLOUDAMQP_RESULT_QUEUE`（默认 `result_queue`）

## 5. 本地冒烟

```bash
# 1) 安装 common（含 pika）
pip install -e packages/openfarm_common

# 2) 发布测试任务（需 .env 中 CLOUDAMQP_*）
python scripts/mq_publish_test.py --field-id <uuid>
# 或 --parcel-id <land_id>

# 3) 起 consumer（需 Redis + DB + ingest）
docker compose --profile consumer up -d --build mq_consumer

# 4) 起 result writer
docker compose --profile producer up -d --build mq_result_writer

# 5) 可选 API 入队（需登录）
# POST /v1/mq/tasks  {"type":"satellite_analysis","field_id":"..."}

# 6) 查库
# SELECT task_id, status, oss_urls, updated_at FROM agri.mq_task_results ORDER BY updated_at DESC LIMIT 10;
```

`docker compose config` 应能通过（即使 CloudAMQP 网络不可达也不影响 compose 校验）。

## 6. 关键文件

| 路径 | 说明 |
|------|------|
| `packages/openfarm_common/openfarm_common/mq.py` | pika 连接 / 声明 / publish / consume |
| `packages/openfarm_common/openfarm_common/mq_schemas.py` | TaskMessage / ResultMessage |
| `packages/openfarm_common/openfarm_common/mq_results.py` | OSS URL 收集 + Result 发布 |
| `services/mq_consumer/` | 任务消费者 |
| `services/mq_result_writer/` | 结果写库 |
| `services/ingest/app/tasks/agri_bridge.py` | 完成后 `mq_task_id` → ResultMessage |
| `services/api/app/routers/mq_tasks.py` | `POST /v1/mq/tasks` |
| `services/api/alembic/versions/0016_mq_task_results.py` | `agri.mq_task_results` |
| `scripts/mq_publish_test.py` | 冒烟发布 |

## 7. 已知限制 / Follow-ups

1. Full `satellite_analysis` 依赖 ingest worker 在线；结果在 **bridge 完成** 后发布，耗时可能很长。  
2. STAC bridge 写入的 `json_oss_key` 常为 NULL，MVP 用 `mq_results/{task_id}.json` 摘要补齐 OSS URL。  
3. 失败重试：consumer 用 `x-retry-count` 头，默认最多 3 次后 ack 丢弃（应已发 failed result）。  
4. 未替换 Celery；CloudAMQP 仅作外层调度。  
5. CI 不强制连通 CloudAMQP。
