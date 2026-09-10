# Ingest Celery worker — same sources as API (compose uses services/api context).
# Standalone: docker build -f services/ingest/Dockerfile -t openfarm-ingest services/api

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev gcc g++ curl && \
    rm -rf /var/lib/apt/lists/*

ENV MPLCONFIGDIR=/tmp/matplotlib
ENV OPENFARM_SCRATCH_DIR=/data/scratch

COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[all]" 2>/dev/null || pip install --no-cache-dir .

COPY . .

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app \
    && mkdir -p /data/scratch && chown -R appuser:appgroup /data/scratch
USER appuser

CMD ["celery", "-A", "app.worker", "worker", "-Q", "ingest", "--loglevel=info", "--concurrency=4"]
