# Agri-first data plane

How the canonical `land_parcels` records relate to the `agric_satellite` schema after the lonlat_v1 / OSS work.

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
  → if DECLOUD_ENABLED and cloud > 30%: cache parcel windows; after the job
     has enough nearby S2 (+ S1) context, batch UnCRtainTS decloud
     (additive lonlat product; only quality=good is official)
```

`bridge_after_backfill` only waits for those jobs and publishes the MQ result. It does **not** HEAD/GET `cogs/{org}/{field}/{date}/ndvi.tif`. The OSS TIF scanner (`bridge_stac_cogs_to_agri_lonlat`) is a one-shot migration tool (`agri_bridge` / `mode=bridge_only`).

Env knobs (ingest worker):

| Env | Default | Meaning |
| --- | --- | --- |
| `WRITE_INDEX_COGS` | unset | Agri: no index rasters. Classic fields: COGs on. `0` never, `1` always. |
| `UPLOAD_SCENE_JSON` | `1` | Compact lonlat JSON to `OSS_PREFIX` (not rasters). |
| `DECLOUD_ENABLED` | `0` | Optional UnCRtainTS parcel-window decloud. Additive product; only quality `good` enters official drought metrics. See `docs/decloud-uncrtaints.md`. |
| `DECLOUD_MODE` | `batch` | When decloud is on: buffer many field windows, then decloud. `per_scene` only if neighbors are already cached. |

## Remote sensing (truth)

| Source | Role |
| --- | --- |
| **`agric_satellite.parcel_scene_products`** | **Primary** RS for agri parcels. Prefer `pixel_data.format = lonlat_v1` at insert/ingest time. Served via `GET /v1/agri/lands/{land_id}/scenes?include_pixels=1`. |
| `agric_satellite.raster_layers` / `agric_satellite.field_stats` | Optional legacy COG + TiTiler path. Canonical parcel detail, 色斑图, and growth curves read the same `land_id` from `land_parcels` / `parcel_scene_products`. |

Rules:

- Canonical parcels use the lonlat-direct ingest path (not per-index COG backfill). Manual refresh still runs satellite jobs; they write lonlat, not TIFs.
- Dedup for agri uses dates already in `parcel_scene_products`, not `raster_layers` (old COG rows must not block lonlat).
- Ingest / seed must write **lonlat_v1** pixels into `parcel_scene_products` at insert time when available.
- Do **not** implement soil inside `parcel_scene_products`.

## Soil + weather tables

| Table | Key | Binding |
| --- | --- | --- |
| `soil_profiles` / `soil_layers` / `soil_field_summary` | `land_id` | Direct FK to `land_parcels.land_id` |
| `weather_daily` | `land_id` | Direct FK to `land_parcels.land_id` |

Soil and weather bind directly to the canonical parcel row:

```text
land_parcels.land_id = "13691"
soil_field_summary.land_id = "13691"
weather_daily.land_id = "13691"
```

The UI uses the same `land_id` for parcel detail, RS, soil, weather, alerts,
and reports. There is no tag parser or UUID translation step. Drought and flood
date classes plus NDVI tooltip cloud / de-cloud text are in
`docs/agri-drought-flood.md`.

On field create (including agri-tagged):

- Always enqueue `backfill_weather_for_land` + `fetch_soil_for_land`
- Satellite index work is also dispatched with the same `land_id`

Ops helper for existing demo rows:

```bash
python3 scripts/agri_seed/ensure_land_soil_weather.py          # dry-run default farm
python3 scripts/agri_seed/ensure_land_soil_weather.py --apply  # enqueue missing tasks
python3 scripts/agri_seed/ensure_land_soil_weather.py --apply --all
```

Admin API: `POST /v1/admin/ensure-agri-soil-weather` (owner) — same enqueue
for the org’s canonical land parcels.

## Product UI

- NdviTab / monitoring: reads the canonical `land_id` timeseries / 色斑 path (`AgriTimeseriesPanel`).
- Soil / weather tabs use that same `land_id`.

## Out of scope (this pass)

- Dropping soil/weather tables or moving soil into `parcel_scene_products`
