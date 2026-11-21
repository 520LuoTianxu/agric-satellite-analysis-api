# T6 种植双周报后台离线逻辑

## 状态与硬边界

本轮为可运行、可测试的**离线逻辑**，不是生产就绪或已对接服务。前端PR17保持Mock，未改动前端仓库。没有新增/挂载HTTP路由、Beat/Celery/MQ任务、定时器、生产迁移、真实数据源请求、AI调用或OSS上传；默认关闭、默认DenyAll。只有显式`--offline`才能运行合成示例。

安全发现：当前`services/api/app/middleware/auth.py`写明`AUTH_DISABLED=True`，返回匿名用户且不做组织过滤。不能把它当租户鉴权。新API服务未引用该旁路；正式对接前须提供经过验证的JWT主体、租户和地块归属授权适配器，不能信任请求body中的tenant_id。此轮不擅自改旧鉴权。

## 快速运行（全部本地）

Python3.11+；正常服务已有ReportLab/PyPDF依赖。轻量独立测试环境可使用：

```sh
python3 -m venv /tmp/biweekly-offline-venv
/tmp/biweekly-offline-venv/bin/pip install reportlab==4.4.3 pypdf==6.1.0 ruff==0.12.0
export PYTHONPATH=packages/agric_satellite_analysis_common:services/ingest:tests/biweekly_offline
/tmp/biweekly-offline-venv/bin/python -m unittest discover -s tests/biweekly_offline -v

# 默认dry-run，不读输入、不创建输出目录
/tmp/biweekly-offline-venv/bin/python scripts/biweekly_offline_demo.py

# 显式合成输入；不要改成生产源/真实地块数据
/tmp/biweekly-offline-venv/bin/python scripts/biweekly_offline_demo.py \
  --offline --input examples/biweekly/synthetic-wheat.json \
  --output /tmp/biweekly-offline-output
```

产出：本地`offline-api-control.sqlite`、不可变`*.snapshot.json`、`job-manifest.json`、内容寻址`artifacts/<sha256>.pdf`和经权限用例取回的`report-MOCK.pdf`。再次运行同一输入返回相同job、attempts仍为1，不重复调用worker。失败需显式`api.retry()`，不是自动循环；输入/模板版本变化创建不同job。

SQLite只是本地单进程/多连接测试的**API控制面模拟实现**。此CLI在一台离线电脑上模拟两侧，不是部署到下载机的入口。下载机worker模块仅依赖WorkGateway、ArtifactPort和渲染/文案端口；将来必须通过鉴权Internal HTTP领取冻结快照与回报结果，绝不能向API Postgres/Redis开连接。

## 模块与职责

- 共享包`agric_satellite_analysis_common.biweekly`：
  - `snapshot.py`：紧凑源契约校验、试验作物策略/计划三阶段、质量与缺失、内容哈希。
  - `models.py`：不可变Snapshot/Narrative/Lease/Artifact、错误码、固定任务版本。
  - `narrative.py`：去除租户/地块/资产私密标识的模型输入、严格结构化输出与数值/单位/证据引用校验、默认无AI模板。
  - `application.py`：请求/状态/原始页/重试/失效/下载用例，逐次授权；离线Gateway模拟API。
  - `repository.py`：显式本地事务/租约/幂等/重试状态，不在导入时连接任何服务。
  - `artifacts.py`：暂存后原子发布、内容寻址、读取SHA256校验，拒绝路径穿越和符号链接。
  - `ports.py`：源/权限/内部工作网关/AI/渲染/存储适配器契约。
- API侧`services/api/app/services/biweekly_reports.py`：未注册的默认关闭工厂。生产仓库/源/授权不在本轮伪实现。
- 下载侧`services/ingest/app/reports/biweekly/worker.py`：单次显式执行，冻结输入→结构化文案→PDF→产物清单→结果回报；不发现待处理地块、不查询DB、不导入环境配置。
- `pdf_render.py`：复用现有ReportLab及`land_assessment/fonts/wqy-zenhei.ttf`；中文字体缺失明确失败，不静默生成方块字。输出含计划阶段、证据、缺失、文案与快照/模型/提示词/模板版本。

旧Bailian/报告编排会自行读环境、查库或走宽松文本接口，因此本轮只复用PDF依赖/字体，不直接调用旧编排。没有默选模型或付费服务。

## 源契约与科学边界

`examples/biweekly/synthetic-wheat.json`是唯一随仓库提供的示例，明确`data_kind=mock`。完整输入包含：tenant_id、field_id（用户确认等于landId）、season_id、crop、geometry_fingerprint、source_revision、plan_version、processing_versions（mask/grid/aggregation/calibration）、14天`[start,end)`、资产ID、计划窗口、采样质量和紧凑证据。

