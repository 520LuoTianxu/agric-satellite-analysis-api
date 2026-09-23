# 按地块清单动态规划10km窗口的遥感回填

`POST /v1/lands/backfill-indices/batch`，返回HTTP 202。

```json
{
  "landIdList": ["4933", "4934", "4935"]
}
```

只传`landIdList`即可调用：`date_to`默认取接口调用当天，`date_from`默认回溯三个日历年，包含S1、S2。
例如2026-09-16调用时，默认范围为2023-09-16～2026-09-16，包含首尾日期；闰日回溯到非闰年时取2月28日。
日期字段无需传入。仍兼容可选的`date_from`、`date_to`和`months`（默认36，允许1～120）；未指定开始日期时按日历月回溯。
兼容`landIdlist`和`land_ids`。允许整数地块编号，转换为字符串并去重。
一次最多1000个编号，空清单、空编号、无效日期返回422；不存在或已删除的地块返回404且整批不派发。
复用现有写权限依赖（owner/admin/member）；当前项目鉴权模块处于匿名绕过模式，接口行为与现有地块接口一致。

## 动态聚合与下载

- 本轮输入地块经 Smart 补齐/数据库查询后，统一进入 10×10km 动态窗口规划器；不查询持久化分组，也不匹配历史窗口。
- 每轮从剩余地块中选全局候选 anchor，按稀缺地块保护、成员数量、紧凑度等评分选窗口；窗口中心由规划器优化，不以 anchor 地块中心固定居中。
- 普通组保证所有成员地块完整落在精确 `processing_boundary_geojson` 内；超过10km的地块独立处理，使用覆盖其完整范围的窗口。
- 每个本轮空间组、传感器、日期分片创建一个 Job，参数仅带地块ID、临时窗口边界和查询日期，不持久化项目区ID/归属。日期两端都包含，分片沿用 `INDEX_BACKFILL_CHUNK_DAYS`。
- worker 每次重新搜索 STAC 并读取 COG，不从 OSS 或本地磁盘复用10km窗口像素，不上传10km级影像/JSON/PNG。只在内存中共享读取一次，再按各地块边界裁剪/重采样并保存单地块 JSON 产品。
- S2 默认走 AWS/Element84 公共 Sentinel-2 COG；STAC搜索无结果/失败或主源 COG 读取失败时，才降级到 Planetary Computer 并使用 SAS 签名。S1 继续使用现有 Planetary Computer/STAC 流程。
- 跳过已有日期及未完整覆盖地块的景；S2沿用作物季节云量过滤和去云缓存/调度。`force=true`重新处理已有日期，同一任务每地块每天只发布首个成功景。
- 所有地块元数据、已有日期和任务状态通过Internal HTTP读取/更新，下载机无Postgres/远程Redis会话。结果继续通过OSS场景JSON和结果MQ写入`parcel_scene_products`。
- 日增量、周度过期补拉、Smart同步、手动单地块/批量回填、报告拉取和管理员历史回填都使用同一无状态规划与任务构造器。旧`satellite_analysis`消息仍保留兼容消费者，但新增业务入口不再生产逐地块任务。

## 响应及状态

返回`land_count`、`group_count`、`job_count`、日期范围和`groups`。
每组包含`anchor_land_id`、`land_ids`、`aggregation_bbox`、`download_bbox`、`processing_boundary_geojson`、`oversized`、`job_ids`。
两种bbox均为WGS84 `[min_lon, min_lat, max_lon, max_lat]`；10×10公里真实窗口以Job中的`processing_boundary_geojson`为准，bbox用于检索和响应展示。

用`GET /v1/jobs/{job_id}`查询分片状态：`pending`、`running`、`completed`、`failed`。
`progress_json`记录`scenes_total`、`scenes_done`、`products_published`、`failed`。
`products_published`表示单地块结果已发布，数据入库由现有结果写入服务异步完成。
没有新景时任务完成且产出为零；任何下载或地块景处理失败时任务标记失败，已成功结果保留。
API派发部分失败时，错误响应包含`queued_job_ids`和`failed_job_ids`供调用方核对。

MQ、HTTP claim及本地Celery fallback都支持`satellite_batch`任务类型。
若部署显式设置`WORK_CLAIM_TYPES`，需加入`satellite_batch`；不设置时默认支持。
部署需要同时更新API、ingest、mq_consumer；无需数据库表结构变更。
下载端需要配置`API_BASE_URL`和`INTERNAL_API_TOKEN`，本地Celery fallback同样通过Internal HTTP读取任务。
