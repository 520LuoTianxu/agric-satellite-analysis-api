# 双周报离线出报（导出文件 → 正式 PDF）

用已经导出的真实数据文件，在一台不连任何服务的机器上，为一个地块生成各期正式种植双周报。
入口是 `scripts/biweekly_offline_render.py`，渲染走线上同一个函数
`generate_official_biweekly_pdf`（`offline=True`）。

适用场景：真实数据在下载机或数据服务里，出报的人拿不到也不应拿生产凭据。数据由有权限的一方
只读导出成文件，出报方只处理文件。

## 边界

- 只读输入文件，只写 `--out` 目录。
- 不连接 Postgres、Redis、MySQL、对象存储、内部接口、消息队列；不下载影像和气象；不调用
  Celery 任务；不上传 PDF，不回写任务状态。
- 启动时把 `DATABASE_URL`、`REDIS_URL`、`API_BASE_URL`、`INTERNAL_API_TOKEN`、`OSS_ACCESS_KEY_*`
  等连接配置从本进程环境里移除（只打印变量名），所以即便在下载机的终端里运行也连不到服务。
- 不产生数据：输入缺什么，报告就少什么并写明原因。不会把真实地块标成示例数据，也拒绝读取
  带示例标记（`data_kind` 为 `demo` / `mock`）的产品。
- 其他地块的方案、景、气象、产品一律报错，不拿来代替。

## 与线上共用的代码

| 步骤 | 线上 | 离线 |
| --- | --- | --- |
| 种植季与分期 | 接口机读 Smart 后调用 `plan_calendar.build_seasons_from_rows` → `calendar.iter_periods` | 同一函数，输入为导出的行 |
| 景是否采用 | `scene_rows.to_observation`（`parcel_scene_products` 行） | 同一函数 |
| 上一期对比 | `_last_optical_observations`：向前找最近一个有可用光学的期 | `offline_render.last_optical_observations`，同一规则 |
| 事实构建 | `TriggerService.prepare_generate_facts` | 同一方法 |
| 补充与渲染 | `generate_official_biweekly_pdf` | 同一函数，`offline=True` |

`offline=True` 只关掉三件需要外部服务的事：从对象存储补读产品、查气象、取周边真彩色影像。

## 窗口怎么来

种植季 = 本田 Smart 小麦批次（`crop_id = 4`）下全部种植任务的最早 `plan_start` 到最晚
`plan_finish`。分期由 `iter_periods` 按 14 天一期切分，北京时间自然日，区间左闭右开，最后一期
可以不足 14 天。期号从 1 起。种植季编号是 `2025/26` 这样的跨年标签（同年则为 `2026`），
输出目录名里的 `/` 写成 `-`。

没有方案就没有种植季，也就不出报，线上同样如此。离线工具不接受手填的起止日期。

先运行 `periods` 可以只看窗口，并得到各类数据应导出的日期范围：

```bash
python scripts/biweekly_offline_render.py periods --land-id 19994 --plan plan.json
```

## 输入文件

### 1. 方案（必需）

三种来源任选其一，结果相同。

**a. Smart 原始行**（`--plan plan.json`，或 `--plan-batches` + `--plan-tasks` 分别给 JSON/CSV）

```json
{"batches": [...], "tasks": [...], "mapping_rows": [...]}
```

`batches`：先从 `agriculture_batch_task` 取出仍在种植中的批次，再联
`agriculture_crop_batch`。条件是 `t.land_id = <地块> AND t.del_flag = '0'
AND t.status IN ('0','1','2')`（status 3、4 为已完成，不取），以及
`b.del_flag = '0' AND b.crop_id = 4`。地块以任务表的 `land_id` 为准，
不用 `crop_batch_land_scheme`，也不用 `agriculture_crop_batch.land_id`。

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `batch_id` | 是 | 与任务的 `plan_id` 对应 |
| `land_id` | 是 | 必须等于目标地块 |
| `crop_id` | 是 | 只认 4（小麦） |
| `batch_name`、`crop_name`、`scheme_handling_profile`、`project_crop_season_id` | 否 | 原样带入 |

`tasks`：`agriculture_plant_task`，条件 `plan_id IN (<batch_id…>) AND (del_flag = '0' OR del_flag IS NULL)`。

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `plan_id` | 是 | 所属批次 |
| `task_name` | 是 | 生育阶段中文名，用于映射 S1–S3 等阶段 |
| `plan_start`、`plan_finish` | 是 | 日期或日期时间 |
| `plant_task_id`、`job_id`、`del_flag` | 否 | `del_flag` 非 `0`/空的行会被丢弃 |

`mapping_rows`（可选）：`crop_remote_phase_mapping` 中 `if_delete = 0 AND crop_id = 4` 且当前映射
版本的行。不给时用代码内的小麦试点映射，接口机在该表无数据时也是这样。

**b. 接口返回**：`GET /v1/lands/{land_id}/biweekly/seasons` 的整个响应。

**c. 接口返回**：`GET /v1/lands/{land_id}/biweekly/periods?season=…` 的整个响应（单个种植季）。

b、c 中的分期会用 `iter_periods` 重新推导并逐期核对，不一致即报错。

