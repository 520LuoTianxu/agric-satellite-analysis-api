# storage worker 精简镜像：只做 OSS 上传，不装 GDAL / 栅格栈。
# 必须在仓库根目录构建，才能 COPY packages/agric_satellite_analysis_common：
#   docker build -f services/storage/Dockerfile -t openfarm-storage .

FROM python:3.12-slim

WORKDIR /app

# curl 给容器健康检查用，不参与上传逻辑。
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && \
    rm -rf /var/lib/apt/lists/*

# ObjectStorage / Celery factory / 日志都来自共享包，避免本服务再拷一份。
COPY packages/agric_satellite_analysis_common /packages/agric_satellite_analysis_common
RUN pip install --no-cache-dir /packages/agric_satellite_analysis_common

COPY services/storage/app ./app

# 非 root 运行；scratch 目录给 ingest 与本 worker 共享暂存文件。
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app \
    && mkdir -p /data/scratch && chown -R appuser:appgroup /data/scratch
USER appuser

ENV OPENFARM_SCRATCH_DIR=/data/scratch
ENV PYTHONUNBUFFERED=1

# 只消费 storage 队列；concurrency=2 适合上传这类 I/O 任务。
CMD ["celery", "-A", "app.worker", "worker", "-Q", "storage", "--loglevel=info", "--concurrency=2"]
