"""Synchronous database session for Celery worker tasks."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from openfarm_common.settings import sync_database_url

sync_engine = create_engine(sync_database_url(), echo=False, pool_pre_ping=True)
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
