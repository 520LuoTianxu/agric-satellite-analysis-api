"""Storage worker entrypoint pointer.

Compose runs ``celery -A app.worker`` against the API image. A future physical
split can copy ``app.core.storage`` + ``storage_tasks`` here and point
Dockerfile CMD at this module.
"""

raise SystemExit(
    "Use the API image worker: celery -A app.worker worker -Q storage "
    "(see services/storage/README.md and docs/design/ingest-storage-split.md)"
)
