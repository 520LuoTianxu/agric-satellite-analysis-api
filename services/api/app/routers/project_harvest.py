"""项目级收获分析接口 /v1/projects/{group_id}/harvest-analysis（proj_ts_m1_v1，与逐地块 v4 并存）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.auth import OrgContext, require_roles
from app.middleware.internal_auth import InternalAuth
from app.services import project_harvest as svc
from app.services import project_harvest_export as hx
from app.services import project_harvest_views as v

router = APIRouter(tags=["projects"])
_reader = require_roles("owner", "admin", "member", "viewer")
_admin = require_roles("owner", "admin")
GroupId = Annotated[str, Path(min_length=1, max_length=64)]
BASE = "/projects/{group_id}/harvest-analysis"


def _not_configured() -> JSONResponse:
    return JSONResponse({"available": False, "reason": "tables_missing"}, status_code=200)


class RunCreate(BaseModel):
    season_from: date | None = None
    season_to: date | None = None
    force: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


@router.post(BASE + "/runs", status_code=202)
async def create_run(body: RunCreate, group_id: GroupId, ctx: Annotated[OrgContext, Depends(_admin)],
                     db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        run, reused = await svc.create_run(db, group_id, season_from=body.season_from, season_to=body.season_to,
                                           params=body.params, force=body.force)
    except svc.TriggerDisabled as exc:
        raise HTTPException(503, str(exc)) from exc
    except svc.TablesMissing as exc:
        raise HTTPException(503, "项目收获分析表未创建") from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {**(v.run_view(run) or {}), "reused": reused}


@router.get(BASE + "/runs/{run_id}")
async def get_run(group_id: GroupId, run_id: str, ctx: Annotated[OrgContext, Depends(_reader)],
                  db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        run = await svc.get_run(db, group_id, run_id)
    except svc.TablesMissing:
        return _not_configured()
    if not run:
        raise HTTPException(404, "运行不存在")
    return v.run_view(run)


@router.get(BASE)
async def overview(group_id: GroupId, ctx: Annotated[OrgContext, Depends(_reader)],
                   db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        run = await svc.current_run(db, group_id)
        act = await svc.active_run(db, group_id)
    except svc.TablesMissing:
        return _not_configured()
    out = v.overview_view(run, act)
    out["can_trigger"] = svc.trigger_enabled()
    return out


async def _current(db, group_id) -> dict:
    run = await svc.current_run(db, group_id)
    if not run:
        raise HTTPException(404, "该项目还没有收获分析结果")
    return run


@router.get(BASE + "/summary")
async def summary(group_id: GroupId, ctx: Annotated[OrgContext, Depends(_reader)],
                  db: Annotated[AsyncSession, Depends(get_db)], date: str | None = Query(None, max_length=10)):
    try:
        run = await _current(db, group_id)
        rows = await svc.summaries(db, str(run["run_id"]))
    except svc.TablesMissing:
        return _not_configured()
    return v.summary_view(rows, date, run["season_to"].isoformat(), (run.get("extras") or {}).get("pick_dates"))


async def _parcel_rows(db, group_id, date_):
    run = await _current(db, group_id)
    st = await svc.stats(db, str(run["run_id"]))
    dd = {str(r["land_id"]): r for r in await svc.dedup_rows(db, group_id, run["dedup_hash"], with_geom=False)}
    return run, v.parcel_rows(st, dd, date_)


@router.get(BASE + "/parcels")
async def parcels(group_id: GroupId, ctx: Annotated[OrgContext, Depends(_reader)],
                  db: Annotated[AsyncSession, Depends(get_db)], date: str | None = Query(None, max_length=10),
                  q: str | None = Query(None, max_length=64), status: str | None = Query(None, max_length=8),
                  sort: str = Query("harvested_pct"), order: Literal["asc", "desc"] = "desc",
                  offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=1000)):
    try:
        _run, rows = await _parcel_rows(db, group_id, date)
    except svc.TablesMissing:
        return _not_configured()
    return v.filter_sort_page(rows, q=q, status=status, sort=sort, order=order, offset=offset, limit=limit)


@router.get(BASE + "/parcels.geojson")
async def parcels_geojson(group_id: GroupId, ctx: Annotated[OrgContext, Depends(_reader)],
                          db: Annotated[AsyncSession, Depends(get_db)], date: str | None = Query(None, max_length=10)):
    """去重后几何（WGS84，不做 GCJ02 转换）+ 当日分级属性。"""
    try:
        run, rows = await _parcel_rows(db, group_id, date)
        dd = await svc.dedup_rows(db, group_id, run["dedup_hash"])
    except svc.TablesMissing:
        return _not_configured()
    return v.geojson_view(dd, {r["land_id"]: r for r in rows})


@router.get(BASE + "/layers")
async def layers(group_id: GroupId, ctx: Annotated[OrgContext, Depends(_reader)],
                 db: Annotated[AsyncSession, Depends(get_db)], date: str = Query(..., max_length=10),
                 kind: Literal["tier", "rgb"] = "tier"):
    try:
        run = await _current(db, group_id)
        rs = await svc.rasters(db, str(run["run_id"]), date)
    except svc.TablesMissing:
        return _not_configured()
    from app.core.storage import get_storage

    st = get_storage()

    def url_for(key: str) -> str | None:
        try:
            return st.presigned_get(key, expires=timedelta(hours=1))
        except Exception:
            return None

    return v.layers_view(rs, run.get("blocks") or [], date, kind, url_for)


@router.get(BASE + "/dedup")
async def dedup(group_id: GroupId, ctx: Annotated[OrgContext, Depends(_reader)],
                db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        run = await _current(db, group_id)
        rows = await svc.dedup_rows(db, group_id, run["dedup_hash"], with_geom=False)
    except svc.TablesMissing:
        return _not_configured()
    return v.dedup_view(rows)


# ---------------- 导出 ----------------
def _redis(request: Request):
    r = getattr(request.app.state, "redis_client", None)
    if r is None:
        raise HTTPException(503, "Redis 未就绪")
    return r


@router.post(BASE + "/export-jobs", status_code=202)
async def create_export(group_id: GroupId, request: Request, ctx: Annotated[OrgContext, Depends(_reader)],
                        db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        run = await _current(db, group_id)
    except svc.TablesMissing as exc:
        raise HTTPException(503, "项目收获分析表未创建") from exc
    from app.core.database import async_session

    async def loader():
        async with async_session() as s:
            st = await svc.stats(s, str(run["run_id"]))
            sm = await svc.summaries(s, str(run["run_id"]))
            dd = await svc.dedup_rows(s, group_id, run["dedup_hash"], with_geom=False)
        return run, st, sm, dd

    job = await hx.create_job(_redis(request), group_id, loader)
    return hx.public_view(job)


@router.get(BASE + "/export-jobs/{job_id}")
async def get_export(group_id: GroupId, job_id: str, request: Request, ctx: Annotated[OrgContext, Depends(_reader)]):
    job = await hx.get_job(_redis(request), job_id)
    if job is None or job.get("group_id") != group_id:
        raise HTTPException(404, "导出任务不存在或已过期（24 小时）")
    return hx.public_view(job)


@router.get(BASE + "/export-jobs/{job_id}/download")
async def download_export(group_id: GroupId, job_id: str, request: Request, ctx: Annotated[OrgContext, Depends(_reader)]):
    job = await hx.get_job(_redis(request), job_id)
    if job is None or job.get("group_id") != group_id:
        raise HTTPException(404, "导出任务不存在或已过期（24 小时）")
    if job["status"] != "done":
        raise HTTPException(409, f"导出任务尚未完成（{job['status']}）")
    from urllib.parse import quote

    from app.core.storage import get_storage

    name = job["file_name"]
    return StreamingResponse(get_storage().iter_bytes(job["object_key"]), media_type=hx.XLSX_MIME,
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# ---------------- 内部：下载机拉取输入 ----------------
internal_router = APIRouter(prefix="/internal/project-harvest", tags=["internal"])


@internal_router.get("/runs/{run_id}/inputs")
async def run_inputs(run_id: str, _: InternalAuth, db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        data = await svc.run_inputs(db, run_id)
    except svc.TablesMissing as exc:
        raise HTTPException(503, "项目收获分析表未创建") from exc
    if data is None:
        raise HTTPException(404, "运行不存在")
    return data
