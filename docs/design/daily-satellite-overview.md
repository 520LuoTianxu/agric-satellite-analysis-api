# 全国态势每日刷新运行说明

所有周期任务默认关闭。设置`SCHEDULE_DAILY_SATELLITE_ENABLED=true`后，每天北京时间23:00（15:00 UTC），Beat触发`app.tasks.overview_preagg.refresh_daily_satellite`。下载与计算完成、结果入库确认后生成当天快照；生成完成时间取决于数据量，不是固定23:00立即可见。

`.env`配置：

```dotenv
SCHEDULE_DAILY_SATELLITE_ENABLED=false
SCHEDULE_DAILY_WEATHER_ENABLED=false
SCHEDULE_WEEKLY_INDEX_ENABLED=false
SCHEDULE_OVERVIEW_REFRESH_ENABLED=false
```

按需把对应开关设为true，修改后重建/重启Beat以更新容器环境和注册任务。启动Beat但未开启任何开关时，周期任务列表为空。API与下载机使用相同每日遥感开关，API重启后页面正确展示配置状态。开关只控制新的周期触发；手动调用和已入队批次按现有流程执行。

需要同步部署API、共享包、ingest、Beat及前端。既有MQ消费者/HTTP claim需要包含`satellite_batch`支持，结果写入服务正常运行。下载机配置`API_BASE_URL`、`INTERNAL_API_TOKEN`和本地Celery Redis；保持一个Beat实例。下载机不得直连API Postgres或API Redis。

复用现有`jobs`、`land_parcels`、`parcel_scene_products`和`overview_stats_daily`，无需新增SQL迁移。原每周单地块光学调度和固定时间总览缓存刷新仍可分别显式开启；使用每日聚合时通常保持这两个旧开关关闭。天气需要开启对应开关，既有手动多年回填、去云流程继续使用。

API接口：

| 接口 | 用途 |
| --- | --- |
| `POST /v1/internal/schedule/daily-satellite?as_of=2026-09-16` | 建立/恢复该日全国批次并派发下载，日期默认北京时间当天 |
| `POST /v1/internal/schedule/daily-satellite/{run_id}/finalize` | 检查任务/结果入库，准备好后原子保存快照 |
| `GET /v1/agri/overview/daily?level=country` | 最近快照和当天批次阶段，stats可能为空 |
| `GET /v1/agri/overview/daily?level=province&code=11&as_of=2026-09-16` | 查询指定日省级快照，缺失时stats=null；支持city/county |
| `GET /v1/agri/overview/history?level=country&from=2026-09-01&to=2026-09-16` | 按保存日期返回趋势，单次最多366天，不补缺失日期 |
| `GET /v1/agri/overview/export/stats.csv?daily=true&as_of=2026-09-16` | 导出已保存的该日下级行政区统计，缺失返回404 |

需要立即运行首批时，在下载机服务环境中调用：

```powershell
celery -A app.worker call app.tasks.overview_preagg.refresh_daily_satellite --queue ingest
```

该任务自动发现地块、派发并每5分钟检查完成状态，无需手动轮询finalize。人工Internal HTTP诊断时使用现有Bearer内部令牌。`run_id`可以通过`GET /v1/jobs/{id}`查看全国批次，`params_json.job_ids`用于排查组任务；每组任务的`progress_json.published_products`为入库检查明细。

批次阶段：dispatching → downloading → waiting_results → finished；状态为running、completed或partial。partial的原因记录在error，并保留pending_jobs、failed_jobs和results_pending。无效边界记录invalid_land_ids。修复失败数据后可使用既有批量回填接口补拉；下一统计日重新检查并使用已有最新结果，已保存历史快照保持原值。

首次或没有历史的地块下载近60天；已有地块按传感器最近观测回看7天补新景，按`INDEX_BACKFILL_CHUNK_DAYS`拆分。多年历史从既有`POST /v1/lands/backfill-indices/batch`回填。部署前的日期不会自动拥有每日快照，可在区间分析查看已有历史影像；若人工用as_of重建历史，使用当前已入库的历史影像，它不代表当时实际已保存的实时态势。

验证范围：批次恢复、分组边界、传感器增量窗口、发布结果入库检查、缺测/失败/超时、各级计数与面积一致、历史无实时回退、Celery延时重试及页面静态构建。真实全国下载耗时、Copernicus/STAC访问和生产MQ吞吐需要在部署环境核对。
