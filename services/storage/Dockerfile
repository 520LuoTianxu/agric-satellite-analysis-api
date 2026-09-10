# Storage Celery worker image.
# Compose builds with context=services/api (same runtime as API/ingest);
# this Dockerfile is for standalone builds from the monorepo root:
#   docker build -f services/storage/Dockerfile -t openfarm-storage services/api
#
# Runtime command (compose): celery -A app.worker worker -Q storage ...

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[all]" 2>/dev/null || pip install --no-cache-dir .

COPY . .

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app \
    && mkdir -p /data/scratch && chown -R appuser:appgroup /data/scratch
USER appuser

ENV OPENFARM_SCRATCH_DIR=/data/scratch

CMD ["celery", "-A", "app.worker", "worker", "-Q", "storage", "--loglevel=info", "--concurrency=2"]
