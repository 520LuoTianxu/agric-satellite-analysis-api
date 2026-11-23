# 双周报 PDF 链路审查与改造说明（2026-11）

审查基准：`master` @ `0eb9ddc`（含 #66–#70）。下文「原代码位置」均指该提交。
范围：数据 → 指标 → 阶段 → 叙述 → 绘图 → PDF，即
`TriggerService.prepare_generate_facts`（API 侧事实）→ Internal HTTP →
`generate_official_biweekly_pdf` → `enrich_official_report_facts` → `render_official_biweekly_pdf`。

## 1. S1–S3 的业务含义

`S1`/`S2`/`S3` 是小麦的三个**生长阶段组**，定义在
`packages/agric_satellite_analysis_common/.../biweekly/phase_policy.py`（映射版本 `wheat-smart-pilot-1`，待农学审核），
不是哨兵一号、二号、三号卫星。传感器字段里的 `S1`/`S2` 才是卫星，两者是不同的命名空间。

| 代号 | 正式名称 | 包含的生育期 | 主要指标 | 辅助指标 |
| --- | --- | --- | --- | --- |
| S1 | 营养生长阶段 | 出苗期至返青期 | NDVI | EVI、NDMI |
| S2 | 营养生长与生殖生长并进阶段 | 拔节孕穗期 | NDRE、EVI | NDVI、NDMI |
| S3 | 生殖生长阶段 | 抽穗期至成熟期 | NDRE、NDMI | NDVI、EVI |

指标组合沿用仓库已有的 `PHASE_METRICS`，本次没有新增指标，也没有新增阈值。

## 2. 审查发现

### 高

| # | 问题 | 原代码位置 |
| --- | --- | --- |
| H1 | 田内图每个面板各自自动缩放（`vmin=None`，`YlGn`），0.20–0.32 与 0.30–0.475 都把各自最大值涂成深绿；不同日期、不同田块的图不能比较 | `services/ingest/app/reports/biweekly/context_maps.py:441-449` |
| H2 | 变化图用 `RdYlGn` 且未以 0 居中，颜色不代表升降方向 | `context_maps.py:454` |
| H3 | 每个阶段只画 `metrics[0]` 一个指标，S2、S3 的第二个主要指标没有图 | `context_maps.py:354-369` |
| H4 | 指标用替代词：「绿度」「增强绿度」「叶片营养」「植被水分」 | `phase_policy.py:135-140`，`context_maps.py:33` |
| H5 | 最低三分之一的像元恒被称为「偏低」，任何负变化都叫「变差」，正文固定写「田内并不均匀」；相对本田偏低与绝对值低没有区分 | `context_maps.py:248-316,430-435`，`official_pdf.py:596` |
| H6 | 像元级云掩膜（`clear=0`）未使用，云和云影像元照常出图并计入统计 | `context_maps.py:155-170` |
| H7 | 文字用 API 的 `prior_comparison`，图用下载机侧回看 28 天的另一对日期，正文与图的对比基准可能不一致 | `trigger.py:531-558`，`enrichment.py:118-122` |
| H8 | 缺测被画成 0：降水柱、周边 2 公里柱；周边对照判断里 `or 0` | `official_pdf.py:414,570`，`context_maps.py:782` |
| H9 | 气象合计不披露缺测天数，部分天数缺测时合计偏小 | `weather_fetch.py`（`summarize_weather_rows`） |
| H10 | 阶段转换从未显示：读取 `date`/`start`/`stage_code`，而事实里的键是 `on`/`to_stage` | `official_pdf.py:619-626` |
| H11 | 叙述口语化并带因果暗示：「这两周」「读数」「更应看」「水分账」「存在失墒压力」（仅凭 ET0 大于降水） | `official_pdf.py:499-500,610-689,832-945` |

### 中

| # | 问题 | 原代码位置 |
| --- | --- | --- |
| M1 | `scipy` 未在依赖中声明却被直接导入；缺包时整份 PDF 失败 | `context_maps.py:572`，`services/ingest/pyproject.toml` |
| M2 | 周边对照整段没有异常保护，任何异常都会让报告生成失败 | `enrichment.py:290-345` |
| M3 | 指标折线图横轴为类别轴（间隔与天数无关），纵轴自动缩放 | `official_pdf.py:295-386` |
| M4 | 事实里没有云量和被剔除的过境，报告无法说明数据质量 | `services/api/app/services/biweekly_scenes.py`，`trigger.py` |
| M5 | 跨阶段周期 `primary_metrics` 为空，正文没有指标 | `phase_policy.py`（`display_phase`） |
| M6 | 打包数据只含 `assets/*.ttf`，封面底图和标志 PNG 不随包安装 | `services/ingest/pyproject.toml` |
| M7 | 图片按固定高度绘制，超出页面时无兜底 | `official_pdf.py:103` |
| M8 | 单个离群像元即可进入「偏低位置」的文字描述 | `context_maps.py:234-316` |
| M9 | 统计只用均值，无分位数和有效像元占比，易受极值影响 | `context_maps.py`，`official_pdf.py` |
| M10 | 全角「；」「，」「：」可出现在行首（reportlab 禁则表不含） | reportlab `ALL_CANNOT_START` |

