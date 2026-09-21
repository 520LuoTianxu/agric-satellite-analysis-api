"""agric-satellite-analysis API - FastAPI application entry point."""

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
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
    internal_agri,
    internal_results,
    internal_schedule,
    admin_ops,
    internal_admin,
    satellite_batch,
)


# TEMP(PROD-DB-INIT): 首次生产发布完成后删除本段初始化逻辑及镜像中的 SQL 文件。
_SCHEMA_INIT_LOCK_KEY = "agric_satellite.schema.init.v1"
_SCHEMA_INIT_OBJECTS = (
    "admin_task_runs",
    "alembic_version",
    "land_parcels",
    "work_items",
    "v_land_parcels_detail",
)


def _schema_init_sql_path() -> Path:
    """定位发布镜像和本地源码中的一次性初始化 SQL。"""
    candidates = (
        Path("/app/agric_satellite.sql"),
        Path(__file__).resolve().parents[3] / "ABflow" / "agric_satellite.sql",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "找不到生产数据库初始化 SQL，期望路径: "
        + ", ".join(str(candidate) for candidate in candidates)
    )


async def _initialize_agric_satellite_schema() -> None:
    """启动时一次性创建业务表；已完成、并发或半初始化状态分别处理。"""
    import asyncpg

    sql_path = _schema_init_sql_path()
    sql_text = sql_path.read_text(encoding="utf-8")
    if not sql_text.strip():
        raise RuntimeError(f"生产数据库初始化 SQL 为空: {sql_path}")

    database_url = settings.database_url.replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    connection = await asyncpg.connect(
        database_url,
        timeout=15,
        server_settings={"search_path": settings.database_schema},
    )
    try:
        async with connection.transaction():
            # 多个 API worker 可能同时启动，事务级 advisory lock 保证只执行一次。
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                _SCHEMA_INIT_LOCK_KEY,
            )

            schema_exists = await connection.fetchval(
                "SELECT to_regnamespace($1::text) IS NOT NULL",
                settings.database_schema,
            )
            if not schema_exists:
                raise RuntimeError(
                    f"数据库中不存在 schema {settings.database_schema}，初始化已停止"
                )

            object_state: dict[str, bool] = {}
            for object_name in _SCHEMA_INIT_OBJECTS:
                qualified_name = f"{settings.database_schema}.{object_name}"
                object_state[object_name] = await connection.fetchval(
                    "SELECT to_regclass($1::text) IS NOT NULL", qualified_name
                )

            existing_objects = [
                name for name, exists in object_state.items() if exists
            ]
            if len(existing_objects) == len(_SCHEMA_INIT_OBJECTS):
                logger.info(
                    "database_schema_init_skipped",
                    schema=settings.database_schema,
                    reason="already_initialized",
                )
                return
            if existing_objects:
                raise RuntimeError(
                    "数据库检测到半初始化状态，已停止启动: "
                    + ", ".join(existing_objects)
                )

            # 原生 asyncpg 执行完整 SQL，保留函数体、视图、触发器和多语句结构。
            logger.warning(
                "database_schema_init_started",
                schema=settings.database_schema,
                sql_file=str(sql_path),
            )
            await connection.execute(sql_text)

            missing_objects: list[str] = []
            for object_name in _SCHEMA_INIT_OBJECTS:
                qualified_name = f"{settings.database_schema}.{object_name}"
                exists = await connection.fetchval(
                    "SELECT to_regclass($1::text) IS NOT NULL", qualified_name
                )
                if not exists:
                    missing_objects.append(object_name)
            if missing_objects:
                raise RuntimeError(
                    "初始化完成后缺少核心数据库对象: "
                    + ", ".join(missing_objects)
                )
            logger.warning(
                "database_schema_init_completed",
                schema=settings.database_schema,
            )
    finally:
        await connection.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup / shutdown lifecycle."""
    setup_logging()

    # TEMP(PROD-DB-INIT): 首次生产发布完成后删除此调用。
    await _initialize_agric_satellite_schema()

    # Create shared httpx client for outbound HTTP (e.g., tile proxy)
    import httpx
    from app.services.scene_result_cache import consume_cached_scene_results

    app.state.http_client = httpx.AsyncClient(timeout=30.0)
    # API 进程负责从 Redis 消费下载结果，下载机只做 HTTP 入队，不参与数据库写入。
    scene_result_consumer = asyncio.create_task(consume_cached_scene_results())
    try:
        yield
    finally:
        scene_result_consumer.cancel()
        await asyncio.gather(scene_result_consumer, return_exceptions=True)
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
