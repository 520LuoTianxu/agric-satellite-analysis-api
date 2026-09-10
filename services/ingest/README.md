# ingest service

Celery worker consuming the **`ingest`** queue (weather / soil / S1 / S2 /
vegetation / agri bridge / assessment PDF generation).

Compose service name: ``ingest`` (replaces legacy ``processor``).
Same image as ``services/api``; command:

```
celery -A app.worker worker -Q ingest --loglevel=info
```

Large artifact uploads go through ``app.tasks.storage.*`` via shared
``/data/scratch``. See `docs/design/ingest-storage-split.md`.
