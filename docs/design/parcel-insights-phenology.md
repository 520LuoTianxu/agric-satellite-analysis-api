# 地块分析与自动生育窗

## 使用入口与范围

前端导航「地块分析」（`/zh/insights/`，同时提供英文和西班牙文界面）。项目详情可带入 `groupId` 筛选范围，地块报告页可带入 `fieldId` 预选地块。

- 第一期：选择 1–20 块地，进行横向曲线和空间像元对比、地块体检、历史营销简报下载。建议精细对比时选择 2–4 块同作物地块。
- 第三期：指定历史对照年份，查看同期差异；按所选区间查看种植与收获表现；录入灌溉、追肥等农事措施和可选对照地块，查看前后变化。
- 自动生育窗：地块分析、地块时序页、生长季报告选窗和旧地块评估报告共用有效观测推断口径。支持跨年、多周期、未完整覆盖的周期，允许人工核对与指定一茬。

## 历史报告与近期分析

历史模式的截止日必须早于今天（上海时区），单次范围最多 550 天。生成时保存日期、地块信息、输入措施、有效观测、指标和推断结果；下载同一份报告时只渲染已保存结果，不读取新影像、天气或当前告警。用户可填写报告标题和品牌名称。

近期模式可包含今天，只展示页面结果，不保存历史报告，不提供 PDF 导出。数据更新程度以有效影像的实际观测日期为准。

农事措施随该份历史快照保存；修改条件或措施后重新生成会形成新报告。这里不是独立农事台账，也不会修改农业业务系统的种植记录。

## 数据解释

- 使用已有官方光学产品质量规则，优先清晰原始产品，重建产品仅接受质量为 good 的结果，同一日不重复计数。
- 横向比较采用一对一近邻日期匹配，整组日期跨度不超过 3 天，至少 3 组才计算对比均值和差值。不同作物、播期、管理条件不能直接排名。
- 空间图使用区间内最近有效观测，只展示具有清晰标记的像元，所有地块颜色分级一致。低绿度比例是有效像元中 NDVI < 0.35 的比例，不是受灾面积；空间图分别标注观测日期。用于显示的像元最多 1600 个，统计基于全部有效像元。
- 同期对比按日历日期对齐，闰日映射到目标年 2 月末；不声称已验证历史作物或对齐农学阶段。
- 人工窗口同时限制该地块遥感统计、降雨统计和同期对照日期范围；不会只改变窗口名称而仍计算全年平均。
- 措施前后各至少 2 个有效观测日才计算变化；有对照时，前后分别至少 3 组同步观测才计算相对变化。日期范围外的观测不参与。自然生长、天气和其他管理也会影响结果，变化不能作为增产或措施因果证明。
- 期末分类是遥感表现，不是已播种或已收获面积完成率；不足 6 个有效观测日，或期末已超过 35 天无有效影像时，标为证据不足。

## 自动窗口规则

`ndvi-relative-amplitude-v1`：至少 6 个有效观测日、覆盖至少 45 天，局部中值平滑，使用相对振幅阈值识别绿度起伏。超过 35 天的空缺不跨越推断。只有前后低值覆盖边界时才输出确定的起止候选日，同时保留卫星观测的不确定日期区间。

这是一版可解释的经验算法，尚需实际地块样本校准。输出是冠层生长窗口，不能当成精确播种、出苗、成熟或收获日；完整窗口最高给中置信度。平坦曲线、常绿作物、长期云遮均可能无法推断，不能由此判定未种植。无法识别时不回退到固定 6–9 月。

## 接口与发布

以下路径均位于 API 现有路由前缀内：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/lands/{land_id}/phenology` | 有效观测推断，默认最近 550 天，可传 start_date/end_date |
| POST | `/parcel-insights` | 生成历史快照或近期页面结果 |
| GET | `/parcel-insights` | 历史记录分页 |
| GET | `/parcel-insights/{id}` | 重开已保存结果 |
| GET | `/parcel-insights/{id}/report.pdf` | 从历史快照生成 PDF |

复用 `jobs` 的 `parcel_insights` 类型保存已完成快照，无新增表或迁移。API、前端和共用 Python 包需同步发布；旧评估报告的 API/ingest 副本同步修改。新增查询只在 API 执行，没有新增下载机直连 Postgres/Redis 的路径。

## 验证

PowerShell，在 API 仓库运行：

```powershell
$env:PYTHONPATH = "$PWD\services\api;$PWD\packages\agric_satellite_analysis_common"
python -m unittest discover -s packages/agric_satellite_analysis_common/tests -p test_phenology.py
python -m unittest discover -s services/api/tests -p test_parcel_insights.py
python -m unittest discover -s services/api/tests -p test_assessment_phenology.py
python -m unittest discover -s services/api/tests -p test_assessment_window.py
python scripts/verify_parcel_insights.py
$env:PYTHONPATH = "$PWD\services\ingest;$PWD\packages\agric_satellite_analysis_common"
python -m unittest discover -s services/ingest/tests -p test_land_assessment_pdf.py
python -m unittest discover -s services/ingest/tests -p test_land_assessment_ai.py
```

在前端运行 `npm run build` 和 `python scripts/check-i18n-keys.py`。先启动构建产物的本地静态服务，再运行 `python scripts/verify-parcel-insights-ui.py http://127.0.0.1:3018`，验证选地、自动窗、农事记录、五个视图、PDF 下载、历史重开、近期无报告、移动端、空数据与错误恢复。

验证使用模拟数据及拦截接口，不连接真实业务库。示例 PDF、渲染页、页面截图位于 API 的 `tmp/parcel-insights-qa/`。上线前仍需要实际数据库和影像样本验收。
