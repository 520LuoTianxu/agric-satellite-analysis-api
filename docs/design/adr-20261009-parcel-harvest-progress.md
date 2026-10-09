# ADR 2026-10-09：地块逐日收获占比（parcel_harvest_progress）

## 背景
需求：按影像日期比较地块“已收获面积占比”，即这一期收了多少、下一期又收了多少；
每天有新影像入库时自动计算并落库。期望形态：收获前为 0，收获期内从 0 单调上升到 100%。

现状：
- `harvest_detect` 只基于**地块平均 NDVI**给出一个收获日，没有像元级分类，也不落库；
- 已入库 S2 `parcel_scene_products.pixel_data`（`lonlat_v1`）带逐像元 `NDVI/EVI/MNDWI/…` 与 `clear`。

## v1 复盘（`s2_ndvi_peak_drop_v1`，已废弃）
v1 每个日期独立判定：回看 150 天取地块均值 NDVI 峰值，峰值后像元 NDVI≤min(0.3, 峰值×0.5) 记为已收获。
在测试环境地块 61251（河北 38.9°N，2026 年仅一季夏玉米）上 2026 年 30 个日期里出现
0%↔100% 来回跳变。用真实像元逐日复现（与库内结果 0 差异）后定位到：

| 现象 | 根因 | 证据 |
|---|---|---|
| 夏秋季 06-15、07-12、07-25、07-30、08-19、09-18、09-28、09-30 等直接 100%/74% | **整景云**：`clear` 全为 0 时 v1 回退为“全部像元有效”，云的 NDVI≈0 被当成 100% 有效且已收获 | 这些日期 `parcel_cloud_cover_pct=100`、clear 像元 0%、原始 NDVI 均值 −0.12～0.25 |
| 1–3 月 01-26 94%、01-31 81%、03-07 99% | **跨季峰值**：150 天回看取到上一季（2025-09 饱和 1.0 或 10-13 的 0.77），冬季休眠/裸土 NDVI≈0.24–0.28<0.3 即判已收获 | 同期 EVI≈0.11–0.12（无作物），MNDWI −0.5～−0.64（非积雪） |
| 4–6 月 10%→76% 逐步上升 | 同上：春季无作物（EVI≤0.17），裸土 NDVI 在 0.3 上下波动，与秋季峰值比较 | — |
| 10-03/10-08 只有 18% | 已收获但 NDVI 仍≈0.39–0.40（>0.3） | 见下“NDVI 偏高” |
| 表中大量 “—” | `newly=max(0,本期−上期)`，占比下降时为 0，前端显示 “—”；根因是逐日独立判定不单调 | — |
| `peak_ndvi=1.0` | 晴空夏季景 98–100% 像元 NDVI=1.000（饱和） | 见下 |

**上游数据问题（不在本 PR 修复，需单独处理）**：Element84 Earth Search `sentinel-2-l2a` 条目的
COG 已扣除 BOA 偏移（`earthsearch:boa_offset_applied=true`，2026-07-15 红波段 DN=260），但其
`raster:bands` 仍声明 `offset=-0.1`，`ingest/tasks/pipeline.py::_resolve_band_radiometry` 照用，
反射率被**重复扣除 0.1**：茂密作物红光 0.026→−0.074，NDVI>1 被截成 1.0；裸土 NDVI 系统性偏高
（01-26 正确 0.15，库内 0.27，按重复扣偏移推算 0.272）。EVI 分母只偏 0.05，基本不受影响。
早期未带 `algorithm_version` 的产品则相反：指数按未定标 DN 计算，NDVI（比值）正确，EVI 被截成 2.0。
另外新旧产品像元网格不同（经纬度网格 vs UTM 网格，偏移数米）。

## 决策：v2（`s2_season_monotonic_v2`）
`services/api/app/core/harvest_progress.py`，纯函数可单测。

1. **观测质控**（每天至多一景，正式产品优先、有效像元多者优先）
   - 只用 `clear=1` 像元；整景无 clear 像元即不可用；仅当像元完全没有 `clear` 字段（旧产品）且景级为正式光学产品时用全部像元；
   - 去云合成景（UnCRtainTS）是模型预测，不作为收获证据；
   - MNDWI（S2 上即 NDSI）≥`snow_mndwi` 的像元视为积雪/水体剔除；
   - 所选指数超出物理范围的像元占比 >`max_bad_radiometry_pct` 的景视为定标异常整景剔除；
   - 有效像元 <`min_valid_pct` 的日期剔除；
   - 地块中位绿度比前后相邻观测（各 ≤`outlier_days` 天）都低 `outlier_drop` 以上的孤立凹陷日剔除。
2. **统一绿度**：按产品选指数——Earth Search `sentinel-2-l2a` 条目（`S2A_50SLJ_20260103_0_L2A` 形式）用 EVI，
   其余（旧产品、PC、Earth Search c1）用 NDVI；线性映射到 0–1 绿度（NDVI 0.15→0、0.85→1；EVI 0.10→0、0.60→1）。
   不同网格的像元按 ≤12 m 最近邻吸附到参考网格（使用最多的网格），像元身份跨产品延续。
