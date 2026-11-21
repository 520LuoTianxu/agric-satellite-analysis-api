"""Local hang of the API-machine biweekly routes (not a production entry).

This process mounts the same router and dedicated auth as app.main, without
Redis, Postgres, outbox, or MQ. Use it to call the trigger APIs on this
machine. Do not deploy this module, do not point it at a production domain,
and do not enable BIWEEKLY_DEV_AUTH in production (不能原样上生产).

    cd services/api
    export PYTHONPATH=.:../../packages/agric_satellite_analysis_common
    export BIWEEKLY_API_ENABLED=1
    export BIWEEKLY_DEV_AUTH=1
    export BIWEEKLY_JWT_SECRET='local-dev-secret-do-not-use-in-prod'
    export BIWEEKLY_ALLOWED_LANDS=6417
    export BIWEEKLY_PACK_ROOT=/workspace/reports/field6417-2025-2026
    export BIWEEKLY_WORK_DIR=/tmp/biweekly-api-jobs
    uvicorn app.biweekly_dev_app:app --host 127.0.0.1 --port 8000

The canonical production-shaped entry remains `app.main:app` (full API
machine). BIWEEKLY_API_ENABLED defaults on there; set 0/false to disable.
Never turn on BIWEEKLY_DEV_AUTH in that environment.
scripts/biweekly_local_api.py is not the trigger entry.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import biweekly

app = FastAPI(
    title="agric-satellite-analysis API (biweekly local hang)",
    version="0.1.0-biweekly-dev",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3214",
        "http://127.0.0.1:3214",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

PREFIX = "/v1"
app.include_router(biweekly.router, prefix=PREFIX)


@app.get("/health")
@app.get("/healthz")
def health():
    from app.core.config import settings

    return {
        "ok": True,
        "service": "biweekly-api-machine-local-hang",
        "production_ready": False,
        "not_for_production": True,
        "auto_publish": False,
        "biweekly_api_enabled": bool(settings.biweekly_api_enabled),
        "biweekly_dev_auth": bool(settings.biweekly_dev_auth),
        "entry": "app.biweekly_dev_app:app",
        "note": "Same routes and auth as services/api. Not a production entry. 不能原样上生产.",
    }
