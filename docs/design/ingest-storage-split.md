# 国外数据下载与 OSS 上传服务拆分设计

> 状态：已定稿；**物理拆包已落地**（openfarm_common + 独立 ingest/storage 镜像）  
> 关联仓库：`agric-satellite-analysis`  
> 日期：2026-09-10

## 1. 背景与问题

当前观测层能力全部跑在 **同一套 `services/api` 镜像** 里：

| 能力 | 现状入口 | 国外源 |
|------|----------|--------|
| 光学遥感 S2 | `app.tasks.pipeline` / indices | Element84 STAC `earth-search` |
| 雷达遥感 S1 | `app.tasks.sentinel1` | Element84 STAC `sentinel-1-grd` |
| 天气 | `app.tasks.weather` | Open-Meteo |
| 土壤 | `app.tasks.soil` | SoilGrids WCS / POLARIS S3 |
| 对象存储 | `app.core.storage` + 多处 `upload_file` | 阿里云 OSS（默认） |

Compose 里虽有独立 `processor` 容器，但与 `api` **共用同一代码与依赖**（GDAL、STAC、OSS SDK、业务路由）。结果是：

1. **职责耦合**：业务 API、鉴权、报告与「拉国外数据 / 写 OSS」绑死，镜像臃肿、扩缩容不独立。  
2. **出口冗余**：每个 worker 都持有 OSS AK 与国外源访问路径，密钥面过大。  
3. **运维困难**：限流、重试、代理、跨境带宽无法按「下载」与「上传」分别治理。  
4. **与架构分层不符**：ARCHITECTURE Layer A（Observation）应可独立部署，却埋在 Delivery/API 进程里。

## 2. 目标

1. **独立下载服务（ingest）**：只负责从国外源拉取（遥感 / 天气 / 土壤）并产出标准化中间结果。  
2. **独立存储服务（storage）**：只负责对象存储读写（默认 OSS），对外提供上传任务 / 少量同步 HTTP。  
3. **API 瘦身**：鉴权、派单、读库、给前端；**不再**在 API 进程内直连 STAC / SoilGrids / 大文件 OSS 上传。  
4. **一次改到位**：本轮交付可运行的 compose 拓扑 + 任务路由迁完；旧路径删除或变为薄封装转发，避免长期双轨。

非目标（本轮不做）：

- 换成国产数据源或镜像站（可后续在 ingest 插件化）。  
- 拆独立 Git 仓库（仍 monorepo：`services/ingest`、`services/storage`）。  
- 迁移历史 MinIO 存量数据。

## 3. 目标拓扑

```
┌────────────┐     ┌─────────────┐     ┌──────────────────┐
│  web       │────▶│  api        │────▶│ Redis (broker)   │
└────────────┘     │  (派单/读库) │     └────────┬─────────┘
                   └─────────────┘              │
          ┌─────────────────────────────────────┼─────────────────────────┐
          ▼                                     ▼                         ▼
┌──────────────────┐              ┌──────────────────┐      ┌──────────────────┐
│ ingest worker    │  chain/chord │ storage worker   │      │ (可选) beat      │
│ queue: ingest    │─────────────▶│ queue: storage   │      │ 调度仍在 api 或  │
│ STAC/Open-Meteo/ │   upload_*   │ OSS put/get      │      │ 独立 beat 容器   │
│ SoilGrids/...    │              │ 返回 key/url     │      └──────────────────┘
└────────┬─────────┘              └────────┬─────────┘
         │ 写业务表 / job 状态              │
         └───────────────▶ Postgres ◀───────┘
```

**队列约定**

| Queue | 消费者 | 任务类型 |
|-------|--------|----------|
| `ingest` | `ingest` 服务 | `fetch_weather_*`、`fetch_soil_*`、`process_*` / S1·S2 下载与指数计算、桥接前下载 |
| `storage` | `storage` 服务 | `upload_file`、`put_bytes`、`upload_cog`、`upload_json`、presign 生成（可选） |
| `default` / `celery` | **废弃**（或仅留轻量：alerts、PDF 组装且不上传时） | 过渡期清空 |

**进程职责**

