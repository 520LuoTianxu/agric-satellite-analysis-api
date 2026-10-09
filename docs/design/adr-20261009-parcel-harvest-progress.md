# ADR 2026-10-09：地块逐日收获占比（parcel_harvest_progress）

## 背景
需求：按影像日期比较地块“已收获面积占比”，即这一期收了多少、下一期又收了多少；
每天有新影像入库时自动计算并落库。

现状调研：
- `harvest_detect`（`/agri/lands/{id}/harvest-detect`）只基于**地块平均 NDVI**给出一个收获日，
  没有像元级“已收获/未收获”分类，也不落库。
- 已入库的 S2 `parcel_scene_products.pixel_data`（`lonlat_v1`）带逐像元 `NDVI` 与 `clear`，
  `/agri/lands/{id}/ndvi-day-grade-shares` 已按日聚合像元等级——收获占比沿用这一数据与口径。

## 决策
### 算法（首版启发式，`method_version = s2_ndvi_peak_drop_v1`）
`services/api/app/core/harvest_progress.py`，纯函数可单测：
1. 每天一景：正式光学产品（`is_official_optical_product`）优先，其次有效像元多者；
2. 有效像元：任一像元 `clear=1` 时仅用 clear 像元；有效占比 < `min_valid_pct` 的日期不计；
3. 季节峰值 = 回看 `lookback_days` 内各日像元均值 NDVI 最大值；
   峰值 < `grow_min` → `no_growth`（0%）；当日不晚于峰值日 → `growing`（0%）；
4. 峰值之后像元 NDVI ≤ min(`ndvi_max`, 峰值×(1−`peak_drop`)) 记为已收获；
5. `harvested_pct` = 已收获/有效像元；`newly_harvested_pct` = max(0, 本期−上一有效期)。

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `HARVEST_PROGRESS_NDVI_MAX` | 0.30 | 已收获像元 NDVI 上限 |
| `HARVEST_PROGRESS_PEAK_DROP` | 0.50 | 相对季节峰值最小回落比例 |
| `HARVEST_PROGRESS_GROW_MIN` | 0.35 | 峰值低于此值视为未种植/未长起 |
| `HARVEST_PROGRESS_LOOKBACK_DAYS` | 150 | 季节峰值回看天数（≥30） |
| `HARVEST_PROGRESS_MIN_VALID_PCT` | 50 | 有效(无云)像元占比下限 |
| `HARVEST_PROGRESS_ENABLED` | true | 关闭后不入队、不启动后台重算 |

阈值需农艺实测校准；调整算法时升级 `method_version`，新旧结果并存。

### 存储与触发
- 迁移 `scripts/20261009_parcel_harvest_progress.sql`：
  `parcel_harvest_progress`（主键 land_id+obs_date+sensor+method_version）与
  `parcel_harvest_progress_outbox`（每地块一条，`date_from` 合并取最早）。
- 场景结果入库成功后（`scene_result_cache`，与预警重算同一位置）只写 outbox；
  API 进程后台 `run_harvest_progress_outbox` 按租约领取，`recompute_land` 从受影响日期重算到今天
  （区间内先删后写，幂等）。下载机不访问 Postgres，符合 `download-host-no-direct-pg-redis`。
- 历史补算：`POST /v1/internal/harvest-progress/backfill`（`land_ids` 或 `all_lands`，`date_from`）入队；
  或 `python -m app.services.harvest_progress --land-id L1 --from 2026-01-01 [--enqueue]`。

### 接口
`GET /v1/agri/lands/{land_id}/harvest-progress?from=&to=&include_zero=false`
- 默认当年 1 月 1 日至今，最长 2 年；`include_zero=false` 默认隐藏 0% 日期；
- 优先读表（`source=stored`）；区间内尚无记录时现场计算并入队（`source=live`）；
- 返回 `heuristic=true` 与 `rule_zh`，前端需提示为估算值。

## 局限
- 秸秆覆盖、留茬、倒伏、晚播、间套作会影响 NDVI，可能误判；
- 只用 S2 光学，阴雨季可能长时间无有效观测；S1 VH/VV 变化后续可作为补充信号；
- 像元级占比按有效像元计算，云遮挡部分不外推。
