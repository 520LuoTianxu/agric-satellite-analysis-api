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
