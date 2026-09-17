"""Synchronous database session for Celery worker tasks."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from agric_satellite_analysis_common.settings import resolve_sync_database_url
from app.core.config import settings

# Prefer Settings/.env (same source as the async engine). Raw os.environ is not
# enough in ABflow images that copy .env.test but never export it into the process.
_sync_url = resolve_sync_database_url(settings.database_url_sync, settings.database_url)
_database_schema = settings.database_schema or "agric_satellite"

sync_engine = create_engine(
    _sync_url,
    echo=False,
    pool_pre_ping=True,
    # Celery 任务包含未限定 SQL；每个连接都显式使用统一业务 schema。
    connect_args={"options": f"-csearch_path={_database_schema}"},
)
SyncSession = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)


def get_sync_db() -> Session:
    """Yields a sync DB session (for Celery tasks)."""
    session = SyncSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
