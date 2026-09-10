# CloudAMQP result writer (OSS URL → DB).
# Build from repo root:
#   docker build -f services/mq_result_writer/Dockerfile -t openfarm-mq-result-writer .

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

COPY packages/openfarm_common /packages/openfarm_common
COPY services/mq_result_writer/pyproject.toml .
COPY services/mq_result_writer/app ./app

RUN pip install --no-cache-dir /packages/openfarm_common \
    && pip install --no-cache-dir .

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app
USER appuser

CMD ["python", "-m", "app.main"]
