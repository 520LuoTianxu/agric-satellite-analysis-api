"""Reference surface for storage ops.

Canonical Celery tasks live in ``services/api/app/tasks/storage_tasks.py``
(``app.tasks.storage.*``) because compose uses the API image for the storage
worker. Keep this file as the design-doc pointer; do not diverge logic here.
"""

# Re-export names for documentation / future physical split.
UPLOAD_FILE = "app.tasks.storage.upload_file"
PUT_BYTES = "app.tasks.storage.put_bytes"
EXISTS = "app.tasks.storage.exists"
PUBLIC_URL = "app.tasks.storage.public_url"
