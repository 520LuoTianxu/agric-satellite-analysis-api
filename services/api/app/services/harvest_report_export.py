"""收获报表 Excel 异步导出任务。

- 状态存 Redis（JSON，TTL 24h），多副本共享；文件存对象存储（OSS），任一副本都能下载。
- 读库在事件循环里走 async IO；Excel 生成（xlsxwriter 流式）是纯 CPU，放到独立子进程（spawn，单 worker，
  每次任务后回收），不占用 API 进程的 GIL，避免拖慢健康检查。
- 同参数且仍在 pending/running（或 10 分钟内刚完成）的任务直接复用。
- 运行中定期写心跳；实例被杀导致心跳超时的任务在查询时视为 failed。
- 文件按日期前缀存放，任务完成时顺带清理 24h 以前的旧文件（另建议配 OSS 生命周期规则兜底）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import pickle
import tempfile
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import date, datetime, timedelta, timezone
from typing import Any

import structlog

from app.services import harvest_report as hr

logger = structlog.get_logger(__name__)

JOB_TTL_SEC = int(os.getenv("HARVEST_EXPORT_TTL_SEC", str(24 * 3600)))
REUSE_DONE_SEC = int(os.getenv("HARVEST_EXPORT_REUSE_DONE_SEC", "600"))
HEARTBEAT_SEC = 15
STALE_SEC = int(os.getenv("HARVEST_EXPORT_STALE_SEC", "180"))
MAX_CONCURRENCY = max(1, int(os.getenv("HARVEST_EXPORT_CONCURRENCY", "1")))
KEY_PREFIX = "exports/harvest-report/"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_JOB_KEY = "harvest-export:job:{}"
_DEDUP_KEY = "harvest-export:dedup:{}"
ACTIVE = ("pending", "running")

_tasks: set[asyncio.Task] = set()
_sem: asyncio.Semaphore | None = None
_executor: ProcessPoolExecutor | None = None


def _now() -> float:
    return time.time()


def params_of(f: hr.ReportFilters, sort: str, order: str, carry_forward: bool) -> dict:
    return {
        "from": f.date_from.isoformat(),
        "to": f.date_to.isoformat(),
        "group_id": f.group_id,
        "crop": f.crop,
        "status": list(f.buckets),
        "min_pct": f.min_pct,
        "q": f.keyword,
        "sort": sort,
        "order": order,
        "carry_forward": carry_forward,
    }


def filters_from(p: dict) -> hr.ReportFilters:
    return hr.ReportFilters(
        date_from=date.fromisoformat(p["from"]),
        date_to=date.fromisoformat(p["to"]),
        group_id=p.get("group_id"),
        crop=p.get("crop"),
        buckets=list(p.get("status") or []),
        min_pct=p.get("min_pct"),
        keyword=p.get("q"),
    )


def params_hash(p: dict) -> str:
    return hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:20]


def file_name(p: dict) -> str:
    return f"harvest-report_{p['from'].replace('-', '')}-{p['to'].replace('-', '')}.xlsx"


async def get_job(redis, job_id: str) -> dict | None:
    raw = await redis.get(_JOB_KEY.format(job_id))
    if not raw:
        return None
    job = json.loads(raw)
    if job["status"] in ACTIVE and _now() - job.get("heartbeat_at", 0) > STALE_SEC:
        job["status"] = "failed"
        job["error"] = "导出任务所在实例已中断，请重新导出"
    return job


async def _save(redis, job: dict) -> None:
    await redis.set(_JOB_KEY.format(job["job_id"]), json.dumps(job), ex=JOB_TTL_SEC)


async def _update(redis, job: dict, **kw: Any) -> None:
    job.update(kw)
    job["heartbeat_at"] = _now()
    await _save(redis, job)


async def create_job(redis, params: dict, created_by: str | None, runner=None) -> tuple[dict, bool]:
    """返回 (job, reused)。"""
    h = params_hash(params)
    old_id = await redis.get(_DEDUP_KEY.format(h))
    if old_id:
        old = await get_job(redis, old_id)
        if old and (
            old["status"] in ACTIVE
            or (old["status"] == "done" and _now() - old.get("finished_at", 0) < REUSE_DONE_SEC)
        ):
            return old, True
    now = _now()
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "status": "pending",
        "progress": 0,
        "stage": "排队中",
        "error": None,
        "params": params,
        "file_name": file_name(params),
        "object_key": None,
        "size_bytes": None,
        "row_count": None,
        "created_by": created_by,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "heartbeat_at": now,
    }
    await _save(redis, job)
    await redis.set(_DEDUP_KEY.format(h), job_id, ex=JOB_TTL_SEC)
    task = asyncio.create_task((runner or run_job)(redis, job), name=f"harvest-export-{job_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job, False


def _get_executor() -> ProcessPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ProcessPoolExecutor(
            max_workers=MAX_CONCURRENCY,
            mp_context=multiprocessing.get_context("spawn"),
            max_tasks_per_child=1,  # 每次导出后回收子进程，大报表的内存立即归还
        )
    return _executor


def shutdown_executor() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


_PICKLE_CHUNK = 2000


def build_to_file(rows, daily, kpi, filters, carry_forward, path) -> int:
    """生成工作簿写到 path，返回字节数。"""
    from app.services.harvest_report_xlsx import build_workbook_to_path

    # 流式直接写目标文件，不在内存里拼整份字节
    return build_workbook_to_path(path, rows, daily, kpi, filters, None, carry_forward)


def _read_list(fh) -> list:
    out: list = []
    for _ in range(pickle.load(fh)):
        out.extend(pickle.load(fh))
    return out


def build_from_input(in_path: str, path: str) -> int:
    """子进程入口：从分块 pickle 输入文件读参数再生成。"""
    with open(in_path, "rb") as fh:
        kpi, filters, carry_forward = pickle.load(fh)
        rows = _read_list(fh)
        daily = _read_list(fh)
    return build_to_file(rows, daily, kpi, filters, carry_forward, path)


async def _dump_list(fh, items: list) -> None:
    chunks = [items[i : i + _PICKLE_CHUNK] for i in range(0, len(items), _PICKLE_CHUNK)]
    pickle.dump(len(chunks), fh)
    for ch in chunks:
        pickle.dump(ch, fh, protocol=pickle.HIGHEST_PROTOCOL)
        await asyncio.sleep(0)  # 每块几毫秒，期间让出事件循环


async def _build(rows, daily, kpi, filters, carry_forward, path) -> int:
    """参数分块序列化到临时文件（一次性 pickle 20 万行会持有 GIL 数秒），子进程读文件生成。"""
    in_path = path + ".in"
    try:
        with open(in_path, "wb") as fh:
            pickle.dump((kpi, filters, carry_forward), fh)
            await _dump_list(fh, rows)
            await _dump_list(fh, daily)
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(_get_executor(), build_from_input, in_path, path)
        except BrokenProcessPool:
            shutdown_executor()  # 子进程被杀（如 OOM）后重建进程池，后续任务不受影响
            raise RuntimeError("生成 Excel 的子进程异常退出（可能内存不足），请缩小筛选范围后重试")
    finally:
        if os.path.exists(in_path):
            os.unlink(in_path)


async def _load(filters: hr.ReportFilters):
    from app.core.database import async_session

    async with async_session() as db:
        raw = await hr.load_report_raw(db, filters)
    return await asyncio.to_thread(hr.build_report, *raw)


def _storage():
    from app.core.storage import get_storage

    return get_storage()


def delete_object(storage, key: str) -> None:
    fn = getattr(storage, "delete", None) or getattr(storage, "delete_object", None)
    if callable(fn):
        fn(key)
        return
    bucket = getattr(storage, "_bucket", None)
    if bucket is not None:
        bucket.delete_object(key)


def cleanup_old_files(storage, now: datetime | None = None) -> int:
    """删除 KEY_PREFIX 下早于昨天日期目录的文件（任务 TTL 24h，文件已不可下载）。"""
    today = (now or datetime.now(timezone(timedelta(hours=8)))).date()
    cutoff = (today - timedelta(days=1)).strftime("%Y%m%d")
    n = 0
    for key in storage.list_keys(KEY_PREFIX, ".xlsx", 2000):
        day = key[len(KEY_PREFIX) :].split("/", 1)[0]
        if day.isdigit() and day < cutoff:
            try:
                delete_object(storage, key)
                n += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("harvest_export_cleanup_failed", key=key, err=str(exc))
    return n


def _prepare(parcels, daily, filters, p):
    rows = hr.sort_parcels(hr.apply_filters(parcels, filters), p["sort"], p["order"])
    keep = {r["land_id"] for r in rows}
    return rows, [r for r in daily if r["land_id"] in keep], hr.summarize(rows)


async def _heartbeat(redis, job: dict) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SEC)
        try:
            await _update(redis, job)
        except Exception:  # noqa: BLE001
            pass


async def run_job(redis, job: dict) -> None:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(MAX_CONCURRENCY)
    hb = asyncio.create_task(_heartbeat(redis, job))
    path = None
    t0 = time.perf_counter()
    try:
        async with _sem:
            p = job["params"]
            filters = filters_from(p)
            await _update(redis, job, status="running", progress=5, stage="读取数据", started_at=_now())
            parcels, daily = await _load(filters)
            await _update(redis, job, progress=40, stage="筛选排序")
            rows, daily, kpi = await asyncio.to_thread(_prepare, parcels, daily, filters, p)
            del parcels
            await _update(redis, job, progress=50, stage="生成Excel", row_count=len(rows))
            fd, path = tempfile.mkstemp(prefix="harvest-export-", suffix=".xlsx")
            os.close(fd)
            size = await _build(rows, daily, kpi, filters, p["carry_forward"], path)
            del rows, daily
            await _update(redis, job, progress=90, stage="上传文件")
            day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
            key = f"{KEY_PREFIX}{day}/{job['job_id']}.xlsx"
            storage = _storage()
            await asyncio.to_thread(storage.upload_file, key, path, XLSX_MIME)
            await _update(
                redis, job, status="done", progress=100, stage="完成",
                object_key=key, size_bytes=size, finished_at=_now(),
            )
            logger.info("harvest_export_done", job_id=job["job_id"], rows=job["row_count"],
                        bytes=size, sec=round(time.perf_counter() - t0, 1))
            try:
                await asyncio.to_thread(cleanup_old_files, storage)
            except Exception as exc:  # noqa: BLE001
                logger.warning("harvest_export_cleanup_error", err=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("harvest_export_failed", job_id=job["job_id"])
        try:
            await _update(redis, job, status="failed", stage="失败",
                          error=f"{type(exc).__name__}: {exc}"[:500], finished_at=_now())
        except Exception:  # noqa: BLE001
            pass
    finally:
        hb.cancel()
        if path and os.path.exists(path):
            os.unlink(path)


def public_view(job: dict) -> dict:
    def iso(ts):
        return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).isoformat() if ts else None

    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "progress": job["progress"],
        "stage": job["stage"],
        "error": job["error"],
        "params": job["params"],
        "file_name": job["file_name"],
        "size_bytes": job["size_bytes"],
        "row_count": job["row_count"],
        "created_at": iso(job["created_at"]),
        "started_at": iso(job["started_at"]),
        "finished_at": iso(job["finished_at"]),
    }
