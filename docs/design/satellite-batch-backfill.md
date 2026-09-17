# 按landIdList聚合遥感回填

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

## 聚合与下载

- **只处理请求中的地块**，不纳入数据库中其他邻居。
- 按land_id排序选取未分组地块为锚点，以地块中心建立当地米制等距投影，生成边长5000米的矩形，向四周各延伸2500米。
- 完整边界落入矩形的请求地块并入同组，每个地块只属于一个组；矩形四角也可纳入，不使用圆形半径判断。
- 跨矩形边界的地块另外分组；自身大于矩形的地块独立拉取完整范围，响应`oversized=true`。
- 实际下载范围进一步缩到组内地块的外接矩形，并沿用约0.001°的栅格边缘缓冲。通过已有COG窗口读取拉取所需范围，不下载整景文件。
- 每个组、传感器、日期分片创建一个Job。日期范围两端都包含，分片沿用`INDEX_BACKFILL_CHUNK_DAYS`（默认90天）。
- 每景光谱波段只下载一次，逐地块在内存中重采样到既有网格并掩膜。像元、统计、云量与RGB预览分别按land_id发布；大图预览复用组窗口。
- 跳过已有日期及未完整覆盖地块的景；S2沿用作物季节云量过滤和去云缓存/调度。`force=true`重新处理已有日期，同一任务每地块每天只发布首个成功景。
- 所有地块元数据、已有日期和任务状态通过Internal HTTP读取/更新，下载机无Postgres/远程Redis会话。结果继续通过OSS场景JSON和结果MQ写入`parcel_scene_products`。

## 响应及状态

返回`land_count`、`group_count`、`job_count`、日期范围和`groups`。
每组包含`anchor_land_id`、`land_ids`、`aggregation_bbox`、`download_bbox`、`oversized`、`job_ids`。
两种bbox均为WGS84 `[min_lon, min_lat, max_lon, max_lat]`；`aggregation_bbox`是米制矩形投影回经纬度后的外接范围。

用`GET /v1/jobs/{job_id}`查询分片状态：`pending`、`running`、`completed`、`failed`。
`progress_json`记录`scenes_total`、`scenes_done`、`products_published`、`failed`。
`products_published`表示场景已上传OSS并发布结果消息，数据入库由现有结果写入服务异步完成。
没有新景时任务完成且产出为零；任何下载或地块景处理失败时任务标记失败，已成功结果保留。
API派发部分失败时，错误响应包含`queued_job_ids`和`failed_job_ids`供调用方核对。

MQ、HTTP claim及本地Celery fallback都支持`satellite_batch`任务类型。
若部署显式设置`WORK_CLAIM_TYPES`，需加入`satellite_batch`；不设置时默认支持。
部署需要同时更新API、ingest、mq_consumer；无需数据库表结构变更。
下载端需要配置`API_BASE_URL`和`INTERNAL_API_TOKEN`，本地Celery fallback同样通过Internal HTTP读取任务。