### 2. 地块场景产品 JSON（必需，`--products 目录`）

下载机产出、存放在对象存储 `s1s2_parcel/json/{land_id}/` 下的文件，按原名放进一个目录：
`{日期}_S2.json`、`{日期}_S1.json`。`*_S2_decloud.json` 不读取（线上出报也不读）。

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `land_id` | 是 | 必须等于目标地块 |
| `date` | 是 | 采集日 |
| `sensor` | 是 | `S2` 或 `S1` |
| `ndvi_avg`、`evi_avg`、`ndre_avg`、`ndmi_avg` | 至少一个 | 田块均值；全缺则该景记为「无指数值」不采用 |
| `parcel_cloud_cover_pct`、`cloud_cover` | 否 | 地块云量优先；超过 30% 不采用；都缺时不按云量剔除 |
| `scene_id`、`land_name`、`vv_avg`、`vh_avg` | 否 | 原样带入 |
| `pixel_data` | 否 | `lonlat_columns_v1`：`columns.lon / lat / clear / NDVI / EVI / NDRE / NDMI …`；旧的 `lonlat_v1` 对象数组会在内存中转成列式，数值不变 |

像元产品的日期范围：本季开始前 28 天至本季结束。早于范围的不需要。

### 3. 景清单（可选，`--scenes`）

`agric_satellite.parcel_scene_products` 中该地块的行，JSON 数组或带表头的 CSV。列：
`land_id, date, sensor, scene_id, ndvi_avg, evi_avg, ndre_avg, ndmi_avg, cloud_cover,
parcel_cloud_cover_pct, vv_avg, vh_avg`。

线上以这张表为准。给了就用它判断采用与均值；不给则取产品 JSON 的同名顶层字段（产品入库时
写入的就是这些字段）。`manifest.json` 的 `observation_source` 记录用的是哪一种。

### 4. 气象（可选，`--weather`）

`agric_satellite.weather_daily` 中该地块的行，JSON 数组或 CSV。列：
`date, temperature_2m_min, temperature_2m_max, precipitation_sum, et0_fao_mm`
（也接受 `min_c, max_c, precipitation_mm, et0_mm`）。每天一行。日期范围：本季开始前 30 天至
本季结束。

### 5. 已有 facts 时

如果数据方能导出内部接口 `GET /v1/internal/biweekly/jobs/{job_id}/inputs` 的响应，可以跳过
方案、景、气象，直接渲染这一期：

```bash
python scripts/biweekly_offline_render.py render-facts --facts inputs.json --products products/ --out out/
```

响应带 `error_code` 时拒绝出报。

CSV 的空单元格、`NULL`、`\N` 读成缺测，不读成 0。

## 运行

```bash
python scripts/biweekly_offline_render.py render --land-id 19994 \
    --plan plan.json --products products/ \
    [--scenes scenes.csv] [--weather weather.csv] [--land-name 名称] \
    [--season 2025/26] [--period 7] [--as-of 2026-06-30] [--include-current] \
    --out out/
```

默认生成全部种植季里已经结束的分期。尚未开始的分期和进行中的分期只在清单里记一行，不生成；
进行中的分期可用 `--include-current` 生成。

输出：

```text
out/
├── manifest.json                       # 代码版本、输入文件 sha256、每期结果
└── season-2025-26/
    ├── period-07_2026-01-04_2026-01-17/
    │   ├── 种植双周报_19994_2026-01-04_2026-01-17.pdf
    │   ├── facts.json                  # 接口侧事实（渲染前）
    │   ├── facts-enriched.json         # 渲染实际使用的事实
    │   └── figures/
    └── period-08_…/
        ├── evidence-limited.json       # 本期无可用光学：只有简报，没有 PDF
        └── facts.json
```

退出码：0 全部完成；1 输入有误；2 有分期失败（其余分期照常生成，原因在清单里）。

## 输入不全时

| 情况 | 结果 |
| --- | --- |
| 没有方案，或方案里没有本田小麦批次 | 报错退出，不出报 |
| 本期没有可用光学观测 | 只写 `evidence-limited.json`，不出 PDF；与线上一致 |
| 景被剔除（云量超 30%、云量不可读、无指数值） | 不进入统计，报告的数据质量部分列出剔除数量和原因 |
| 有田块均值、没有像元数据 | 用均值出报，不出田内分布图；报告写明 |
| 上一期没有可用光学 | 向前找最近一个有可用光学的期作对比；都没有则不作变化对比 |
| 没有气象文件 | 不出气象章节，报告写明未提供 |
| 周边 1—2 公里对照 | 离线不取真彩色影像，本节不提供，报告写明「本次为离线生成」 |
| 指标缺测 | 保持缺测，不以 0 代替 |

每期 PDF 的「本期未提供的内容」与 `manifest.json` 里的 `not_provided` 列出同样的条目。

## 测试

```bash
cd services/ingest
python -m pytest tests/test_biweekly_offline_render.py -q
```

测试输入是按真实导出形态构造的测试地块，不是任何真实地块的数据。命令行用例在禁用网络、
并设置假的服务连接配置的子进程里运行，并断言没有加载数据库、Redis、Celery、对象存储和
内部接口相关模块。
