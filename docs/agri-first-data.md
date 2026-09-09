# Agri-first data plane

How OpenFarm fields relate to the `agri` schema after the lonlat_v1 / no-OSS work.

## Remote sensing (truth)

| Source | Role |
| --- | --- |
| **`agri.parcel_scene_products`** | **Primary** RS for agri parcels. Prefer `pixel_data.format = lonlat_v1` at insert/ingest time. Served via `GET /v1/agri/lands/{land_id}/scenes?include_pixels=1`. |
| `public.raster_layers` / `public.field_stats` | **Legacy bridge only** (classic OpenFarm COG pipeline + optional `sync_agri_scenes_to_field_stats.py`). Not required for agri field detail / 色斑图 / growth curves. |

Rules:

- Fields tagged `agri:<land_id>` **must not** enqueue `backfill_indices_for_field` (COG / STAC index backfill). Manual `POST /fields/{id}/backfill-indices` rejects agri-tagged fields.
- Ingest / seed must write **lonlat_v1** pixels into `parcel_scene_products` at insert time when available. Do not rely on a later OSS pull for demo offline paths.
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

- Full STAC → lonlat_v1 worker
- Dropping soil/weather tables or moving soil into `parcel_scene_products`
