# FastAPI API service (no Celery worker for ingest/storage).
# Build from repo root:
#   docker build -f services/api/Dockerfile -t openfarm-api .

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev gcc g++ curl && \
    rm -rf /var/lib/apt/lists/*

ENV MPLCONFIGDIR=/tmp/matplotlib
ENV PYTHONUNBUFFERED=1

COPY packages/openfarm_common /packages/openfarm_common
COPY services/api/pyproject.toml .
COPY services/api/alembic.ini .
COPY services/api/alembic ./alembic
COPY services/api/app ./app

RUN pip install --no-cache-dir /packages/openfarm_common \
    && pip install --no-cache-dir .

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app
USER appuser

CMD ["sh", "-c", "for i in 1 2 3 4 5 6 7 8 9 10; do alembic upgrade head && break || echo \"Waiting for DB... attempt $i\" && sleep 3; done && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