3. **生长季**：地块中位绿度 ≥`season_green` 且下一有效观测仍 ≥ 该值 → 返青（季开始，`season_start` 为季标识）；
   峰值后首次跌破该值为成熟/收获期起点，收获窗口持续 `harvest_window_days` 天或到下一次返青为止；
   季外日期 `off_season`、0%（冬季休眠、裸土、积雪月份因此不会被计为收获）。
4. **像元粘滞**：本季曾 ≥`season_green` 的像元为作物像元；绿度降到 ≤`harvest_green` 且 ≤本像元季峰值×(1−`peak_drop`)，
   并被该像元下一次有效观测（`confirm_days` 天内、≤`confirm_green`）确认，即从首次低值日起记为已收获，
   之后保持已收获到季末。最新一期尚无后续观测的候选像元先计入并标 `confirmed=false`，下一期入库后重算确认或撤销。
5. `harvested_pct` = 本季已收获作物像元 / 本季作物像元（季内单调不减，0→100%）；
   `newly_harvested_pct` = 本期 − 同季上一期（不为负），新一季首期从 0 开始。

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `HARVEST_PROGRESS_SEASON_GREEN` | 0.45 | 返青/作物像元绿度阈值（≈NDVI 0.465 / EVI 0.325） |
| `HARVEST_PROGRESS_HARVEST_GREEN` | 0.25 | 已收获绿度上限（≈NDVI 0.325 / EVI 0.225） |
| `HARVEST_PROGRESS_PEAK_DROP` | 0.50 | 相对像元季峰值最小回落比例 |
| `HARVEST_PROGRESS_CONFIRM_GREEN` | 0.35 | 确认观测绿度上限（容许下茬出苗/杂草） |
| `HARVEST_PROGRESS_CONFIRM_DAYS` | 40 | 确认观测最长间隔 |
| `HARVEST_PROGRESS_WINDOW_DAYS` | 60 | 成熟起点后的收获窗口 |
| `HARVEST_PROGRESS_OUTLIER_DROP` / `_DAYS` | 0.20 / 30 | 凹陷日判定 |
| `HARVEST_PROGRESS_MIN_VALID_PCT` | 50 | 有效像元占比下限 |
| `HARVEST_PROGRESS_SNOW_MNDWI` | 0.40 | 积雪/水体 MNDWI 阈值 |
| `HARVEST_PROGRESS_MAX_BAD_RADIOMETRY_PCT` | 20 | 指数越界像元占比上限 |
| `HARVEST_PROGRESS_ENABLED` | true | 关闭后不入队、不启动后台重算 |

### 实测（测试环境真实像元，2025-06～2026-10-08）
- 61251（夏玉米一季，全部 EVI）：2026 年 1–6 月全为季外 0%；06-25 返青；09-10 1.7% → 09-13 4.3% → 09-23 81.0% → 10-03 95.8% → 10-08 98.3%（待确认）；6 个整景云日期剔除。2025 季 09-13 起至 11-12 98.7%。
- 61233 / 61236（冬小麦–夏玉米两季，旧产品 NDVI + 最新两期 EVI）：小麦 06-02→06-22 升至 85% / 93%，07-15 玉米返青归零；玉米 10-03→10-08 升至 54% / 56%（待确认）。2025 年玉米 10-13 达 99% / 97%。
- 61239（辽宁，一季晚熟作物）：2025 年 09-29→11-18 由 4% 升至 100%；2026 年 09-19 开始 12%（待确认）。
所有序列季内单调。

### 存储与触发
- 迁移 `scripts/20261009_parcel_harvest_progress.sql`（可重复执行）：新建或升级 `parcel_harvest_progress`
  （新增 `crop_pixel_count / greenness / peak_greenness / season_start / vegetation_index / confirmed`，删除 `peak_ndvi`，
  状态约束改为 `off_season | growing | harvesting | harvested`，并删除非 v2 结果）与 `parcel_harvest_progress_outbox`。
- 场景结果入库后（`scene_result_cache`）只写 outbox；API 后台 `run_harvest_progress_outbox` 按租约领取，
  `recompute_land` 读取整季上下文（`SEASON_CONTEXT_DAYS=330`），从受影响日期前 `REVISION_DAYS=60` 天重算到今天
  （先删后写，同时删除该地块旧 `method_version` 结果，幂等）。下载机不访问 Postgres。
- 历史补算：`POST /v1/internal/agri/harvest-progress/backfill`（`land_ids` 或 `all_lands`，`date_from`）入队；
  或 `python -m app.services.harvest_progress --land-id L1 --from 2025-01-01 [--enqueue]`。

### 接口
`GET /v1/agri/lands/{land_id}/harvest-progress?from=&to=&include_zero=false`
- 只读当前 `method_version` 的结果；区间内无结果时现场计算并入队（`source=live`）；
- 默认隐藏 0%（季外与未开始收获）；新增 `season_start / vegetation_index / greenness / confirmed` 字段；
- 返回 `heuristic=true` 与 `rule_zh`，前端提示为估算值。

## 局限
- 光学影像无法区分“已收割”与“完全枯黄未收割”；秸秆覆盖、倒伏、间套作会影响判断；
- 阴雨季可能长时间无有效观测，最新几期可能尚未确认；S1 VV/VH 可作为后续补充信号；
- 绿度端元与阈值为经验值，需结合农艺实测校准；
- Earth Search 重复扣偏移问题修复并重处理历史景之前，NDVI 不可直接用于其他绝对阈值业务。
