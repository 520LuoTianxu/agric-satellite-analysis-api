# Agri schema seed (Aliyun PostgreSQL dump)

Imports the Aliyun `agri` schema into the OpenFarm Postgres (default user/db `openfarm`).

## What you get

| Object | Role | Seed rows (sample dump) |
| --- | --- | --- |
| `agri.virtual_project_areas` | 项目区 / ~5km tiles | 6158 |
| `agri.virtual_project_area_lands` | tile ↔ land | 14050 |
| `agri.land_parcels` | 地块 + `boundary_geojson` | 14050 |
| `agri.parcel_scene_products` | S1/S2 products (indexes + optional `pixel_data`) | **1000 sample** |
| `agri.ingest_*` | OSS ingest ledger / runs | full |

- Sensor CHECK: `S1` \| `S2`.
- Optical growth curves: S2 `ndvi/evi/ndmi/ndre/mndwi/cire` averages.
- SAR: S1 `vv` / `vh` averages.
- Pixel JSON on OSS under prefix `s1s2_parcel/json/` (`pixel_data_url` / `json_oss_key`).

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

# Drop schema agri CASCADE then re-import (destructive):
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
- `GET /v1/agri/lands/{land_id}`
- `GET /v1/agri/lands/{land_id}/scenes` — time series (`?sensor=S1|S2`, `from`, `to`; `?include_pixels=1` optional)
- `GET /v1/agri/lands/{land_id}/scenes/summary`

Auth: same `Authorization` + `X-Org-Id` as other routers (viewer+ for GET; member+ for import-as-field).

## Product model (primary)

| Agri | Role |
| --- | --- |
| `virtual_project_areas` | 项目区 (~5km tiles) — list/map entry point |
| `land_parcels` | 地块 with `boundary_geojson` |
| `parcel_scene_products` | S2 optical indices + S1 VV/VH time series |

OpenFarm `/v1/farms` / `/v1/fields` are **legacy** in this fork; UI should target `/v1/agri/*`.

## Files in this folder

- `001_agri_schema.sql` — DDL only (CREATE SCHEMA/TABLE/VIEW/INDEX/FK)
- `import_agri_seed.sh` — join + import helper
- `join_export.sh` — concatenate `agri_export.sql.part-*.sql`
- `manifest.json` — part checksums + expected joined sha256


## Sync project areas → OpenFarm farms/fields

Map each `agri.virtual_project_areas` tile to a `farms` row and each
`agri.land_parcels` parcel to a `fields` row (tagged `agri:<land_id>`, geom
from `boundary_geojson`). Deterministic uuid5 IDs; conflicts skipped.

```bash
python3 scripts/agri_seed/sync_project_areas_to_farms.py
```

Uses `PGHOST`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` or `DATABASE_URL_SYNC`.
Does not delete existing sample farms.

## Bridge agri scenes → OpenFarm monitoring (optional)

For OpenFarm fields tagged `agri:<land_id>`, copy S1/S2 index averages into
`raster_layers` + `field_stats` so classic NdviTab charts work without Celery:

```bash
python3 scripts/agri_seed/sync_agri_scenes_to_field_stats.py
```

Idempotent (`ON CONFLICT` on `uq_raster_field_date_type`). Placeholder
`cog_uri` values look like `agri://land/{land_id}/{sensor}/{date}`.

**色斑图** does **not** use this sync — the web UI reads
`parcel_scene_products.pixel_data` directly via
`GET /v1/agri/lands/{id}/scenes?include_pixels=1` and rasterizes a MapLibre
image overlay client-side.

## Agri-first data plane

See **[docs/agri-first-data.md](../../docs/agri-first-data.md)** for the binding rules:

- RS → `agri.parcel_scene_products` only (`lonlat_v1`). Agri satellite jobs write lonlat-direct (no new index COGs). The OSS TIF scanner is migration-only.
- **Soil / weather** → public OpenFarm tables keyed by `fields.id`, with `agri:<land_id>` tags linking the parcel.
- Ops: `python3 scripts/agri_seed/ensure_agri_field_soil_weather.py --apply`

