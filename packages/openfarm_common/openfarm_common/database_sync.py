"""Synchronous database session for Celery worker tasks."""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openfarm_common.settings import sync_database_url

# Prefork ingest workers run one Celery task each; that task may open one
# session per scene thread (INGEST_SCENE_MAX_WORKERS). Size the pool so those
# threads do not stall on QueuePool timeout. Defaults fit 16 scene workers
# plus the parent task session (see process_scenes_parallel).
_pool_size = max(5, int(os.environ.get("SYNC_DB_POOL_SIZE", "8") or 8))
_max_overflow = max(10, int(os.environ.get("SYNC_DB_MAX_OVERFLOW", "16") or 16))

sync_engine = create_engine(
    sync_database_url(),
    echo=False,
    pool_pre_ping=True,
    pool_size=_pool_size,
    max_overflow=_max_overflow,
    pool_timeout=120,
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