| 服务 | 镜像目录 | 职责 |
|------|----------|------|
| `api` | `services/api` | FastAPI、鉴权、创建 Job、`send_task` 到 ingest/storage、读 PG |
| `ingest` | `services/ingest` | Celery worker `-Q ingest`；国外下载 + 栅格计算；调用 storage 任务上传 |
| `storage` | `services/storage` | Celery worker `-Q storage`（+ 可选 `:8081` 健康/管理 HTTP）；OSS/MinIO |
| `tiler` / `web` / `db` / `redis` | 不变 | — |

原 `processor` **删除**，由 `ingest` + `storage` 替代。

## 4. 接口约定

### 4.1 派单（API → Redis）

保持 Celery 任务名稳定（便于旧客户端），但 **路由到新队列**：

```python
# celery route 示例
task_routes = {
    "app.tasks.weather.*": {"queue": "ingest"},
    "app.tasks.soil.*": {"queue": "ingest"},
    "app.tasks.pipeline.*": {"queue": "ingest"},
    "app.tasks.sentinel1.*": {"queue": "ingest"},
    "app.tasks.vegetation.*": {"queue": "ingest"},
    "app.tasks.ndvi.*": {"queue": "ingest"},
    "app.tasks.indices.*": {"queue": "ingest"},
    "app.tasks.agri_bridge.*": {"queue": "ingest"},
    "app.tasks.bridge_stac_cogs_to_agri_lonlat.*": {"queue": "ingest"},
    "app.tasks.storage.*": {"queue": "storage"},
}
```

业务任务内部 **禁止** `get_storage().upload_file`；改为：

```python
celery_app.send_task(
    "app.tasks.storage.upload_file",
    args=[key, local_path, content_type],
    queue="storage",
)
# 或 chain: download.s() | compute.s() | upload_file.s()
```

### 4.2 storage 任务契约

| 任务名 | 入参 | 出参 |
|--------|------|------|
| `app.tasks.storage.upload_file` | `key`, `path`（worker 本机或共享卷）, `content_type?` | `{key, public_url, backend}` |
| `app.tasks.storage.put_bytes` | `key`, `data_b64` 或 Redis/临时对象引用, `content_type?` | 同上 |
| `app.tasks.storage.exists` | `key` | `bool` |
| `app.tasks.storage.public_url` | `key` | `str` |

大文件优先走 **共享 volume**（`/data/scratch`）或 ingest 写本地后 storage 同机挂载；跨机时用「先 put 到临时 bucket 前缀」两段式（本轮默认 **compose 同机共享 `scratch` volume**）。

### 4.3 HTTP（storage 可选）

仅运维/脚本：

- `GET /healthz`
- `POST /v1/upload`（内网、需服务 token）— 与现有 `scripts/upload_to_oss.py` 可并存；脚本可继续直连 OSS，服务路径给在线任务用。

API 现有 `routers/storage.py`（presign、客户端直传）保留在 **api**，但 **服务端大文件 put** 转发 storage 队列。

## 5. 代码布局（monorepo）

```
packages/
  openfarm_common/     # settings, ObjectStorage, celery factory, storage_client
services/
  api/                 # FastAPI + beat client（send_task，不含重任务模块）
  ingest/
    Dockerfile         # GDAL/STAC；build context = repo root
    app/
      worker.py
      tasks/           # weather, soil, pipeline, sentinel1, ...（从 api git mv）
      models/ core/ reports/  # ingest 自有副本（或经 common 再导出）
  storage/
    Dockerfile         # python slim，无 GDAL；build context = repo root
    app/
      worker.py
      tasks/storage_tasks.py   # app.tasks.storage.*
```

**共享策略（本轮）**

- 短期：`ingest` / `storage` **复制或 git subtree 式引用** `api` 内必要模块，用 `PYTHONPATH` 或 pip editable 包 `openfarm-common`（若时间紧，Dockerfile `COPY` 共享目录 `packages/common`）。  
- 推荐落地：新建 `packages/common`（config 片段、DB sync、Job 模型访问），api/ingest/storage 依赖之，避免三份 storage 实现。

若「一次改到位」工期紧：允许 ingest Dockerfile `COPY services/api/app` 并以 `celery -A app.worker` 启动、仅改 **compose 命令与 queue** + **任务内上传改为 send_task**；同时新建精简 `services/storage`。随后再物理搬目录。设计上以「逻辑拆分完成」为验收，物理目录以可维护为优先。

