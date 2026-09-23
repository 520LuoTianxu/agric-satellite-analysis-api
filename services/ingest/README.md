# ingest service

Celery worker consuming the **`ingest`** queue (weather / soil / S1 / S2 /
vegetation / agri bridge / assessment PDF generation).

Physical package under `services/ingest/app/` (own task modules, not an empty
wrapper). Compose build context: **repo root** → `services/ingest/Dockerfile`.

```
celery -A app.worker worker -Q ingest --loglevel=info
```

Shared helpers: `packages/agric_satellite_analysis_common`. Uploads go through
`app.tasks.storage.*` via shared `/data/scratch`.
See `docs/design/ingest-storage-split.md`.

### Scene parallelism

Index jobs (`process_ndvi`, vegetation `process_*`, S1 backfill, agri
optical lonlat) search STAC once, then download and process scenes
concurrently inside that Celery task. Inside each scene, windowed band
reads also overlap (S2 optical typically 7 unique bands; index calculations
2-3; S1 VV+VH).

| Env | Default | Meaning |
|---|---|---|
| `INGEST_SCENE_MAX_WORKERS` | `16` | Thread pool size for per-scene download+process. Independent of Celery `--concurrency` (compose ingest default is 4). |
| `INGEST_BAND_MAX_WORKERS` | `8` | Process-wide cap on concurrent GDAL/rasterio band reads. Nested under the scene pool: per-scene threads are `min(n_bands, cap, (cap * 2) // scene_workers)`. With 8 scene workers that is 2 band threads per scene, not 1. A lone scene uses `min(cap, n_bands)`. |
| `BAND_READ_MAX_ATTEMPTS` | `3` | Application-level total attempts per band. Backoff (`BAND_READ_RETRY_DELAYS_SEC`, default `1,3`) happens after releasing the GDAL semaphore. |
| `GDAL_HTTP_CONNECTTIMEOUT` / `GDAL_HTTP_TIMEOUT` | `10` / `60` | libcurl connect and per-request total timeout in seconds. `GDAL_HTTP_LOW_SPEED_LIMIT=1` plus `GDAL_HTTP_LOW_SPEED_TIME=30` also aborts stalled Range responses. |
| `GDAL_HTTP_MAX_RETRY` | `1` | At most one GDAL-internal retry; keep at `0-1` because the application layer already tries three times. |
| `INDEX_BACKFILL_CHUNK_DAYS` | `90` | Historical task window. Keep 90 initially; switch to 30-45 only if measured tails still require a smaller failure domain. |
| `SATELLITE_BATCH_SOFT_TIME_LIMIT_SEC` / `SATELLITE_BATCH_TIME_LIMIT_SEC` | `1500` / `1800` | Optional whole-task guard. Configure `540` / `600` for a strict ten-minute ceiling; the hard limit recycles a stuck Celery prefork child. |
| `PROCESSING_WINDOW_KM` | `10.0` | Fallback processing square side for legacy jobs; new batch tasks carry the exact transient 10×10 km planner boundary. |
| `WRITE_INDEX_COGS` | unset | Canonical optical path skips index TIF/COG uploads by default. `0` = never. `1` = always (storage-heavy). |
| `UPLOAD_SCENE_JSON` | `1` | Upload compact lonlat_v1 scene JSON under `OSS_PREFIX` (not rasters). |
| `DECLOUD_ENABLED` | `0` | Optional UnCRtainTS parcel-window cloud removal after agri optical ingest. Off by default. See `docs/decloud-uncrtaints.md`. |
| `DECLOUD_MODE` | `batch` | When enabled: buffer land windows, then decloud the job. `per_scene` only if neighbors are already cached. |

Raising Celery concurrency alone still leaves each job looping scenes
serially. Scene-level threads overlap HTTP/GDAL I/O across dates in one
chunk. Each scene thread opens its own SQLAlchemy session; do not share the
parent task session across workers. Band threads do not open DB sessions.

Nested scene x band pools share one process-wide semaphore of size
`INGEST_BAND_MAX_WORKERS`, so 8 scenes x 7 bands does not become 56
simultaneous GDAL opens. `GDAL_NUM_THREADS` stays 1; each band worker
opens its own `rasterio.Env()`.

Look for `scene_parallel_start` / `scene_parallel_done` / `lonlat_upserted`
in ingest logs. Each `band_read_attempt_done` includes
`job_id/scene_id/date/sensor/band/host/attempt/outcome/wait_ms/io_ms/reproject_ms`.
`band_parallel_done` adds P50/P95/max, timeout-retry and slow-band counts.
Agri jobs should log `cog_upload_skipped`, not `cog_uploaded`.