### 低

| # | 问题 | 位置 |
| --- | --- | --- |
| L1 | API 侧保留一份未启用的旧渲染器（`use_official_pdf=False`），措辞为旧版 | `services/api/app/reports/biweekly/official_pdf.py` |
| L2 | 阶段代号 `S1`–`S3` 与卫星名同形，正文直接出现易被误读 | `official_pdf.py`（`stage_label`） |

## 3. 改动

### 指标定义与固定色标

- 新增 `biweekly/metric_specs.py`：四项指标的正式名称、一句用途说明、计算式、有效范围、色标范围和低值参考线。

| 指标 | 有效范围 | 色标范围 | 低值参考线 |
| --- | --- | --- | --- |
| 归一化植被指数（NDVI） | −1 至 1 | 0 至 1.0 | 0.30 |
| 增强型植被指数（EVI） | −1 至 2 | 0 至 1.2 | 0.20 |
| 归一化差值红边指数（NDRE） | −1 至 1 | 0 至 0.8 | 0.20 |
| 归一化差值水分指数（NDMI） | −1 至 1 | −0.5 至 0.5（以 0 为中心） | 0.00 |

  - 有效范围与入库裁剪一致（`services/ingest/app/tasks/indices.py`，EVI 裁剪到 −1 至 2）。
  - 低值参考线取 `INDEX_REGISTRY` 的 `AlertDefaults.threshold`，是平台通用值，未按作物和生育期校准；报告里只用来描述数值高低，并在说明页注明。回归测试锁定两处一致。
  - NDVI 上限 1.0 是指数的理论最大值，不是本田或本期的最高像元。植被与裸土通常在 0 至 1；小于 0 的像元（水体、积雪等）单独用蓝灰色。
- 新增 `reports/biweekly/scales.py`：色标不接收数据，任何日期、任何田块都是同一把尺。植被类指数为棕 → 浅黄（参考线处）→ 绿，参考线以下不出现绿色；变化图为红–蓝、相对差异图为橙–紫，均以 0 居中且正负对称，幅度取 |值| 的第 98 百分位向上取整到固定档位；缺测、云、云影为灰色。

### 稳健统计与数据质量

- 新增 `reports/biweekly/metric_stats.py`：有效像元 = 数值有限、在有效范围内、未被 `clear=0` 标为云或云影。田块水平用中位数和第 10–90 百分位；有效像元少于 10 个时记为缺测，不写 0。
- 「显著低于本田整体水平」采用箱线图离群规则（低于 Q1 − 1.5×IQR），是统计口径，不是农学阈值；常数场不标记任何像元。
- 不少于 4 个相邻像元（8 邻域，约 400 平方米）才算成片并给出方位；零星像元只计数，不作为核查位置。依据：哨兵二号几何定位误差约一个像元，孤立像元在田间无法可靠定位。
- API 侧观测带上 `cloud_cover`、`parcel_cloud_cover_pct`、`exclusion_reason`；事实新增 `prior_observations`、`rejected_optical`。报告列出每次过境的云量、有效像元占比和采用情况。
- 气象合计带 `precipitation_days`、`et0_days`、`temperature_days`；有缺测时正文写明「14 天中 2 天缺测，合计只含 12 天」，图上缺测日为灰色、不画 0。

### 阶段与多指标

- `report_metric_plan`：单一阶段用该阶段的主要、辅助指标；跨阶段时按最近一次有效观测所在阶段选取并说明；没有种植方案时四项并列、不作阶段判读。
- 每个主要指标一页田内图（`infield_maps.py`）：① 基准日绝对值 ② 最近日绝对值 ③ 变化量 ④ 相对本田中位数的差异（黑框为成片显著偏低像元）。
- 全报告只用一对日期：本期最近一次有效观测对比它之前的那一次（本期内没有则取前期），文字、表和图一致。变化量按印出的两位小数相减，读者可自行核对。
- 阶段背景（`STAGE_BACKGROUND`）只写该生育期植被指数的常见走向（低位、上升、持平、下降），并注明是一般规律，不是本田实测结论。

