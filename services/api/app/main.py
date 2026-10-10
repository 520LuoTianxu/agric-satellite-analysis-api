"""agric-satellite-analysis API - FastAPI application entry point."""

import asyncio
import re
from contextlib import asynccontextmanager
from time import perf_counter

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.config import settings
from app.core.logging import logger, setup_logging
from app.core.rate_limit import limiter
from app.middleware.trace import TraceIdMiddleware
from app.routers import (
    agri,
    harvest_report,
    alerts,
    assessment,
    season_growth,
    crops,
    farms,
    lands,
    project_monitoring,
    parcel_insights,
    jobs,
    monitoring,
    orgs,
    scouting,
    share,
    soil,
    storage,
    uploads,
    users,
    weather,
    mq_tasks,
    internal_work,
    internal_lands,
    internal_jobs,
    internal_decloud_schedules,
    internal_agri,
    internal_results,
    internal_schedule,
    admin_ops,
    internal_admin,
    satellite_batch,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup / shutdown lifecycle."""
    setup_logging()

    # Create shared httpx client for outbound HTTP (e.g., tile proxy)
    import httpx
    from app.services.scene_result_cache import consume_cached_scene_results

    app.state.http_client = httpx.AsyncClient(timeout=30.0)
    # 回执入队与后台消费共用进程级Redis客户端，复用连接池并由应用生命周期统一释放。
    app.state.redis_client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=10.0,
    )
    # API本机任务保留强引用；启动扫描只恢复claim模式下未写入任何Job的S1回算。
    app.state.api_admin_task_tasks = {}
    app.state.api_admin_recovery_task = asyncio.create_task(
        admin_ops.recover_interrupted_s1_claim_backfills(app),
        name="recover-interrupted-s1-claim-backfills",
    )
    app.state.s1_sigma0_dispatch_outbox_task = None
    app.state.satellite_batch_dispatch_outbox_task = None
    from app.services.work_items import work_queue_mode

    if work_queue_mode() in {"legacy", "dual"}:
        # 事务Outbox仅服务MQ派发模式；claim模式直接原子创建WorkItem，不启动冗余扫描。
        from app.services.s1_sigma0_dispatch_outbox import (
            run_s1_sigma0_dispatch_outbox,
        )
        from app.services.satellite_batch_dispatch_outbox import (
            run_satellite_batch_dispatch_outbox,
        )

        app.state.s1_sigma0_dispatch_outbox_task = asyncio.create_task(
            run_s1_sigma0_dispatch_outbox(),
            name="s1-sigma0-dispatch-outbox",
        )
        app.state.satellite_batch_dispatch_outbox_task = asyncio.create_task(
            run_satellite_batch_dispatch_outbox(),
            name="satellite-batch-dispatch-outbox",
        )
    app.state.harvest_progress_outbox_task = None
    from app.services.harvest_progress import (
        harvest_progress_enabled,
        run_harvest_progress_outbox,
    )

    if harvest_progress_enabled():
        # 新影像入库后的收获占比重算在API本机完成，与下载机隔离。
        app.state.harvest_progress_outbox_task = asyncio.create_task(
            run_harvest_progress_outbox(),
            name="harvest-progress-outbox",
        )
    # API 进程负责从 Redis 消费下载结果，下载机只做 HTTP 入队，不参与数据库写入。
    scene_result_consumer = asyncio.create_task(
        consume_cached_scene_results(app.state.redis_client)
    )
    try:
        yield
    finally:
        app.state.api_admin_recovery_task.cancel()
        if app.state.s1_sigma0_dispatch_outbox_task is not None:
            app.state.s1_sigma0_dispatch_outbox_task.cancel()
        if app.state.satellite_batch_dispatch_outbox_task is not None:
            app.state.satellite_batch_dispatch_outbox_task.cancel()
        await asyncio.gather(
            app.state.api_admin_recovery_task, return_exceptions=True
        )
        if app.state.s1_sigma0_dispatch_outbox_task is not None:
            await asyncio.gather(
                app.state.s1_sigma0_dispatch_outbox_task,
                return_exceptions=True,
            )
        if app.state.satellite_batch_dispatch_outbox_task is not None:
            await asyncio.gather(
                app.state.satellite_batch_dispatch_outbox_task,
                return_exceptions=True,
            )
        if app.state.harvest_progress_outbox_task is not None:
            app.state.harvest_progress_outbox_task.cancel()
            await asyncio.gather(
                app.state.harvest_progress_outbox_task,
                return_exceptions=True,
            )
        scene_result_consumer.cancel()
        await asyncio.gather(scene_result_consumer, return_exceptions=True)
        await app.state.redis_client.aclose()
        await app.state.http_client.aclose()


app = FastAPI(
    title="agric-satellite-analysis API",
    version="0.1.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Trace-Id",
        "X-Request-Id",
        "Hr-Base-Id",
        "X-Account-Id",
    ],
    expose_headers=["X-Trace-Id"],
    max_age=3600,
)

# ── Rate Limiting ────────────────────────────────────────────────────
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": f"Rate limit exceeded: {exc.detail}"},
    )


app.add_middleware(SlowAPIMiddleware)
app.add_middleware(TraceIdMiddleware)


# ── Routers ──────────────────────────────────────────────────────────
PREFIX = "/v1"

app.include_router(users.router, prefix=PREFIX, tags=["users"])
app.include_router(orgs.router, prefix=PREFIX, tags=["orgs"])
app.include_router(farms.router, prefix=PREFIX, tags=["farms"])
app.include_router(lands.router, prefix=PREFIX, tags=["lands"])
app.include_router(project_monitoring.router, prefix=PREFIX, tags=["projects"])
app.include_router(satellite_batch.router, prefix=PREFIX, tags=["lands"])
app.include_router(assessment.router, prefix=PREFIX, tags=["assessment"])
app.include_router(season_growth.router, prefix=PREFIX, tags=["season-growth"])
app.include_router(crops.router, prefix=PREFIX, tags=["crops"])
app.include_router(agri.router, prefix=PREFIX)  # agri-first: 项目区/地块/S1·S2
app.include_router(harvest_report.router, prefix=PREFIX)  # 全部地块收获报表
app.include_router(monitoring.router, prefix=PREFIX, tags=["monitoring"])
app.include_router(jobs.router, prefix=PREFIX, tags=["jobs"])
app.include_router(alerts.router, prefix=PREFIX, tags=["alerts"])
app.include_router(scouting.router, prefix=PREFIX, tags=["scouting"])
app.include_router(share.router, prefix=PREFIX, tags=["share"])
app.include_router(uploads.router, prefix=PREFIX, tags=["uploads"])
app.include_router(storage.router, prefix=PREFIX, tags=["storage"])
app.include_router(weather.router, prefix=PREFIX, tags=["weather"])
app.include_router(soil.router, prefix=PREFIX, tags=["soil"])
app.include_router(parcel_insights.router, prefix=PREFIX)
app.include_router(mq_tasks.router, prefix=PREFIX, tags=["mq"])
app.include_router(internal_work.router, prefix=PREFIX)
app.include_router(internal_lands.router, prefix=PREFIX)
app.include_router(internal_jobs.router, prefix=PREFIX)
app.include_router(internal_decloud_schedules.router, prefix=PREFIX)
app.include_router(internal_agri.router, prefix=PREFIX)
app.include_router(internal_results.router, prefix=PREFIX)
app.include_router(internal_schedule.router, prefix=PREFIX)
app.include_router(admin_ops.router, prefix=PREFIX)
app.include_router(internal_admin.router, prefix=PREFIX)


# ── Health Check ─────────────────────────────────────────────────────
def _health_error_message(exc: Exception) -> str:
    """格式化健康检查异常，避免日志和响应意外暴露连接凭据。"""

    message = re.sub(
        r"((?:postgresql(?:\+\w+)?|redis(?:s)?):\/\/)[^@\s]+@",
        r"\1<redacted>@",
        str(exc),
        flags=re.IGNORECASE,
    )
    return message[:500]


@app.get("/health", tags=["health"])
@app.get("/healthz", tags=["health"], include_in_schema=False)
async def healthz():
    """Check DB connection and Redis ping; /healthz remains a compatibility alias."""
    health_started_at = perf_counter()
    errors: list[str] = []
    redis_scheme = settings.redis_url.partition("://")[0] or "<missing>"
    logger.info(
        "health_check_started",
        database_configured=bool(settings.database_url),
        redis_configured=bool(settings.redis_url),
        redis_scheme=redis_scheme,
    )

    # DB check
    database_started_at = perf_counter()
    logger.info("health_check_database_started")
    try:
        from sqlalchemy import text
        from app.core.database import engine

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info(
            "health_check_database_succeeded",
            duration_ms=round((perf_counter() - database_started_at) * 1000),
        )
    except Exception as exc:
        error_message = _health_error_message(exc)
        errors.append(f"db: {error_message}")
        logger.warning(
            "health_check_database_failed",
            error_type=type(exc).__name__,
            error=error_message,
            duration_ms=round((perf_counter() - database_started_at) * 1000),
        )

    # Redis check
    redis_started_at = perf_counter()
    logger.info("health_check_redis_started")
    try:
        r = aioredis.from_url(settings.redis_url)
        await r.ping()
        await r.aclose()
        logger.info(
            "health_check_redis_succeeded",
            duration_ms=round((perf_counter() - redis_started_at) * 1000),
        )
    except Exception as exc:
        error_message = _health_error_message(exc)
        errors.append(f"redis: {error_message}")
        logger.warning(
            "health_check_redis_failed",
            error_type=type(exc).__name__,
            error=error_message,
            duration_ms=round((perf_counter() - redis_started_at) * 1000),
        )

    if errors:
        logger.warning(
            "health_check_unhealthy",
            error_count=len(errors),
            duration_ms=round((perf_counter() - health_started_at) * 1000),
        )
        return {"status": "unhealthy", "errors": errors}
    logger.info(
        "health_check_succeeded",
        duration_ms=round((perf_counter() - health_started_at) * 1000),
    )
    return {"status": "ok"}
