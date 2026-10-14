"""项目级收获分析（proj_ts_m1_v1）：去重、建运行、派发下载机任务、读结果。

计算在下载机（ingest ``app.tasks.project_harvest``），结果经回执写入
``project_harvest_*`` 表（见 common ``result_apply.apply_project_harvest_result``）。
表尚未创建时，读接口返回 available=false，触发接口返回 503。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date

import structlog
from agric_satellite_analysis_common import project_harvest as ph
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

logger = structlog.get_logger(__name__)

TASK_NAME = "app.tasks.project_harvest.run_project_harvest"
ACTIVE_STATUSES = ("queued", "running")


class TablesMissing(RuntimeError):
    pass


class TriggerDisabled(RuntimeError):
    pass


def trigger_enabled() -> bool:
    """默认开启；仅当显式设置 PROJECT_HARVEST_ENABLED=0/false/no/off 时关闭（应急开关）。"""
    raw = str(os.getenv("PROJECT_HARVEST_ENABLED", "")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def default_season(today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    mmdd = os.getenv("PROJECT_HARVEST_SEASON_FROM", "08-10")
    m, d = (int(x) for x in mmdd.split("-"))
    start = date(today.year, m, d)
    if start > today:
        start = date(today.year - 1, m, d)
    return start, today


def _missing(exc: Exception) -> bool:
    msg = str(exc)
    return "project_" in msg and ("does not exist" in msg or "UndefinedTable" in msg or "UndefinedColumn" in msg)


async def _guard(coro):
    try:
        return await coro
    except ProgrammingError as exc:
        if _missing(exc):
            raise TablesMissing(str(exc)[:200]) from exc
        raise


# ------------------------------------------------------------------ reads
_RUN_COLS = (
    "run_id, group_id, method_version, dedup_hash, season_from, season_to, status, is_current, params, "
    "thresholds, blocks, valid_dates, scenes, parcel_count, merged_count, dedup_area_mu, result_oss_key, "
    "error, trigger, extras, created_at, started_at, finished_at"
)


async def current_run(db: AsyncSession, group_id: str) -> dict | None:
    async def q():
        r = await db.execute(
            text(
                f"SELECT {_RUN_COLS} FROM agric_satellite.project_harvest_runs "
                "WHERE group_id=:g AND method_version=:m AND is_current LIMIT 1"
            ),
            {"g": group_id, "m": ph.METHOD_VERSION},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    return await _guard(q())


async def active_run(db: AsyncSession, group_id: str) -> dict | None:
    async def q():
        r = await db.execute(
            text(
                f"SELECT {_RUN_COLS} FROM agric_satellite.project_harvest_runs "
                "WHERE group_id=:g AND method_version=:m AND status IN ('queued','running') "
                "AND created_at > now() - interval '6 hours' ORDER BY created_at DESC LIMIT 1"
            ),
            {"g": group_id, "m": ph.METHOD_VERSION},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    return await _guard(q())


async def get_run(db: AsyncSession, group_id: str, run_id: str) -> dict | None:
    async def q():
        r = await db.execute(
            text(f"SELECT {_RUN_COLS} FROM agric_satellite.project_harvest_runs WHERE run_id=CAST(:r AS uuid) AND group_id=:g"),
            {"r": run_id, "g": group_id},
        )
        row = r.mappings().first()
        return dict(row) if row else None

    return await _guard(q())


async def summaries(db: AsyncSession, run_id: str) -> list[dict]:
    async def q():
        r = await db.execute(
            text("SELECT * FROM agric_satellite.project_harvest_summaries WHERE run_id=CAST(:r AS uuid)"), {"r": run_id}
        )
        return [dict(x) for x in r.mappings()]

    return await _guard(q())


async def stats(db: AsyncSession, run_id: str, date_: str | None = None) -> list[dict]:
    """date_ 为空/'pick' 时返回全部日期（用于 pick 与回退判断）。"""
    async def q():
        r = await db.execute(
            text("SELECT * FROM agric_satellite.project_harvest_parcel_stats WHERE run_id=CAST(:r AS uuid)"), {"r": run_id}
        )
        return [dict(x) for x in r.mappings()]

    return await _guard(q())


async def rasters(db: AsyncSession, run_id: str, date_: str) -> list[dict]:
    async def q():
        r = await db.execute(
            text(
                "SELECT * FROM agric_satellite.project_harvest_rasters WHERE run_id=CAST(:r AS uuid) AND obs_date=CAST(:d AS date)"
            ),
            {"r": run_id, "d": date_},
        )
        return [dict(x) for x in r.mappings()]

    return await _guard(q())


async def dedup_rows(db: AsyncSession, group_id: str, dedup_hash: str, *, with_geom: bool = True) -> list[dict]:
    cols = "*" if with_geom else (
        "group_id, dedup_hash, land_id, land_status, land_name, priority_rank, priority_rule, kept, merged_reason, "
        "clipped_area_mu, original_area_mu, registered_area_mu, boundary_repaired, covered_by, town_name, village_name"
    )

    async def q():
        r = await db.execute(
            text(f"SELECT {cols} FROM agric_satellite.project_parcel_dedup WHERE group_id=:g AND dedup_hash=:h ORDER BY priority_rank"),
            {"g": group_id, "h": dedup_hash},
        )
        return [dict(x) for x in r.mappings()]

    return await _guard(q())


async def group_lands(db: AsyncSession, group_id: str) -> list[dict]:
    r = await db.execute(
        text(
            "SELECT land_id, land_status, land_name, land_area_mu, town_name, village_name, boundary_geojson "
            "FROM agric_satellite.land_parcels WHERE group_id=:g AND deleted_at IS NULL ORDER BY land_id"
        ),
        {"g": group_id},
    )
    out = []
    for x in r.mappings():
        d = dict(x)
        if d.get("land_area_mu") is not None:
            d["land_area_mu"] = float(d["land_area_mu"])
        out.append(d)
    return out


# ------------------------------------------------------------------ trigger
_DEDUP_COLS = (
    "group_id", "dedup_hash", "land_id", "land_status", "land_name", "priority_rank", "priority_rule", "kept",
    "merged_reason", "clipped_geojson", "clipped_area_mu", "original_area_mu", "registered_area_mu",
    "boundary_repaired", "covered_by", "town_name", "village_name",
)


async def ensure_dedup(db: AsyncSession, group_id: str, lands: list[dict], params: dict) -> dict:
    """同一 dedup_hash 已落库则复用，否则计算（线程池，shapely）并写入。"""
    h = ph.dedup_hash(lands, params)
    existing = await dedup_rows(db, group_id, h, with_geom=False)
    if existing:
        kept = [r for r in existing if r["kept"]]
        return {"hash": h, "kept": len(kept), "merged": len(existing) - len(kept),
                "area_mu": sum(float(r["clipped_area_mu"] or 0) for r in kept)}
    dd = await run_in_threadpool(ph.dedup_parcels, lands, params)
    rows = []
    for r in dd["rows"]:
        rows.append({
            **{k: r.get(k) for k in _DEDUP_COLS if k not in ("group_id", "dedup_hash", "clipped_geojson")},
            "group_id": group_id, "dedup_hash": h,
            "clipped_geojson": json.dumps(r["clipped_geojson"]) if r.get("clipped_geojson") else None,
            "registered_area_mu": r.get("registered_area_mu"),
        })
    cols = ", ".join(_DEDUP_COLS)
    vals = ", ".join(f"CAST(:{c} AS jsonb)" if c == "clipped_geojson" else f":{c}" for c in _DEDUP_COLS)
    await _guard(db.execute(
        text(f"INSERT INTO agric_satellite.project_parcel_dedup ({cols}) VALUES ({vals}) ON CONFLICT DO NOTHING"), rows
    ))
    kept = [r for r in dd["rows"] if r["kept"]]
    return {"hash": h, "kept": len(kept), "merged": len(dd["rows"]) - len(kept),
            "area_mu": sum(float(r["clipped_area_mu"] or 0) for r in kept)}


def dispatch(run_id: str) -> str:
    from agric_satellite_analysis_common.celery_app import SATELLITE_DOWNLOAD_QUEUE

    from app.celery_client import send_task

    res = send_task(TASK_NAME, kwargs={"run_id": run_id}, queue=SATELLITE_DOWNLOAD_QUEUE)
    return str(res.id)


async def create_run(
    db: AsyncSession,
    group_id: str,
    *,
    season_from: date | None,
    season_to: date | None,
    params: dict | None,
    force: bool,
    trigger: str = "manual",
) -> tuple[dict, bool]:
    """返回 (run, reused)。同 group 已有排队/运行中的运行且未 force 时直接复用。"""
    if not trigger_enabled():
        raise TriggerDisabled("PROJECT_HARVEST_ENABLED 已被关闭")
    act = await active_run(db, group_id)
    if act and not force:
        return act, True
    lands = await group_lands(db, group_id)
    if not lands:
        raise LookupError("项目下没有地块")
    p = ph.merge_params(params)
    d0, d1 = default_season()
    d0 = season_from or d0
    d1 = season_to or d1
    dd = await ensure_dedup(db, group_id, lands, p)
    run_id = str(uuid.uuid4())
    await _guard(db.execute(
        text(
            "INSERT INTO agric_satellite.project_harvest_runs (run_id, group_id, method_version, dedup_hash, season_from, "
            "season_to, status, params, parcel_count, merged_count, dedup_area_mu, trigger) VALUES (CAST(:r AS uuid), :g, :m, "
            ":h, :d0, :d1, 'queued', CAST(:p AS jsonb), :n, :mc, :a, :t)"
        ),
        {"r": run_id, "g": group_id, "m": ph.METHOD_VERSION, "h": dd["hash"], "d0": d0, "d1": d1,
         "p": json.dumps(p), "n": dd["kept"], "mc": dd["merged"], "a": round(dd["area_mu"], 2), "t": trigger},
    ))
    from app.services import project_tasks as pt

    if pt.use_work_items():
        # claim 模式：与运行记录同一事务写入 work_items，下载机领取后投递本机队列。
        await pt.enqueue_project_task(
            db, task_name=TASK_NAME, kwargs={"run_id": run_id}, dedup_key=f"harvest:{run_id}", unique=True
        )
        await db.commit()
        run = await get_run(db, group_id, run_id)
        return run or {"run_id": run_id, "group_id": group_id, "status": "queued"}, False
    await db.commit()
    try:
        await run_in_threadpool(dispatch, run_id)
    except Exception as exc:
        await db.execute(
            text("UPDATE agric_satellite.project_harvest_runs SET status='failed', error=:e, finished_at=now() WHERE run_id=CAST(:r AS uuid)"),
            {"r": run_id, "e": f"派发失败：{str(exc)[:500]}"},
        )
        await db.commit()
        raise
    run = await get_run(db, group_id, run_id)
    return run or {"run_id": run_id, "group_id": group_id, "status": "queued"}, False


async def run_inputs(db: AsyncSession, run_id: str) -> dict | None:
    """下载机拉取运行输入。"""
    r = await _guard(db.execute(
        text("SELECT run_id, group_id, dedup_hash, season_from, season_to, params FROM agric_satellite.project_harvest_runs WHERE run_id=CAST(:r AS uuid)"),
        {"r": run_id},
    ))
    run = r.mappings().first()
    if not run:
        return None
    rows = await dedup_rows(db, run["group_id"], run["dedup_hash"])
    lands = await group_lands(db, run["group_id"])
    return {
        "run_id": str(run["run_id"]),
        "group_id": run["group_id"],
        "season_from": run["season_from"].isoformat(),
        "season_to": run["season_to"].isoformat(),
        "params": run["params"] or {},
        "parcels": [
            {k: (float(v) if k.endswith("_mu") and v is not None else v) for k, v in x.items()
             if k in ("land_id", "land_status", "land_name", "clipped_geojson", "clipped_area_mu", "town_name", "village_name")}
            for x in rows if x["kept"]
        ],
        "lands": [{"land_id": x["land_id"], "boundary_geojson": x["boundary_geojson"]} for x in lands],
    }
