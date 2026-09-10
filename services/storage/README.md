# storage service

Celery worker consuming the **`storage`** queue (`app.tasks.storage.*`).

Implementation lives in the shared API package (dual-command same image):

- Tasks: `services/api/app/tasks/storage_tasks.py`
- Object storage: `services/api/app/core/storage.py`
- Routes: `celery_app.conf.task_routes` in `services/api/app/worker.py`

Compose wires this as service `storage` with shared volume `scratch:/data/scratch`.

See `docs/design/ingest-storage-split.md`.