- SourcePort必须在API侧、授权后装配输入。此轮没有声称存在可用的prec_agric生育期查询API。真实适配器负责查找种植实例/组件/版本及规范化stage_key；未知作物/映射拒绝，不按文字猜阶段。
- 冬小麦/玉米映射为版本化**离线草案**。BTU排期仅为`planned_btu_not_observed`，不是卫星实测。计划窗口禁止重叠；空缺明确标记，不补满。跨期按报告起始日主归属、覆盖期分别计天，不重复建报。
- 证据只能为小型聚合字段；数值必须有限、单位/范围/时间窗口匹配，缺失必须null、不补零，不接像元大数组。源资产/方法、几何与处理版本参与快照身份。
- NDVI等仅在输入质量满足离线试验阈值（有效观测至少2日、有效像元至少0.5、最大间隔至多10天）时进入`facts`。这些是验证流程的草案数值，不是已批准的农学发布阈值；正式使用须确认。
- **没有FVC地面校准样本，且LAI无批准模型**：即使输入带值也只留审计，`availability=not_validated`，不进入模型/网页可解释`facts`，PDF不展示其数值；绝不由NDVI伪造FVC/LAI。前端将来必须用facts/availability而非把audit evidence.value当有效指标。
- 灌溉、墒情、完整Sentinel-1雷达等灾害证据缺失时明确限制，不输出确定灾害、产量、损失。未实现遥感反演、天气下载或回填。

## 文案校验与一致性

默认`narrative_mode=template, model_version=none`，明确标注未调用AI。注入NarrativeProvider时，model_version/prompt_version必须匹配任务，且只收到crop、窗口、计划阶段、可用紧凑facts及限制；无租户ID/地块ID/原始资产路径或像元。

输出仅允许`{summary,claims:[{text,evidence_refs,values:[{evidence_id,value,unit}]}]}`。自由文案禁数字（含Unicode中文数值）、比例与危险定性；数值仅能在values中精确引用同条已存在可用事实，单位也必须一致。未知证据、未校准指标、布尔数值、虚构数值/单位、越界字段/长度均拒绝，任务失败而不是假装AI成功。API控制面完成时再次按权威快照验证。

这些是保守规则，不是对任意语言语义的科学证明；真实模型仍需预算/供应商审批、固定提示词/策略评估与人工验收。网页原始事实与PDF同snapshot，PDF可以更详细，但不能另造事实。PDF模板版本必须与渲染器实际版本匹配，不能仅改清单标签。

## 状态、幂等、失效和下载

- key包含snapshot SHA256、template/prompt/model/mode/language；max_attempts为首个任务的操作配置，不通过重复提交抬高上限。
- queued→running→ready或failed。原子领取、随机租约token、过期隔离；并发重复领取只成功一次。失败/过期需显式重试，最多配置次数；旧worker不能完成/覆盖新尝试。
- 无心跳/自动回收调度器；离线claim/retry会处理过期。未来长任务适配器需设计鉴权心跳、持久队列与运维回收，不能直接开现有定时器。
- invalidation原因仅SOURCE_CHANGED/POLICY_REVOKED，保留历史快照但阻止领取/完成/重试/下载；只允许有权限读取历史raw审计。源变化需要未来可信事件适配器显式触发失效，当前不会偷偷轮询生产源。
- ready必须有PDF、页数/长度/SHA256、snapshot/文案/模型/提示词/模板版本一致。下载每次重新检查租户和地块权限，再校验文件内容/长度。当前是字节Download对象，不声称已有真实下载URL；未来私有OSS/签名链接仍需鉴权且不能暴露内部存储键。

## 明确待实现/待决策

1. 生产JWT/租户/地块授权：当前旧鉴权匿名旁路，必须先修正或替换其接入链；不可只打开feature开关。
2. 正式DB仓库与迁移、API阶段/天气/场景源适配、Internal HTTP领取/结果提交、OSS私有存储/签名、路由契约/速率限制/任务调度。此轮没有实现/运行这些生产适配器。
3. 地块实例/作物映射清单、发布质量阈值、跨期归属最终规则、FVC端元/地面样本、LAI模型输入和验证。无样本不能承诺精度。
4. 模型供应商/型号/预算、数据留存与审查策略；没有选择付费AI，没有读取凭证。
5. 正式工作队列的租约心跳/取消/清理与事务性产物提交、源失效事件、产物保留期与部署审批。

本轮不改现有采集/索引/作业状态系统，不执行生产SQL。以上适配器完成后，也必须等用户明确说「对接」才改前端运行数据源。


## 本地可触发接口（2026-11-21）

默认不出报。触发入口是 API 机 `/v1/lands/{land_id}/biweekly/*`（默认 `BIWEEKLY_API_ENABLED=0`）。前端走现有登录态与 `/satellite-api`；补数走 `create_satellite_batch_jobs` / 出站箱或 WorkItem；下载机复用已有 `satellite_batch` 领取；生成在 API 机读入库产品。不走 `AUTH_DISABLED` 匿名旁路。`scripts/biweekly_local_api.py` 不是入口。说明：`/workspace/reports/biweekly-persistent-feature/README.md`。
