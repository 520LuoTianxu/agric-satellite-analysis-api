"""项目级任务在 claim 模式下走 work_items，而不是直接 send_task 到 API 侧 Redis。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import project_harvest as svc
from app.services import project_tasks as pt


def test_trigger_enabled_by_default(monkeypatch):
    monkeypatch.delenv("PROJECT_HARVEST_ENABLED", raising=False)
    assert svc.trigger_enabled() is True
    monkeypatch.setenv("PROJECT_HARVEST_ENABLED", "")
    assert svc.trigger_enabled() is True
    for off in ("0", "false", "OFF", "no"):
        monkeypatch.setenv("PROJECT_HARVEST_ENABLED", off)
        assert svc.trigger_enabled() is False


class _Res:
    def __init__(self, v):
        self.v = v

    def scalar_one_or_none(self):
        return self.v


class _DB:
    def __init__(self, existing=None):
        self.existing = existing
        self.commits = 0

    async def execute(self, stmt):
        return _Res(self.existing)

    async def commit(self):
        self.commits += 1


def test_enqueue_reuses_pending(monkeypatch):
    existing = SimpleNamespace(id="w1", status="pending")
    called = []

    async def enq(*a, **k):
        called.append(k)

    monkeypatch.setattr(pt.wi, "enqueue_work_item", enq)
    item, reused = asyncio.run(
        pt.enqueue_project_task(_DB(existing), task_name=pt.BUILD_PROJECT_COGS, kwargs={}, dedup_key="cogs:1")
    )
    assert item is existing and reused and not called


def test_enqueue_new_when_none_active(monkeypatch):
    seen = {}

    async def enq(db, **k):
        seen.update(k)
        return SimpleNamespace(id="w2", status="pending", attempts=0)

    monkeypatch.setattr(pt.wi, "enqueue_work_item", enq)
    item, reused = asyncio.run(
        pt.enqueue_project_task(_DB(None), task_name=pt.BUILD_PROJECT_COGS, kwargs={"group_id": "2878"},
                                dedup_key="cogs:2878")
    )
    assert not reused and seen["type"] == "project_task"
    assert seen["idempotency_key"].startswith("project_task:cogs:2878:")
    assert seen["payload"] == {"task_name": pt.BUILD_PROJECT_COGS, "kwargs": {"group_id": "2878"}}


def test_enqueue_rejects_unknown_task():
    with pytest.raises(ValueError):
        asyncio.run(pt.enqueue_project_task(_DB(), task_name="x.y", kwargs={}, dedup_key="k"))


def test_cog_backfill_endpoint_claim_mode(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core import database
    from app.middleware import internal_auth
    from app.routers import internal_agri as R

    monkeypatch.setattr(pt, "use_work_items", lambda: True)
    calls = []

    async def enq(db, **k):
        calls.append(k)
        return SimpleNamespace(id="w9"), False

    monkeypatch.setattr(pt, "enqueue_project_task", enq)

    def boom(*a, **k):
        raise AssertionError("must not send_task in claim mode")

    import app.celery_client as cc

    monkeypatch.setattr(cc, "send_task", boom)
    app = FastAPI()
    app.include_router(R.router, prefix="/v1")
    db = _DB()

    async def get_db():
        yield db

    app.dependency_overrides[database.get_db] = get_db
    for dep in _auth_deps(internal_auth):
        app.dependency_overrides[dep] = lambda: None
    r = TestClient(app).post(
        "/v1/internal/agri/project-cogs/backfill",
        json={"group_id": "2878", "date_from": "2026-08-10", "date_to": "2026-08-31"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["via"] == "work_items" and r.json()["work_item_id"] == "w9"
    assert calls[0]["task_name"] == pt.BUILD_PROJECT_COGS
    assert calls[0]["dedup_key"] == "cogs:2878:2026-08-10:2026-08-31:0"
    assert db.commits == 1


def _auth_deps(mod):
    import typing

    out = []
    meta = getattr(mod.InternalAuth, "__metadata__", ())
    for m in meta:
        if hasattr(m, "dependency"):
            out.append(m.dependency)
    return out
