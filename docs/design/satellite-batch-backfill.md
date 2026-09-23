# 按landIdList映射虚拟项目区的遥感回填

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

## 项目区匹配与下载

- **只为请求中的地块生成地块结果**，不把同项目区未请求地块加入当前Job。
- 先检查地块是否完整包含于已有虚拟项目区；命中时复用持久化边界和OSS缓存。未命中地块由10×10公里动态窗口规划器分组，按稀缺地块保护、组内地块数量、空间紧凑度及与已有项目区的重叠率贪心选择窗口。
- 新区域窗口由全局候选锚点/候选窗口规划，不固定为某一块地的中心；项目区边界和tile_id持久化，后续Smart同步可继续匹配。
- 超大地块独立规划并完整覆盖。下载机再次验证当前成员地块完整落在项目区边界内，边界已变化且跨出窗口的地块不会被裁断处理。
- 每个项目区、传感器、日期分片创建一个Job，携带`virtual_area_tile_id`和`processing_boundary_geojson`。日期范围两端都包含，分片沿用`INDEX_BACKFILL_CHUNK_DAYS`（默认90天）。
- 缓存命中时直接读取OSS区域像素；缓存缺失时对整个项目区搜索/下载一次，并将S2波段及NDVI/EVI等指数、S1的VV/VH像素以压缩JSON存入OSS。随后按请求地块原边界裁剪和重采样，写入原有地块场景产品及JSON结果。
- 大图预览可以复用项目区窗口；像元、统计和云量仍分别按land_id发布。
- 跳过已有日期及未完整覆盖地块的景；S2沿用作物季节云量过滤和去云缓存/调度。`force=true`重新处理已有日期，同一任务每地块每天只发布首个成功景。
- 所有地块元数据、已有日期和任务状态通过Internal HTTP读取/更新，下载机无Postgres/远程Redis会话。结果继续通过OSS场景JSON和结果MQ写入`parcel_scene_products`。
- 日增量、周度过期补拉、Smart同步、手动单地块/批量回填、报告拉取和管理员历史回填都使用同一项目区规划与任务构造器。旧`satellite_analysis`消息仍保留兼容消费者，但新增业务入口不再生产逐地块任务。

## 响应及状态

返回`land_count`、`group_count`、`job_count`、日期范围和`groups`。
每组包含`anchor_land_id`、`land_ids`、`aggregation_bbox`、`download_bbox`、`oversized`、`job_ids`。
两种bbox均为WGS84 `[min_lon, min_lat, max_lon, max_lat]`；10×10公里真实窗口以Job中的`processing_boundary_geojson`为准，bbox用于检索和响应展示。

用`GET /v1/jobs/{job_id}`查询分片状态：`pending`、`running`、`completed`、`failed`。
`progress_json`记录`scenes_total`、`scenes_done`、`products_published`、`failed`。
`products_published`表示场景已上传OSS并发布结果消息，数据入库由现有结果写入服务异步完成。
没有新景时任务完成且产出为零；任何下载或地块景处理失败时任务标记失败，已成功结果保留。
API派发部分失败时，错误响应包含`queued_job_ids`和`failed_job_ids`供调用方核对。

MQ、HTTP claim及本地Celery fallback都支持`satellite_batch`任务类型。
若部署显式设置`WORK_CLAIM_TYPES`，需加入`satellite_batch`；不设置时默认支持。
部署需要同时更新API、ingest、mq_consumer；无需数据库表结构变更。
下载端需要配置`API_BASE_URL`和`INTERNAL_API_TOKEN`，本地Celery fallback同样通过Internal HTTP读取任务。
