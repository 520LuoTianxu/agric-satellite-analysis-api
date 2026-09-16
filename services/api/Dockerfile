# FastAPI API service (no Celery worker for ingest/storage).
# Build from repo root:
#   docker build -f services/api/Dockerfile -t openfarm-api .

FROM python:3.12-slim

WORKDIR /app

# curl is used by the compose healthcheck against /healthz.
# Raster/GDAL libs live in the ingest image, not here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

ENV MPLCONFIGDIR=/tmp/matplotlib
ENV PYTHONUNBUFFERED=1

COPY packages/agric_satellite_analysis_common /packages/agric_satellite_analysis_common
COPY services/api/pyproject.toml .
COPY services/api/app ./app

RUN pip install --no-cache-dir /packages/agric_satellite_analysis_common \
    && pip install --no-cache-dir .

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app
USER appuser

# 镜像不包含数据库迁移工具。容器启动只运行 API；结构变更用 scripts/*.sql 在容器外执行。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
