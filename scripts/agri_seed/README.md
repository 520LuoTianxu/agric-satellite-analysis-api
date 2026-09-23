# Agri schema seed (Aliyun PostgreSQL dump)

Imports the Aliyun agricultural data into the single `agric_satellite` application schema in the agric-satellite-analysis Postgres database (default user/db `openfarm`).

## What you get

| Object | Role | Seed rows (sample dump) |
| --- | --- | --- |
| `agric_satellite.virtual_project_areas` | legacy ~5km tiles + vpa10 10×10km tiles | 6158 |
| `agric_satellite.virtual_project_area_lands` | tile ↔ land | 14050 |
| `agric_satellite.land_parcels` | 地块 + `boundary_geojson` | 14050 |
| `agric_satellite.parcel_scene_products` | S1/S2 products (indexes + optional `pixel_data`) | **1000 sample** |
| `agric_satellite.ingest_*` | OSS ingest ledger / runs | full |

- Sensor CHECK: `S1` \| `S2`.
- Optical growth curves: S2 `ndvi/evi/ndmi/ndre/mndwi/cire` averages.
- SAR: S1 `vv` / `vh` averages.
- Pixel JSON on OSS under prefix `s1s2_parcel/json/` (`pixel_data_url` / `json_oss_key`).
- `004_vpa10_virtual_area.sql` adds the vpa10 planning/assignment fields and
  `virtual_project_area_assets`; it is idempotent and does not delete legacy 5km rows.
- vpa10 project-area pixels are stored as gzip-compressed JSON manifests under
  `virtual_project_area/{tile_id}/{sensor}/...`, with a PNG preview alongside them.

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
- `GET /v1/agri/project-areas` — list 项目区 tiles
- `GET /v1/agri/project-areas/{tile_id}`
- `GET /v1/agri/project-areas/{tile_id}/lands`
- `GET /v1/agri/project-areas/{tile_id}/assets?sensor=S1|S2&asset_kind=pixel_json|preview_png`
- `GET /v1/agri/lands/{land_id}`
- `GET /v1/agri/lands/{land_id}/scenes` — time series (`?sensor=S1|S2`, `from`, `to`; `?include_pixels=1` optional)
- `GET /v1/agri/lands/{land_id}/scenes/summary`

Auth: same `Authorization` + `X-Org-Id` as other routers (viewer+ for GET; member+ for imports).

## VPA10 operations

- `POST /v1/admin/virtual-project-areas/initialize` — 建立 10×10 km 项目区与地块归属，不拉取影像。
- `POST /v1/admin/virtual-project-areas/history-backfill` — 按项目区共享下发 S1/S2 历史回填，默认五年。
- 设置 `SCHEDULE_VIRTUAL_AREA_HISTORY_ENABLED=true` 后，下载机 Beat 每周二北京时间 02:30 请求 API 机补齐项目区历史数据；默认关闭，避免部署后自动产生大批历史任务。
- 下载机优先读取 `/internal/virtual-project-areas/{tile_id}/assets` 返回的项目区压缩像素 JSON；缓存命中后只对地块边界做像素裁剪，缓存缺失才搜索并下载新的 10×10 km 窗口。

## Product model (primary)

| Agri | Role |
| --- | --- |
| `virtual_project_areas` | 项目区（旧 5km / VPA10 10km tiles）— list/map entry point |
| `land_parcels` | 地块 with `boundary_geojson` |
| `parcel_scene_products` | S2 optical indices + S1 VV/VH time series |

`/v1/lands` is the canonical parcel API. Every parcel-related API, task, and
report uses `land_parcels.land_id`; there is no second `fields` table or
parcel-ID mapping layer. The `/v1/agri/*` routes are read-oriented scene and
project-area endpoints over the same canonical rows.

## Files in this folder

- `001_agri_schema.sql` — DDL only (CREATE SCHEMA/TABLE/VIEW/INDEX/FK)
- `004_vpa10_virtual_area.sql` — vpa10 10×10km incremental schema migration
- `import_agri_seed.sh` — join + import helper
- `join_export.sh` — concatenate `agri_export.sql.part-*.sql`
- `manifest.json` — part checksums + expected joined sha256


## Assign project areas to farm containers

Create/update one `farms` container per project tile and fill the optional
`land_parcels.farm_id` ownership column. The utility never creates a parcel
mirror, UUID identity, or tag-based mapping.

```bash
python3 scripts/agri_seed/sync_project_areas_to_farms.py
```

Uses `PGHOST`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` or `DATABASE_URL_SYNC`.
Does not delete existing sample farms.

## Agri data plane

See **[docs/agri-first-data.md](../../docs/agri-first-data.md)** for the binding rules:

- RS → `agric_satellite.parcel_scene_products` (`lonlat_v1`) keyed by `land_id`.
- Soil / weather → `agric_satellite` tables keyed directly by `land_id`.
- Ops: `python3 scripts/agri_seed/ensure_land_soil_weather.py --apply`
