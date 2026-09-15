# TiTiler 历史兼容镜像（当前暂不使用）。
# 默认 Compose 已移除该服务，当前 agri 页面走 OSS + pixel_data 渲染；只有
# 后续重新启用 COG 瓦片链路时才需要构建本镜像。
FROM python:3.11-slim

# Install GDAL system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    gdal-bin \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Run as non-root
RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser \
    && chown -R appuser:appgroup /app
USER appuser

EXPOSE 80

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "80", "--workers", "2"]
