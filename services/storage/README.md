# storage service

Celery worker consuming the **`storage`** queue (`app.tasks.storage.*`).

Physical package (lean image, **no GDAL**):

- Tasks: `services/storage/app/tasks/storage_tasks.py`
- Object storage: `packages/openfarm_common` (`ObjectStorage`)
- Compose build context: **repo root** → `services/storage/Dockerfile`

```
celery -A app.worker worker -Q storage --loglevel=info
```

Shared volume: `scratch:/data/scratch`. See `docs/design/ingest-storage-split.md`.
