# ingest service

Celery worker consuming the **`ingest`** queue (weather / soil / S1 / S2 /
vegetation / agri bridge / assessment PDF generation).

Physical package under `services/ingest/app/` (own task modules, not an empty
wrapper). Compose build context: **repo root** → `services/ingest/Dockerfile`.

```
celery -A app.worker worker -Q ingest --loglevel=info
```

Shared helpers: `packages/openfarm_common`. Uploads go through
`app.tasks.storage.*` via shared `/data/scratch`.
See `docs/design/ingest-storage-split.md`.

### Scene parallelism

Index jobs (`process_ndvi`, vegetation `process_*`, S1 backfill, agri
optical lonlat) search STAC once, then download and process scenes
concurrently inside that Celery task. Inside each scene, windowed band
reads also overlap (S2 agri optical typically 7 unique bands; classic
indices 2-3; S1 VV+VH).

| Env | Default | Meaning |
|---|---|---|
| `INGEST_SCENE_MAX_WORKERS` | `16` | Thread pool size for per-scene download+process. Independent of Celery `--concurrency` (compose ingest default is 4). |
| `INGEST_BAND_MAX_WORKERS` | `16` | Process-wide cap on concurrent GDAL/rasterio band reads. Nested under the scene pool: per-scene threads are `min(n_bands, cap, (cap * 2) // scene_workers)`. With 8 scene workers that is 4 band threads per scene, not 1. A lone scene uses `min(cap, n_bands)`. |
| `WRITE_INDEX_COGS` | unset | Agri: skip index TIF/COG uploads. Classic fields: write COGs. `0` = never. `1` = always (storage-heavy). |
| `UPLOAD_SCENE_JSON` | `1` | Upload compact lonlat_v1 scene JSON under `OSS_PREFIX` (not rasters). |
| `DECLOUD_ENABLED` | `0` | Optional UnCRtainTS parcel-window cloud removal after agri optical ingest. Off by default. See `docs/decloud-uncrtaints.md`. |

Raising Celery concurrency alone still leaves each job looping scenes
serially. Scene-level threads overlap HTTP/GDAL I/O across dates in one
chunk. Each scene thread opens its own SQLAlchemy session; do not share the
parent task session across workers. Band threads do not open DB sessions.

Nested scene x band pools share one process-wide semaphore of size
`INGEST_BAND_MAX_WORKERS`, so 8 scenes x 7 bands does not become 56
simultaneous GDAL opens. `GDAL_NUM_THREADS` stays 1; each band worker
opens its own `rasterio.Env()`.

Look for `scene_parallel_start` / `scene_parallel_done` / `lonlat_upserted`
in ingest logs. Band overlap shows as interleaved `band_read_start` /
`band_read_done` (different `thread=ingest-band-*` names) and
`band_parallel_done` `wall_ms` much smaller than the sum of per-band
`elapsed_ms`. Agri jobs should log `cog_upload_skipped`, not `cog_uploaded`.
