"""Synchronous database session for Celery worker tasks."""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Celery workers need sync DB; read DATABASE_URL_SYNC or convert asyncpg URL.
_sync_url = os.environ.get("DATABASE_URL_SYNC", "")
if not _sync_url:
    _async_url = os.environ.get("DATABASE_URL", "")
    _sync_url = _async_url.replace("postgresql+asyncpg://", "postgresql://")
_database_schema = os.environ.get("DATABASE_SCHEMA", "agric_satellite")

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
