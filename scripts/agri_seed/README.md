# Agri schema seed (Aliyun PostgreSQL dump)

Imports the Aliyun agricultural data into the single `agric_satellite` application schema in the agric-satellite-analysis Postgres database (default user/db `openfarm`).

## What you get

| Object | Role | Seed rows (sample dump) |
| --- | --- | --- |
| `agric_satellite.land_parcels` | 地块 + `boundary_geojson` | 14050 |
| `agric_satellite.parcel_scene_products` | S1/S2 products (indexes + optional `pixel_data`) | **1000 sample** |
| `agric_satellite.ingest_*` | OSS ingest ledger / runs | full |

- Sensor CHECK: `S1` \| `S2`.
- Optical growth curves: S2 `ndvi/evi/ndmi/ndre/mndwi/cire` averages.
- SAR: S1 `vv` / `vh` averages.
- Pixel JSON on OSS under prefix `s1s2_parcel/json/` (`pixel_data_url` / `json_oss_key`).
- Spatial grouping is transient: each request runs the 10×10 km dynamic-window
  planner and keeps its boundary only in the satellite Job parameters.
- The current schema intentionally has no project-area membership or shared
  pixel-cache tables. Only parcel-level products are persisted.

The **213MB** joined SQL dump is **not** committed. Place it locally (gitignored).

## 1) Obtain / join the dump

Either:

**A. Already joined SQL** — copy to:

```text
data/agri_export.sql
```

**B. 8 zip parts** — unzip all parts into one directory so you have:

```text
agri_export.sql.part-001.sql … part-008.sql
join_export.sh
manifest.json
```

Then:

```bash
bash scripts/agri_seed/join_export.sh   # if parts live in scripts/agri_seed/
# or from the parts directory:
bash /path/to/parts/join_export.sh
# then:
mkdir -p data
cp /path/to/parts/agri_export.sql data/agri_export.sql
```

Verify sha256 against `scripts/agri_seed/manifest.json`:

```bash
sha256sum data/agri_export.sql
# expect: 44da8f8a2094758408b21ef52f2ec061d5a437c72a7ae439fdd30b0019f1cf63
```

A cleaned dump without `\restrict` / `\unrestrict` (pg_dump 14+) also works; the import script strips those lines automatically.

## 2) Import

Defaults: Docker Compose service `db`, user/db `openfarm` (from `.env`).

```bash
# Schema only (empty tables) — committed DDL:
make agri-schema
# or:
./scripts/agri_seed/import_agri_seed.sh --schema-only

# Full seed (schema + data):
make agri-seed
# or:
./scripts/agri_seed/import_agri_seed.sh data/agri_export.sql

# Join parts from a directory, then import:
./scripts/agri_seed/import_agri_seed.sh --data-dir /path/to/unzipped-parts

# Drop schema agric_satellite CASCADE then re-import (destructive):
./scripts/agri_seed/import_agri_seed.sh --reset data/agri_export.sql
```

Environment overrides:

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | async SQLAlchemy URL or plain `postgresql://…` |
| `DATABASE_URL_SYNC` | preferred for `psql` if set |
| `POSTGRES_USER` / `POSTGRES_DB` / `POSTGRES_PASSWORD` | docker exec defaults |
| `AGRI_SQL` | default path to dump (`data/agri_export.sql`) |

Idempotency: re-running a full dump without `--reset` will fail on existing PKs. Use `--reset` for a clean reload, or `--schema-only` on a fresh database.

## 3) API

After import (and API restart if needed), authenticated org members can call:

- `GET /v1/agri/stats` — row counts
- `GET /v1/agri/admin/import-status` — same counts (read-only admin status)
- `GET /v1/agri/lands/{land_id}`
- `GET /v1/agri/lands/{land_id}/scenes` — time series (`?sensor=S1|S2`, `from`, `to`; `?include_pixels=1` optional)
- `GET /v1/agri/lands/{land_id}/scenes/summary`

Auth: same `Authorization` + `X-Org-Id` as other routers (viewer+ for GET; member+ for imports).

## 10km historical backfill

- `POST /v1/admin/satellite-batch/history-backfill` — 按本次地块集合动态规划 10×10 km S1/S2 历史下载，默认五年。
- 设置 `SCHEDULE_SATELLITE_HISTORY_ENABLED=true` 后，下载机 Beat 每周二北京时间 02:30 请求 API 机下发历史任务；默认关闭。
- 每轮均重新搜索 STAC 并读取 COG，不读取或上传 10×10 km 像素缓存；只写入单地块 JSON 产品。

## Product model (primary)

| Agri | Role |
| --- | --- |
| `land_parcels` | 地块 with `boundary_geojson` |
| `parcel_scene_products` | S2 optical indices + S1 VV/VH time series |

`/v1/lands` is the canonical parcel API. Every parcel-related API, task, and
report uses `land_parcels.land_id`; there is no second `fields` table or
parcel-ID mapping layer. The `/v1/agri/*` routes are read-oriented scene and
parcel endpoints over the same canonical rows.

## Files in this folder

- `001_agri_schema.sql` — DDL only (CREATE SCHEMA/TABLE/VIEW/INDEX/FK)
- `004_vpa10_virtual_area.sql` — historical migration retained for upgrade ordering
- `005_remove_virtual_project_areas.sql` — drop legacy project-area tables/view after import
- `import_agri_seed.sh` — join + import helper
- `join_export.sh` — concatenate `agri_export.sql.part-*.sql`
- `manifest.json` — part checksums + expected joined sha256


## Agri data plane

See **[docs/agri-first-data.md](../../docs/agri-first-data.md)** for the binding rules:

- RS → `agric_satellite.parcel_scene_products` (`lonlat_v1`) keyed by `land_id`.
- Soil / weather → `agric_satellite` tables keyed directly by `land_id`.
- Ops: `python3 scripts/agri_seed/ensure_land_soil_weather.py --apply`