### 叙述与版式

- 文字集中在 `report_text.py`：先结论，再证据表，再田间核查建议；只描述水平、方向和位置，不写「好转」「变差」，不判定病虫害、缺肥、干旱等原因。
- `official_pdf.py` 改用 platypus 流式排版：一、本期结论；二、指标与数据质量；三、田内空间分布；四、周边农田对照；五、气象条件；说明。没有数据的章节不出页，统一列在「本期未提供的内容」。图片按剩余页高缩放，页眉页脚带页码。
- 指标变化图：每个指标一个小图，纵轴固定为该指标的色标范围，横轴为真实日期，带第 10–90 百分位竖线、低值参考线和未采用的过境。
- 周边对照改用「超绿指数（ExG）」正式名称和中位数，并写明它与 NDVI 不是同一指标；整段加异常保护，失败时记录原因。
- 依赖声明 `scipy`；打包数据加入 `assets/*.png`；补全全角标点的行首禁则。

## 4. 样稿与复现

仓库内没有脱敏的实测像元 fixture：`tests/biweekly_offline` 依赖本地的 6417 数据包（仓库中不存在），
`examples/biweekly/synthetic-wheat.json` 是只有田块均值的模拟输入（`data_kind: mock`），没有像元。
因此样稿使用 `reports/biweekly/demo_data.py` 生成的**示例数据：非实测，不用于农事决策**。

- 来源：固定随机种子的数值生成器。田块、坐标、阶段日期、指数、云量和气象均为虚构，不对应任何真实地块。
- 用途：展示版式和各生长阶段的指标组合，并覆盖低值田块、云影、被剔除的过境、离群像元、气象缺测等情形。
- 限制：数值之间的关系是人为设定的，不能用来评价指标或阈值的农学有效性；周边对照的底图是合成图像，不是卫星影像。
- 标识：封面、每页页眉、水印和「使用范围」均注明示例数据。

事实由真实的 `TriggerService.prepare_generate_facts` 构建，PDF 由正式入口 `generate_official_biweekly_pdf` 生成。全程只读写本地文件，不访问数据库、Redis、对象存储和内部接口。

```bash
pip install -e packages/agric_satellite_analysis_common -e services/ingest pymupdf
python scripts/biweekly_sample_report.py --out /tmp/biweekly-samples --pages
```

| 场景 | 生长阶段 | 主要指标 | 覆盖的情形 |
| --- | --- | --- | --- |
| `s1-tillering` | 营养生长阶段（分蘖期） | NDVI | 全田低于 0.30；东北部成片偏低；一次过境因云量被剔除 |
| `s2-jointing` | 营养生长与生殖生长并进阶段（拔节孕穗期） | NDRE、EVI | 云影像元为灰色；东部成片偏低但绝对值不低；周边对照 |
| `s3-grainfill` | 生殖生长阶段（灌浆期） | NDRE、NDMI | 指数季节性下降；只有零星离群像元；气象有缺测日 |

## 5. 测试

```bash
python -m pytest packages/agric_satellite_analysis_common/tests -q
PYTHONPATH=services/ingest python -m unittest discover -s services/ingest/tests -v
cd services/api && python -m unittest discover -s tests -v
```

新增回归测试：

- `services/ingest/tests/test_biweekly_report_quality.py`：NDVI 色标固定 0 至 1、0.30 及以下不为绿色、跨日期跨田块同尺、各指标各自范围、对称色标与离群值、云掩膜与缺测、全无效、常数场、成片规则、三个阶段指标不同、跨阶段、缺测不为 0、三份样稿的正式术语和禁用词、示例标识、气象缺测披露。
- `packages/agric_satellite_analysis_common/tests/test_biweekly_metric_specs.py`：指标定义与阶段指标组合。
- `services/api/tests/test_biweekly_scenes.py`：云量与剔除原因随观测下发。

## 6. 未处理事项

- `wheat-smart-pilot-1` 的阶段划分、指标组合和阶段背景仍待农学审核；低值参考线未按作物和生育期校准。
- 其他作物没有阶段映射，按「无阶段」处理：四项指标并列，田内图用 NDVI。
- API 侧未启用的旧渲染器（L1）只改了指标名称和一句措辞，建议后续删除。
- 没有用真实地块数据验证过新版式；上线前应以一块实测地块生成并人工核对。
