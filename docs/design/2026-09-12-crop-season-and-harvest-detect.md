# Design: 作物生育窗（春/夏玉米）+ 遥感收获日检出

**Date:** 2026-09-12  
**Status:** draft-for-user-review  
**Repo:** agric-satellite-analysis  
**Approved direction:** 用户确认两块一起设计；收获日只标「有景且呈收获表现」的日期，不做预报/外推。

## Problem

1. **拉数生育期过粗：** 玉米在 `CROP_SEASONS` 中固定为夏玉米 6–9 月。春玉米被同一套默认窗驱动时，拉数、去云、干旱/长势口径都会错。
2. **刷新遥感 UX：** 虽已有 `growing_seasons` 多窗，但主路径不够「先选作物 → 春/夏或自定义播种～结束」。未选手动窗时易掉回 6–9。
3. **收获时间：** 需要能统计「哪一天的遥感已经表现出收获」，而不是按物候模型预估一个未来/空日。

## Goals

1. 拉数/分析前：用户选择作物（含春玉米 / 夏玉米）并确认或自定义生育窗（播种日起～结束日；可多窗轮作）。
2. ingest / decloud / 干旱 / 长势 / 报告口径统一消费该窗；**禁止**再把所有玉米一律当 6–9 月。
3. 在选定生育窗内，基于官方光学 NDVI 时序检出**候选收获日**（仅有数据的 scene date）。
4. UI 可展示候选日 + 证据；可点开真彩核对。缺景/云遮标 `uncertain`，不编造日期。

## Non-goals

- 不预报尚未发生的收获日。
- 不要求第一版做完整物候阶段轴（苗期/拔节/抽雄…）；若后续需要可另开设计。
- 不把「日历推算收获」当作主结果（可作弱参考备注，不写进主字段）。

## Current anchors (as-is)

| Area | Today |
|------|--------|
| 作物季默认 | `services/ingest/app/core/crops.py` → `CROP_SEASONS["corn"]` = 6–9 / peak 7–8 |
| 拉数传窗 | 前端 `agri-timeseries-panel` 组 `growing_seasons` → API `mq_publish` → ingest |
| 窗语义 | 已有月窗 / 多窗轮作偏好（用户记忆）；需升级到日期级播种～结束并与春/夏玉米预设对齐 |
| 官方 NDVI | drought / NDVI 挑选逻辑已区分 official / decloud quality |

## Design

### A. 作物与生育窗

#### A.1 作物目录

- 新增（或等价建模）：
  - `corn_spring`：默认约 **4–8 月**（华北春玉米起点，可配置）
  - `corn_summer`：默认约 **6–9 月**（现有夏玉米）
- 保留 `corn` 作别名 → 默认映射 `corn_summer`（兼容旧地块），并在 UI 引导改选春/夏。
- 其它作物仍用现有 `CROP_SEASONS`；自定义窗可覆盖任何作物默认。

#### A.2 窗口模型

```text
GrowingSeasonWindow:
  start_date: "YYYY-MM-DD"   # 播种/生育开始（用户大致选择）
  end_date:   "YYYY-MM-DD"   # 种植/生育结束
  label?: string             # 可选：春玉米2025 / 夏玉米2025
```

兼容：若只传 `start_month`/`end_month`/`months[]`，后端归一成当年或任务年份的日期窗（与现有 decloud 窗解析共存一个归一函数）。

#### A.3 拉数 / 刷新 UX

1. 选择或确认地块作物（春玉米 / 夏玉米 / 其它）。
2. 默认填入该作物预设窗；用户可改「播种日起～结束日」。
3. 支持多窗（轮作）；每一窗独立进入 `growing_seasons[]`。
4. 提交 backfill / refresh 时 **必带** `crop_key`（或地块已绑定作物）+ 归一后的 `growing_seasons`。
5. 文案：「未选手动窗则用作物默认生育期」——且默认已按春/夏区分。

#### A.4 管道消费

- `mq_publish` / ingest job meta：持久化本次任务的 `crop_key` + `growing_seasons`。
- decloud STAC 云阈值季节逻辑、干旱是否 in-season、长势/报告阴影带：全部改为读任务窗（或地块绑定窗），不再写死玉米 6–9。
- 地块绑定作物变更时：后续新任务用新默认；历史产品不强制重算，除非用户再点刷新。

