# Smart 参数化地块历史回填

管理员任务 `smart-land-backfill` 可通过 `POST /v1/admin/ops/task-runs` 触发：

```json
{
  "task_key": "smart-land-backfill",
  "landIdList": ["1001", "1002"],
  "years": 3,
  "sensors": ["S1", "S2"]
}
```

也可以使用数字闭区间：

```json
{
  "task_key": "smart-land-backfill",
  "from_land_id": "1001",
  "to_land_id": "1100",
  "years": 5
}
```

清单和闭区间不能同时填写，单次最多 1000 个编号。任务先读取有效的
`agric_satellite.land_parcels`，仅对缺失编号从 Smart/MySQL 精确同步，随后按
自然年窗口创建现有 `satellite_batch` S1/S2 分片。Smart 凭据只存在 API 机，
下载机仍通过 Internal HTTP 读取地块和 Job，不直连 Smart 或 API PostgreSQL。

这是管理员显式指定的回填，因此不会套用每日自动同步的基地/面积过滤；仍会
校验 Smart 边界并要求地块能够正常生成遥感处理窗口。

原有 `POST /v1/lands/backfill-indices/batch` 同样支持这两种选地方式和 `years`
字段；旧的 `landIdList`、`months`、`date_from`、`date_to` 请求保持兼容。
