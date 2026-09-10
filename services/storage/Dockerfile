# Lean storage worker — no GDAL / raster stack.
# Build from repo root:
#   docker build -f services/storage/Dockerfile -t openfarm-storage .

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

COPY packages/openfarm_common /packages/openfarm_common
RUN pip install --no-cache-dir /packages/openfarm_common

COPY services/storage/app ./app

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app \
    && mkdir -p /data/scratch && chown -R appuser:appgroup /data/scratch
USER appuser

ENV OPENFARM_SCRATCH_DIR=/data/scratch
ENV PYTHONUNBUFFERED=1

CMD ["celery", "-A", "app.worker", "worker", "-Q", "storage", "--loglevel=info", "--concurrency=2"]
