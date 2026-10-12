"""Database session management."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

def _server_settings() -> dict[str, str]:
    # 原生 SQL 仍有未限定表名；固定搜索路径保证其与 ORM 指向同一业务 schema。
    values = {"search_path": settings.database_schema}
    for key, ms in (
        ("lock_timeout", settings.db_lock_timeout_ms),
        ("idle_in_transaction_session_timeout", settings.db_idle_in_transaction_timeout_ms),
        ("statement_timeout", settings.db_statement_timeout_ms),
    ):
        if int(ms or 0) > 0:
            values[key] = str(int(ms))
    return values


engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=max(1, int(settings.db_pool_size)),
    max_overflow=max(0, int(settings.db_max_overflow)),
    pool_timeout=max(1.0, float(settings.db_pool_timeout_seconds)),
    connect_args={"server_settings": _server_settings()},
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    """FastAPI dependency - yields an async DB session."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
