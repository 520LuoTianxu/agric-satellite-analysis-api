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

Index jobs (`process_ndvi`, vegetation `process_*`, S1 backfill) search STAC
once, then download and process scenes concurrently inside that Celery task.

| Env | Default | Meaning |
|---|---|---|
| `INGEST_SCENE_MAX_WORKERS` | `16` | Thread pool size for per-scene download+process. Independent of Celery `--concurrency` (compose ingest default is 4). |

Raising Celery concurrency alone still leaves each job looping scenes
serially. Scene-level threads overlap HTTP/GDAL I/O across dates in one
chunk. Each thread opens its own SQLAlchemy session; do not share the
parent task session across workers.

Look for `scene_parallel_start` / `scene_parallel_done` in ingest logs
(includes `workers`, `scenes`, `layers_created`).
