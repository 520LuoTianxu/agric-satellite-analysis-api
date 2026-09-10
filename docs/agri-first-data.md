# Agri-first data plane

How OpenFarm fields relate to the `agri` schema after the lonlat_v1 / no-OSS work.

## Object storage (uploads / COGs)

Default object store is **Aliyun OSS** (`STORAGE_BACKEND=oss`). MinIO is optional and only starts with `docker compose --profile minio up`. Classic (non-agri) Celery `write_cog` uploads go through `app.core.storage.get_storage()`; TiTiler reads those COGs via the OSS S3-compatible API (`TILER_*` / `OSS_*`). See README § Object storage.

Agri satellite jobs **do not** upload index `.tif` / COG products (`WRITE_INDEX_COGS` defaults off for agri). Compact lonlat JSON under `OSS_PREFIX` is optional (`UPLOAD_SCENE_JSON`, default on). Existing index TIFs can stay; new agri runs must not add more.

## Agri satellite data flow

```
STAC (Sentinel-2 L2A / Sentinel-1 GRD)
  → download source bands (windowed to parcel)
  → compute indices in memory (NDVI, EVI, NDMI, NDRE, CIre, MNDWI; S1 VV/VH dB)
  → sample parcel geometry → lonlat_v1 pixel JSON
  → upload lonlat JSON to OSS
  → publish one MQ result per scene (producer mq_result_writer pulls OSS → upserts PG)
  → optional compact JSON object under OSS_PREFIX
```

`bridge_after_backfill` only waits for those jobs and publishes the MQ result. It does **not** HEAD/GET `cogs/{org}/{field}/{date}/ndvi.tif`. The OSS TIF scanner (`bridge_stac_cogs_to_agri_lonlat`) is a one-shot migration tool (`agri_bridge` / `mode=bridge_only`).

Env knobs (ingest worker):

| Env | Default | Meaning |
| --- | --- | --- |
| `WRITE_INDEX_COGS` | unset | Agri: no index rasters. Classic fields: COGs on. `0` never, `1` always. |
| `UPLOAD_SCENE_JSON` | `1` | Compact lonlat JSON to `OSS_PREFIX` (not rasters). |

## Remote sensing (truth)

| Source | Role |
| --- | --- |
| **`agri.parcel_scene_products`** | **Primary** RS for agri parcels. Prefer `pixel_data.format = lonlat_v1` at insert/ingest time. Served via `GET /v1/agri/lands/{land_id}/scenes?include_pixels=1`. |
| `public.raster_layers` / `public.field_stats` | Classic OpenFarm COG + TiTiler path only. Not required for agri field detail / 色斑图 / growth curves. |

Rules:

- Fields tagged `agri:<land_id>` use the lonlat-direct ingest path (not per-index COG backfill). Manual refresh still runs satellite jobs; they write lonlat, not TIFs.
- Dedup for agri uses dates already in `parcel_scene_products`, not `raster_layers` (old COG rows must not block lonlat).
- Ingest / seed must write **lonlat_v1** pixels into `parcel_scene_products` at insert time when available.
- Do **not** implement soil inside `parcel_scene_products`.

## Soil + weather (still OpenFarm tables)

| Table | Key | Binding |
| --- | --- | --- |
| `soil_profiles` / `soil_layers` / `soil_field_summary` | `field_id` | Same `fields.id` as the UI field |
| `weather_daily` | `field_id` | Same |

Agri parcels bind soil/weather by tagging the OpenFarm field:

```text
tags_json: ["agri:13691", "source:agri.land_parcels", ...]
```

UI uses `parseAgriLandId(tags)` for RS (`/v1/agri/...`) and the field UUID for soil/weather (`/v1/fields/{id}/soil`, weather APIs).

On field create (including agri-tagged):

- Always enqueue `backfill_weather_for_field` + `fetch_soil_for_field`
- Skip `backfill_indices_for_field` when agri-tagged

Ops helper for existing demo rows:

```bash
python3 scripts/agri_seed/ensure_agri_field_soil_weather.py          # dry-run 范莘·lonlat_v1样例
python3 scripts/agri_seed/ensure_agri_field_soil_weather.py --apply  # sync geom if needed + enqueue
python3 scripts/agri_seed/ensure_agri_field_soil_weather.py --apply --all
```

Admin API: `POST /v1/admin/ensure-agri-soil-weather` (owner) — same enqueue for the org’s agri-tagged fields.

## Product UI

- NdviTab / monitoring: if `agri:<land_id>` present → **only** agri timeseries / 色斑 path (`AgriTimeseriesPanel`). No COG backfill CTA.
- Soil / weather tabs keep using `field_id`.

## Out of scope (this pass)

- Dropping soil/weather tables or moving soil into `parcel_scene_products`
