# S1/S2 地块产品：Aliyun OSS JSON → PostgreSQL

独立小工具：从 bucket `agric-dev` 前缀 `s1s2_parcel/json/{tile_id}/parcel_{id}/{date}_{S1|S2}.json` 拉取产品 JSON，upsert 到 PostgreSQL schema `agri`。

相对旧脚本 `agri_s1s2_parcel_bundle/scripts/oss_json_to_postgres.py` 的改进：独立 schema/视图/入库日志、更多元数据列、`--apply-schema`、可配置 `OSS_ENV`、兼容 psycopg3/psycopg2。

## 依赖

```bash
cd scripts/s1s2_parcel_oss_pg
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 环境变量

1. 复制示例并填写 **PostgreSQL**（不要把真实 OSS 密钥写进项目）：

```bash
cp .env.example .env
# 编辑 .env：PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE
```

2. **OSS** 凭证仍放在本机文件（默认）：

`/home/box/.config/aliyun/oss.env`

需含：`OSS_REGION`、`OSS_ENDPOINT`、`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`OSS_BUCKET`。

可用环境变量或 `.env` 中的 `OSS_ENV` 指向其它路径；`OSS_PREFIX` 默认为 `s1s2_parcel/json/`。

脚本会自动 `setdefault` 加载项目根目录 `.env`（已有环境变量不被覆盖）。

## 建表

方式 A — 用 psql：

```bash
export $(grep -v '^#' .env | xargs)   # 或自行 export PG*
psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -f sql/001_schema.sql
```

方式 B — 入库时顺带应用：

```bash
python3 scripts/oss_to_pg.py --apply-schema --limit 1
```

主要对象：

| 对象 | 说明 |
|------|------|
| `agri.parcel_scene_products` | 主表，PK `(parcel_id, date, sensor, scene_id)` |
| `agri.v_parcel_scene_products_meta` | 去掉 `pixels` 的轻量视图 |
| `agri.ingest_runs` | 可选入库运行日志 |

更多示例见 `sql/002_sample_queries.sql`。

## 入库

```bash
# 仅列出 OSS key
python3 scripts/oss_to_pg.py --dry-run --limit 20

# 小批量试跑（先建表）
python3 scripts/oss_to_pg.py --apply-schema --prefix s1s2_parcel/json/ --limit 100

# 指定瓦片前缀
python3 scripts/oss_to_pg.py --prefix s1s2_parcel/json/p4079_t00001_a15526/

# 全量（慎用）
python3 scripts/oss_to_pg.py --prefix s1s2_parcel/json/
```

行为要点：

- 默认列举 OSS 前缀（**需要 ListObjects**）；无列举权限见下一节 `--from-done-dir` / `--keys-file`
- upsert：冲突时更新 URL/云量/payload 等，并刷新 `ingested_at`
- 每 50 条 `commit`，打印进度
- 单对象解析失败会记 error 并继续


## 无 ListObjects 权限时

部分 OSS 凭证只有 **GetObject**，没有 **ListObjects**（无法 `ObjectIterator` / 列举 `s1s2_parcel/json/`）。此时请用本地 key 来源，脚本只按 key 调用 `get_object`：

```bash
# 从本地 pipeline done 标记读取 json_oss_key
# done 路径形如：parcel_products/{tile}/_done/parcel_{id}_{date}_{S1|S2}.done
python3 scripts/oss_to_pg.py --from-done-dir output/machine-1/parcel_products --dry-run --limit 20
python3 scripts/oss_to_pg.py --from-done-dir output/machine-1/parcel_products --apply-schema

# 或从文本文件（一行一个 key，# 为注释）
python3 scripts/oss_to_pg.py --keys-file keys.txt --dry-run
python3 scripts/oss_to_pg.py --keys-file keys.txt --limit 100
```

未指定 `--from-done-dir` / `--keys-file` 时仍走 OSS 列举，**需要 ListObjects**。

## 查询示例

```sql
-- 按瓦片/日期/传感器列元数据（无 pixels）
SELECT parcel_id, date, sensor, scene_id, cloud_cover, rgb_url
FROM agri.v_parcel_scene_products_meta
WHERE tile_id = 'p4079_t00001_a15526'
  AND date >= '2025-06-01' AND sensor = 'S2'
ORDER BY date
LIMIT 50;

-- 计数
SELECT sensor, count(*) FROM agri.parcel_scene_products GROUP BY 1;

-- jsonb 抽一个像元 NDVI
SELECT parcel_id, date,
       payload #>> '{pixels,0,ndvi}' AS ndvi0
FROM agri.parcel_scene_products
WHERE sensor = 'S2' AND parcel_id = '15526'
ORDER BY date DESC LIMIT 10;
```

## 目录

```
s1s2_parcel_oss_pg/
  README.md
  requirements.txt
  .env.example
  sql/001_schema.sql
  sql/002_sample_queries.sql
  scripts/oss_to_pg.py
```

**注意：** 不要提交真实 `.env` / 密钥；打包时排除 `.venv`。

## 无 ListObjects / 项目内无 `.done` 时

本仓库**不包含**跑批产生的 `_done` 文件（那些在管道机器的 `parcel_products/` 下）。

已导出的 key 名单：

- `data/oss_json_keys.txt`（从生产机 `_done` 导出，可直接 `--keys-file`）
- `data/oss_json_keys_sample20.txt`（试跑）

```bash
python3 scripts/oss_to_pg.py --keys-file data/oss_json_keys_sample20.txt --dry-run
python3 scripts/oss_to_pg.py --keys-file data/oss_json_keys.txt --apply-schema
```

若你本机有跑批产出目录，也可用：

```bash
python3 scripts/oss_to_pg.py --from-done-dir /path/to/parcel_products --apply-schema
```

