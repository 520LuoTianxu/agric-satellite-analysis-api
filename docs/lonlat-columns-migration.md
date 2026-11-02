# lonlat_v1 → lonlat_columns_v1 历史数据迁移

## 背景

新写入的场景产品使用 `pixel_data.format = lonlat_columns_v1`（等长 `columns` 数组），
旧数据仍为 `lonlat_v1` 对象数组。API / 收获 / 报告 / OSS 回读路径通过
`agric_satellite_analysis_common.lonlat_pixels` **永久双读**。

对外 HTTP 响应里的 `pixels_lonlat` **始终是对象数组**，前端无需改解析。

本仓库提供 **原地转换** 工具（不删像素、不降精度、不 gzip）。**默认 dry-run**，
不会动生产 OSS/DB，除非显式传 `--apply`。

## 推荐顺序

1. **先部署** 含 dual-read 的 API + ingest（本迁移 PR 或已合并的 #35）。
2. Dry-run 摸底：
   ```bash
   python3 scripts/migrate_lonlat_columnar.py inventory-oss --limit 500 --concurrency 8
   python3 scripts/migrate_lonlat_columnar.py inventory-oss --keys-file keys.txt --write-legacy-keys legacy_keys.txt
   python3 scripts/migrate_lonlat_columnar.py inventory-db
   ```
3. 小批量 dry-run 转换：
   ```bash
   python3 scripts/migrate_lonlat_columnar.py convert-oss --keys-file legacy_keys.txt --limit 20 --verbose
   python3 scripts/migrate_lonlat_columnar.py convert-db --limit 20 --verbose
   ```
4. 业务确认无误后，再 `--apply`（建议先单地块 / 单 prefix）：
   ```bash
   python3 scripts/migrate_lonlat_columnar.py convert-oss --keys-file legacy_keys.txt --concurrency 6 \
     --state-file /tmp/lonlat_migrate.state --apply
   python3 scripts/migrate_lonlat_columnar.py convert-db --land-id 7750 --apply
   ```

OSS 上传优先走加速域名：设置 `OSS_ACCELERATE_ENDPOINT=https://oss-accelerate.aliyuncs.com`
（或你们现网的 `agric-prod.oss-accelerate.aliyuncs.com` 对应 endpoint）。大 JSON 会走
`oss2.resumable_upload`（默认 part 4MiB、阈值 ≥5MiB）。

## CLI 子命令

| 子命令 | 作用 |
| --- | --- |
| `inventory-oss` | List/读 key，按 format 计数；可写出需迁移 key 列表 |
| `inventory-db` | `parcel_scene_products.pixel_data` 按 format 计数 |
| `convert-oss` | 原地改写 OSS 场景 JSON（默认 dry-run） |
| `convert-db` | 原地 UPDATE `pixel_data`（默认 dry-run） |

公共约束：转换前后用 `extract_pixels` 做整表 roundtrip 校验；不一致则跳过/报错，不写回。

## 读写路径对照（摘要）

| 路径 | 角色 | 状态 |
| --- | --- | --- |
| `lonlat_pixels.py` | encode/decode + SQL helpers | 权威 |
| ingest `agri_lonlat` / `sentinel1` / `decloud` | 写 columnar | OK（#35） |
| API `routers/agri.py` / `share.py` | 读 → `pixels_lonlat` 对象数组 | OK |
| `harvest_progress` / 报告 data_loader / season_growth | SQL + `extract_pixels` | OK（本 PR 收拢到 SQL_LONLAT_*） |
| `oss_to_pg.py` / `result_apply.py` | 入库透传 pixel_data | OK（双 format） |
| Web `pixels_lonlat` | 只消费 API 对象数组 | 外部 OK，无需改 |
| skill `agri-s1s2-parcel-pipeline` | 文档约定；本地 workflow 无写对象数组脚本 | 见 skill 备注 |

## 明确不做

- 本任务 **不会** 对生产执行 `--apply`。
- 不删除历史 OSS 对象再回灌；只做 in-place convert。
- 不把 API 的 `pixels_lonlat` 改成列式。