### B. 收获日检出（观测，非预报）

#### B.1 输入

- 地块 + 某一个 `GrowingSeasonWindow`（或任务窗列表逐窗处理）。
- 窗内官方光学场景序列：`date`, `ndvi_avg`（或等价官方曲线所用值）, quality/official 标记, 可选 `rgb_url`/`large_rgb_url`。

#### B.2 收获表现定义（v1）

在窗内按日期排序的官方清晰点上：

1. **曾在生长：** 存在至少一景 NDVI ≥ `grow_min`（可配，默认如 0.35，或相对窗内峰值比例）。
2. **收获跌落：** 候选日 `d` 相对其前 `k` 个清晰官方点的中位/均值，下降幅度 ≥ `drop_frac`（可配，默认如 0.35 相对跌幅或绝对 ΔNDVI）。
3. **收后维持：** `d` 之后至少 `m` 个清晰点（若有）仍低于「生长」阈值；若窗末无后续点，则仅用跌落条件并降置信度为 `low`。
4. **只输出有景的 `d`：** 从不在两景之间插值出一天。

多候选时：取**最早**满足「跌落 + 收后维持」的日期作为主 `harvest_date`；其余进 `alternates[]`。

云多 / 官方点过少（如清晰点 < 4）：`status=uncertain`，不写死收获日。

#### B.3 输出模型

建议表或 JSON（地块×窗×年）：

| Field | Meaning |
|-------|---------|
| `land_id` | 地块 |
| `window_label` / `start_date` / `end_date` | 所用生育窗 |
| `harvest_date` | 候选收获景日期，可空 |
| `status` | `detected` \| `uncertain` \| `no_growth` |
| `confidence` | `high` \| `medium` \| `low` |
| `evidence` | 前后景日期、NDVI、跌幅、所用阈值 |
| `scene_id` | 对应光学 scene（便于打开真彩） |

API：`GET` 地块收获检出；或挂在 agri timeseries 汇总里。

#### B.4 UI

- 时间轴：收获候选日专用标记（与干旱点区分）。
- 详情：一句话证据 + 链到该日真彩（`large_rgb` 优先）。
- 明确文案：「根据遥感表现检出，不是预报」。

### C. 配置

环境变量或 settings（示例名）：

- `HARVEST_GROW_MIN_NDVI`
- `HARVEST_DROP_FRAC`
- `HARVEST_LOOKBACK_K`
- `HARVEST_CONFIRM_M`

第一版用默认值上线，按样例地块再调，不阻塞管道。

## Pipeline sketch

```text
UI: crop + windows
  → API refresh/backfill (crop_key, growing_seasons)
  → MQ ingest/decloud (filter & score by windows)
  → parcel_scene_products (official NDVI series)
  → harvest_detect(land, window)  # batch or on-demand
  → store harvest result + UI marker
```

## Rollout order

1. **作物预设 + 拉数窗 UI/API 归一**（纠正春/夏玉米错误，优先级最高）。
2. **管道全面吃自定义/春夏窗**（decloud / drought / 报告文案去硬编码夏玉米）。
3. **收获检出服务 + 前端标记**（只读已有官方序列，可先 on-demand）。
4. 样例地块回归（春玉米窗 vs 夏玉米窗各一）+ 阈值微调。

## Risks / open points

- 云序列导致「收获跌落」落在第一张清晰收后景，实际收获可能早于该日 → UI 必须写「不晚于/该日已呈收获表现」类表述（推荐文案：**「该日遥感已呈收获后表现（不早于上一清晰景）」**）。
- 去云 bad 重建 NDVI 偏低可能假跌落 → **仅用官方/good 点**。
- 一年两熟双窗：每窗独立检出，避免跨窗误用峰值。

## Success criteria

- [ ] 春玉米地块刷新不再默认 6–9；可选春玉米预设或自定义播种～结束。
- [ ] 夏玉米行为与现网兼容（默认 6–9）。
- [ ] 收获检出只返回真实 scene 日期或 `uncertain`，无插值日。
- [ ] UI 可区分「检出收获表现」与普通 NDVI 点，并可打开真彩核对。
