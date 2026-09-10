# Ingest Celery worker — GDAL/STAC/raster stack.
# Build from repo root:
#   docker build -f services/ingest/Dockerfile -t openfarm-ingest .

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev gcc g++ curl && \
    rm -rf /var/lib/apt/lists/*

ENV MPLCONFIGDIR=/tmp/matplotlib
ENV OPENFARM_SCRATCH_DIR=/data/scratch
ENV PYTHONUNBUFFERED=1

COPY packages/openfarm_common /packages/openfarm_common
COPY services/ingest/pyproject.toml .
COPY services/ingest/app ./app

RUN pip install --no-cache-dir /packages/openfarm_common \
    && pip install --no-cache-dir .

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app \
    && mkdir -p /data/scratch && chown -R appuser:appgroup /data/scratch
USER appuser

CMD ["celery", "-A", "app.worker", "worker", "-Q", "ingest", "--loglevel=info", "--concurrency=4"]