**本轮落地选择（执行口径）**

1. 新增 `services/storage`（独立 worker + `app.tasks.storage.*`）。  
2. 将原 `processor` 重命名/替换为 `ingest` 服务，**只消费 `ingest` 队列**，环境变量去掉「仅 API 需要」的项可保留兼容。  
3. 所有 `get_storage().upload_*` 改为调用 storage 任务（同步 `apply` 短超时或 chain）。  
4. `api` 容器 **不再启动 Celery worker**；beat 可挂在 `ingest` 或独立 `beat` 服务。  
5. 删除 compose 中的 `processor` 服务名，文档与 DEPLOYMENT 同步。

## 6. 数据流（示例）

### 6.1 天气

```
API POST /weather/backfill
  → Job(row)
  → send_task(fetch_weather_for_field) [queue=ingest]
  → Open-Meteo HTTP
  → upsert weather_daily (PG)
```

无 OSS。

### 6.2 土壤

同天气；SoilGrids/POLARIS 仅在 ingest。

### 6.3 Sentinel-2 指数 COG

```
ingest: STAC search → download bands → compute → 写 /scratch/{job}/xxx.tif
     → send_task(upload_file, key, path) [queue=storage]
storage: OSS put → 返回 public_url / key
ingest: 写 raster_layers / field_stats / agri bridge
```

### 6.4 选地 PDF

PDF 生成可留在 ingest 或 api 侧临时目录，**上传 OSS** 必须走 storage。

## 7. 配置与密钥

| 变量 | api | ingest | storage |
|------|-----|--------|---------|
| `DATABASE_URL(_SYNC)` | ✓ | ✓ | ✗（除非要写审计，默认否） |
| `REDIS_URL` | ✓ | ✓ | ✓ |
| `CELERY_REDIS_*` / `CELERY_BROKER_*` | ✓ | ✓ | ✓ |
| `STAC_API_URL` | ✗ | ✓ | ✗ |
| `SOILGRIDS_*` / `POLARIS_*` | ✗ | ✓ | ✗ |
| `OSS_*` / `STORAGE_BACKEND` | 仅 presign 需要时可留 | ✗ | ✓ |
| `OPENFARM_JWT_SECRET` | ✓ | ✗ | ✗ |

Ingress 网络：仅 `storage` 与 `ingest` 出网拉国外源 / 写 OSS；`api` 可禁出网到 STAC（可选防火墙策略，本轮文档记载，不强绑实现）。

## 8. 迁移与回滚

1. 合并路由与新服务后，本地 `docker compose up` 验证天气/土壤/S2/S1 各一条任务。  
2. 回滚：恢复 `processor` 服务且 `task_routes` 清空（commit revert）。  
3. 不保留「API 内同步下载」双轨；发现遗漏调用点一律改派单。

## 9. 验收标准

- [ ] compose 存在 `ingest`、`storage`，无 `processor`。  
- [ ] `ruff`/web CI 绿；API 镜像可去掉部分重量依赖为加分项（本轮不强制削依赖）。  
- [ ] 触发天气回填、土壤获取、地块指数/S1 任务成功，OSS 上出现对象。  
- [ ] 代码检索：`upload_file` / `put_object` 仅出现在 `services/storage`（及脚本 `scripts/upload_to_oss.py`）。  
- [ ] 本文档链接写入 `ARCHITECTURE.md` / `DEPLOYMENT.md` 短节。

## 10. 风险

| 风险 | 缓解 |
|------|------|
| Celery chain 跨队列延迟 | scratch 卷 + 明确超时；关键路径 `eager` 仅测试 |
| 大文件 base64 进 broker | 禁止；只用共享盘路径 |
| 任务名拆分导致旧 worker 吃单 | 滚动时先起新消费者再停 processor |
| agri 桥接与 COG 路径假设 | 桥接任务同放 ingest，上传后只存 key |

## 11. 实施顺序（本 PR）

1. 合并本文档。  
2. 实现 `services/storage` + queue。  
3. 实现 `ingest` 替换 `processor` + `task_routes`。  
4. 替换所有进程内 OSS 上传为 storage 任务。  
5. 更新 compose / DEPLOYMENT / ARCHITECTURE。  
6. 本地冒烟 + 推 PR。
