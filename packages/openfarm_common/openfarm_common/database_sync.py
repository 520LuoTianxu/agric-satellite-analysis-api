"""Synchronous database session for Celery worker tasks."""

from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from openfarm_common.settings import sync_database_url

# Celery prefork: each child process gets its own engine. Keep pools tiny so
# N workers × (pool_size+overflow) stays well under Postgres max_connections.
# Override with SYNC_DB_POOL_SIZE / SYNC_DB_MAX_OVERFLOW if needed.
_pool_size = max(1, int(os.environ.get("SYNC_DB_POOL_SIZE", "2") or 2))
_max_overflow = max(0, int(os.environ.get("SYNC_DB_MAX_OVERFLOW", "2") or 2))
_pool_recycle = max(60, int(os.environ.get("SYNC_DB_POOL_RECYCLE", "300") or 300))
_app_name = (os.environ.get("SYNC_DB_APP_NAME") or "openfarm-sync").strip() or "openfarm-sync"

sync_engine = create_engine(
    sync_database_url(),
    echo=False,
    pool_pre_ping=True,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    pool_timeout=60,
    pool_recycle=_pool_recycle,
    connect_args={"application_name": _app_name},
)
SyncSession = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)


def get_sync_db() -> Session:
    session = SyncSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
