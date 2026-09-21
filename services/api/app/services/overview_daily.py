"""全国每日遥感发现、结果入库检查及各级不可变快照，只在API机访问数据库。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.scheduled_land_filter import (
    EXCLUDED_SCHEDULE_BASE_IDS,
    MAX_SCHEDULE_LAND_AREA_MU,
)
from agric_satellite_analysis_common.task_priority import BACKGROUND_TASK_PRIORITY
from app.core.config import settings
from app.models.tables import Job, LandParcel
from app.mq_publish import publish_api_task
from app.schemas.agri import OverviewStatsOut
from app.services.satellite_batch import group_satellite_lands, satellite_land_geometry

WINDOW_DAYS = 60
LOOKBACK_DAYS = 7
RUN_TYPE = "overview_daily"
CHINA_TZ = timezone(timedelta(hours=8))
# 每批写入多个区划快照，减少每日终态汇总时的数据库往返次数。
OVERVIEW_UPSERT_BATCH_SIZE = 200
# 子任务进入失败/取消也是终态；兼容旧worker使用的 succeeded，汇总只等待未终态任务。
TERMINAL_SATELLITE_JOB_STATUSES = frozenset(
    {"completed", "succeeded", "failed", "cancelled"}
)
FAILED_SATELLITE_JOB_STATUSES = frozenset({"failed", "cancelled"})


def business_today() -> date:
    """统计日使用北京时间，避免UTC定时任务把翌日快照写到前一天。"""
    return datetime.now(CHINA_TZ).date()


def run_id_for(day: date) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"agric-satellite/overview-daily/{day}")


def download_start(_latest: date | None, day: date) -> date:
    """所有地块只检查包含当天在内的近7个自然日，并按数据库结果去重。"""
    # 使用闭区间 [day-6, day]，避免把“近7天”扩大成8个自然日。
    return day - timedelta(days=LOOKBACK_DAYS - 1)


def region_key(level: str, code: str | None, name: str | None, parent: str = "") -> str:
    """标准行政码统一到6位；缺码区划使用父区划和名称防止重名合并。"""
    from app.routers.agri_overview import _pad_adcode

    if level == "country":
        return ""
    return _pad_adcode(level, code) if code else f"{parent}/{name}"


def _affected_ratio(count: int, total: int) -> float:
    """每日快照和实时接口使用同一比例口径，保证地图切换视图时颜色一致。"""
    if total <= 0 or count <= 0:
        return 0.0
    return round(min(count / total, 1.0), 6)


def aggregate_snapshots(
    facts: dict[str, dict[str, Any]], template: OverviewStatsOut, day: date
) -> list[OverviewStatsOut]:
    """一次遍历地块分类，按当天区划和相同口径汇总全国到县级，不重复读取像元。"""
    from app.routers.agri_overview import _pad_adcode

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    country = {"level": "country", "code": None, "name": "全国"}
    groups[("country", "")] = {
        "node": country,
        "path": [country],
        "lands": [],
        "children": set(),
    }
    for fact in facts.values():
        path, parent_key = [country], ("country", "")
        groups[parent_key]["lands"].append(fact)
        for level in ("province", "city", "county"):
            name = fact.get(f"{level}_name")
            if not name:
                continue
            code = str(fact[f"{level}_code"]) if fact.get(f"{level}_code") else None
            key = (level, region_key(level, code, name, parent_key[1]))
            # 快照中的行政码必须与实时接口和 GeoJSON 使用同一套六位编码，
            # 否则市/县边界只能按名称匹配，重名区划会出现错色或无法着色。
            node = {
                "level": level,
                "code": _pad_adcode(level, code) if code else None,
                "name": name,
            }
            path = [*path, node]
            group = groups.setdefault(
                key, {"node": node, "path": path, "lands": [], "children": set()}
            )
            group["lands"].append(fact)
            groups[parent_key]["children"].add(key)
            parent_key = key

    outputs = {}
    for key, group in groups.items():
        payload = template.model_dump(mode="json")
        payload["region"] = {
            **group["node"],
            "path": group["path"],
            "adcode": "100000"
            if key[0] == "country"
            else (key[1] if group["node"]["code"] else None),
        }
        drought = {k: 0 for k in ("severe", "moderate", "mild", "normal", "unknown")}
        flood = {
            k: 0
            for k in ("flood_severe", "flood_moderate", "flood_mild", "dry", "unknown")
        }
        da, fa = defaultdict(float), defaultdict(float)
        area, weak_count, weak_area = 0.0, 0, 0.0
        freshness = {}
        for sensor in ("s1", "s2"):
            dates = [f[f"{sensor}_date"] for f in group["lands"] if f[f"{sensor}_date"]]
            today_count = dates.count(day.isoformat())
            freshness[sensor] = {
                "today": today_count,
                "carried": len(dates) - today_count,
                "unknown": len(group["lands"]) - len(dates),
                "oldest": min(dates) if dates else None,
                "latest": max(dates) if dates else None,
            }
        for fact in group["lands"]:
            mu = float(fact["area_mu"] or 0)
            area += mu
            drought[fact["drought"]] += 1
            flood[fact["flood"]] += 1
            da[fact["drought"]] += mu
            fa[fact["flood"]] += mu
            if fact["weak"]:
                weak_count += 1
                weak_area += mu
        flood.update(
            flood=flood["flood_severe"] + flood["flood_moderate"],
            wet=flood["flood_mild"],
        )
        fa.update(flood=fa["flood_severe"] + fa["flood_moderate"], wet=fa["flood_mild"])
        payload.update(
            totals={"parcel_count": len(group["lands"]), "area_mu": round(area, 2)},
            drought={**drought, "area_mu": {k: round(da[k], 2) for k in drought}},
            flood={**flood, "area_mu": {k: round(fa[k], 2) for k in flood}},
            weak_growth={"parcel_count": weak_count, "area_mu": round(weak_area, 2)},
            children=[],
        )
        payload["filters"].update(
            snapshot=True,
            as_of_date=day.isoformat(),
            freshness=freshness,
            snapshot_region_key=key[1],
        )
        outputs[key] = OverviewStatsOut.model_validate(payload)

    for key, group in groups.items():
        children = []
        for child_key in group["children"]:
            out = outputs[child_key]
            drought_alert = out.drought.severe + out.drought.moderate + out.drought.mild
            # 洪涝地图展示“关注”总量，包含轻度积水；open water 仍保留在 flood 字段中。
            flood_alert = (
                out.flood.flood_severe
                + out.flood.flood_moderate
                + out.flood.flood_mild
            )
            parcel_count = out.totals.parcel_count
            children.append(
                {
                    **group_node(outputs[child_key]),
                    **out.totals.model_dump(),
                    "drought_severe": out.drought.severe,
                    "drought_alert": drought_alert,
                    "flood": out.flood.flood,
                    "flood_alert": flood_alert,
                    "weak_growth": out.weak_growth.parcel_count,
                    "drought_ratio": _affected_ratio(drought_alert, parcel_count),
                    "flood_ratio": _affected_ratio(flood_alert, parcel_count),
                    "weak_growth_ratio": _affected_ratio(
                        out.weak_growth.parcel_count, parcel_count
                    ),
                }
            )
        outputs[key] = outputs[key].model_copy(
            update={
                "children": OverviewStatsOut.model_validate(
                    {
                        **outputs[key].model_dump(),
                        "children": sorted(
                            children, key=lambda c: (-c["parcel_count"], c["name"])
                        ),
                    }
                ).children
            }
        )
    return list(outputs.values())


def group_node(out: OverviewStatsOut) -> dict[str, Any]:
    return {k: out.region[k] for k in ("level", "code", "name")}


def run_summary(run: Job) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "as_of_date": run.params_json["as_of_date"],
        "status": run.status,
        **(run.progress_json or {}),
        "error": run.error,
    }


async def _redispatch_failed_work_item_jobs(
    db: AsyncSession,
    jobs: list[Job],
) -> tuple[list[str], list[str]]:
    """重派因下载机派发失败而终止的子任务，返回成功和待重试的 Job ID。"""
    redispatched: list[str] = []
    retry_pending: list[str] = []
    now = datetime.now(timezone.utc)
    for job in jobs:
        progress = dict(job.progress_json or {})
        # work_item_failed 是新链路的派发失败标记；stale_recovered 是旧版本
        # 2小时回收遗留标记。两者都表示任务尚未真正执行，应允许重新入队。
        if job.status != "failed" or not (
            progress.get("work_item_failed") or progress.get("stale_recovered")
        ):
            continue
        try:
            await asyncio.to_thread(
                publish_api_task,
                type="satellite_batch",
                land_id=job.land_id,
                task_id=str(job.id),
                extras={"job_id": str(job.id)},
                priority=BACKGROUND_TASK_PRIORITY,
            )
        except Exception as exc:
            # 重新入队本身失败时保留失败 Job，但把它算作 pending，交给下一轮
            # finalize 继续尝试，不能因为一次 API/MQ 短暂错误直接结束父任务。
            progress.update(
                {
                    "redispatch_error": str(exc)[:2000],
                    "redispatch_error_at": now.isoformat(),
                }
            )
            job.progress_json = progress
            retry_pending.append(str(job.id))
            continue

        try:
            requeue_count = int(progress.get("dispatch_requeue_count") or 0) + 1
        except (TypeError, ValueError):
            requeue_count = 1
        progress.update(
            {
                "dispatch_requeue_count": requeue_count,
                "dispatch_requeued_at": now.isoformat(),
                "dispatch_requeued": True,
            }
        )
        # 重派后清除旧失败门闩，否则后续真正执行失败会被误判为派发失败，
        # 反复绕过下载任务自身的补偿上限。旧回收标记也必须清除，否则
        # 下载机收到任务后会把它当作迟到的旧任务直接跳过。
        progress.pop("work_item_failed", None)
        progress.pop("stale_recovered", None)
        job.status = "pending"
        job.error = None
        job.started_at = now
        job.finished_at = None
        job.progress_json = progress
        redispatched.append(str(job.id))
    return redispatched, retry_pending


async def prepare_daily(db: AsyncSession, day: date) -> dict[str, Any]:
    """以统计日和事务锁防止重复建批次，任务仍走既有MQ/HTTP claim派发。"""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(736401, :day)"), {"day": day.toordinal()}
    )
    run = await db.get(Job, run_id_for(day))
    if run is None:
        lands = (
            (
                await db.execute(
                    select(LandParcel)
                    .where(LandParcel.deleted_at.is_(None))
                    .where(
                        or_(
                            LandParcel.base_id.is_(None),
                            LandParcel.base_id.notin_(EXCLUDED_SCHEDULE_BASE_IDS),
                        ),
                        or_(
                            LandParcel.land_area_mu.is_(None),
                            LandParcel.land_area_mu <= MAX_SCHEDULE_LAND_AREA_MU,
                        ),
                    )
                    .order_by(LandParcel.land_id)
                )
            )
            .scalars()
            .all()
        )
        valid, invalid = [], []
        for land in lands:
            try:
                satellite_land_geometry(land)
                valid.append(land)
            except ValueError:
                invalid.append(land.land_id)
        groups = await asyncio.to_thread(group_satellite_lands, valid)
        jobs = []
        for group in groups:
            for sensor in ("S1", "S2"):
                cursor = download_start(None, day)
                while cursor <= day:
                    end = min(
                        cursor
                        + timedelta(
                            days=max(settings.index_backfill_chunk_days, 1) - 1
                        ),
                        day,
                    )
                    job = Job(
                        id=uuid.uuid4(),
                        land_id=group.anchor_land_id,
                        type="satellite_batch",
                        status="pending",
                        # 预先写入父任务 ID，任务树查询无需再解析 overview_run_id。
                        parent_job_id=run_id_for(day),
                        params_json={
                            "land_ids": group.land_ids,
                            "anchor_land_id": group.anchor_land_id,
                            "processing_window_km": 5.0,
                            "oversized": group.oversized,
                            "sensor": sensor,
                            "download_bbox": list(group.download_bbox),
                            "aggregation_bbox": list(group.aggregation_bbox),
                            "date_from": cursor.isoformat(),
                            "date_to": end.isoformat(),
                            "force": False,
                            "overview_run_id": str(run_id_for(day)),
                        },
                    )
                    db.add(job)
                    jobs.append(job)
                    cursor = end + timedelta(days=1)
        run = Job(
            id=run_id_for(day),
            type=RUN_TYPE,
            status="running",
            started_at=datetime.now(timezone.utc),
            params_json={
                "as_of_date": day.isoformat(),
                "job_ids": [str(j.id) for j in jobs],
            },
            progress_json={
                "phase": "dispatching",
                "lands_checked": len(lands),
                "group_count": len(groups),
                "job_count": len(jobs),
                "invalid_land_ids": invalid,
                "dispatched_job_ids": [],
            },
        )
        db.add(run)
        await db.commit()
    if run.status in ("completed", "partial"):
        return run_summary(run)
    # 重试只补派上次未确认入队的任务；稳定task_id使HTTP claim的幂等键保持一致。
    progress = dict(run.progress_json or {})
    dispatched = set(progress.get("dispatched_job_ids", []))
    for job_id in run.params_json["job_ids"]:
        if job_id in dispatched:
            continue
        job = await db.get(Job, uuid.UUID(job_id))
        await asyncio.to_thread(
            publish_api_task,
            type="satellite_batch",
            land_id=job.land_id,
            task_id=job_id,
            extras={"job_id": job_id},
            priority=BACKGROUND_TASK_PRIORITY,
        )
        dispatched.add(job_id)
        progress["dispatched_job_ids"] = sorted(dispatched)
        run.progress_json = dict(progress)
        await db.commit()
    progress["phase"] = "downloading"
    run.progress_json = progress
    await db.commit()
    return run_summary(run)


async def finalize_daily(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    """所有子任务进入终态后核对结果入库，并原子保存允许部分失败的区划快照。"""
    run = (
        await db.execute(
            select(Job).where(Job.id == run_id, Job.type == RUN_TYPE).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "每日遥感批次不存在")
    if run.status in ("completed", "partial"):
        return run_summary(run)
    day = date.fromisoformat(run.params_json["as_of_date"])
    ids = [uuid.UUID(value) for value in run.params_json["job_ids"]]
    jobs = (
        (await db.execute(select(Job).where(Job.id.in_(ids)))).scalars().all()
        if ids
        else []
    )
    redispatched_job_ids, redispatch_pending_ids = (
        await _redispatch_failed_work_item_jobs(db, jobs)
    )
    if redispatched_job_ids or redispatch_pending_ids:
        await db.flush()
    # 失败地块交给后续补偿，不应阻塞本次全国汇总；只有未进入终态的任务才算 pending。
    pending = (
        sum(job.status not in TERMINAL_SATELLITE_JOB_STATUSES for job in jobs)
        + len(ids)
        - len(jobs)
        + len(redispatch_pending_ids)
    )
    failed = sum(job.status in FAILED_SATELLITE_JOB_STATUSES for job in jobs)
    failed_job_ids = [
        str(job.id) for job in jobs if job.status in FAILED_SATELLITE_JOB_STATUSES
    ]
    failed_land_ids: set[str] = set()
    expected = set()
    unreported = 0
    for job in jobs:
        progress = job.progress_json or {}
        if job.status in FAILED_SATELLITE_JOB_STATUSES:
            failed_land_ids.update(
                str(value) for value in progress.get("failed_land_ids", [])
            )
            if not progress.get("failed_land_ids"):
                # 旧worker没有失败明细时，退化为该批次全部地块作为补偿候选。
                failed_land_ids.update(
                    str(value) for value in (job.params_json or {}).get("land_ids", [])
                )
        products = progress.get("published_products", [])
        unreported += max(0, progress.get("products_published", 0) - len(products))
        expected.update(
            (value["land_id"], value["date"], job.params_json["sensor"])
            for value in products
        )
    # 全国结果一次核对，避免逐任务查库；S1/S2分开匹配，旧worker缺少明细时不能误报入库完成。
    missing = unreported
    if expected:
        missing += (
            await db.execute(
                text("""
            SELECT count(*) FROM jsonb_to_recordset(CAST(:expected AS jsonb))
              AS x(land_id text, date date, sensor text)
            WHERE NOT EXISTS (
                SELECT 1 FROM agric_satellite.parcel_scene_products p
                WHERE p.land_id = x.land_id AND p.date = x.date AND p.sensor = x.sensor
            )
        """),
                {
                    "expected": json.dumps(
                        [
                            {"land_id": land, "date": day, "sensor": sensor}
                            for land, day, sensor in sorted(expected)
                        ]
                    )
                },
            )
        ).scalar_one()
    # 结果已完成但入库回执缺失时仍保留原有23小时屏障；它与下载子任务的
    # 2小时无终态回收不同，不会把尚未被下载机领取的任务误判为失败。
    expired = datetime.now(timezone.utc) - run.started_at > timedelta(hours=23)
    progress = {
        **(run.progress_json or {}),
        "pending_jobs": pending,
        "failed_jobs": failed,
        "failed_job_ids": failed_job_ids,
        "failed_land_ids": sorted(failed_land_ids),
        "failed_land_count": len(failed_land_ids),
        "results_pending": missing,
        "redispatched_job_ids": redispatched_job_ids,
        "redispatch_pending_ids": redispatch_pending_ids,
        "phase": "downloading" if pending else "waiting_results",
    }
    run.progress_json = progress
    # API 不按任务年龄猜测下载机是否丢失；只有下载/入库明确进入终态后才生成快照。
    # 未领取的任务会继续保持 pending，真正的执行失败由下载机重试上限收敛为 failed。
    if pending or (missing and not expired):
        await db.commit()
        return run_summary(run)
    partial = bool(pending or missing or failed or progress.get("invalid_land_ids"))
    from app.routers.agri_overview import (
        _compute_live_stats,
        ensure_overview_cache_table,
    )
    from app.routers.internal_schedule import _UPSERT_OVERVIEW_SQL

    facts: dict[str, dict[str, Any]] = {}
    template = await _compute_live_stats(
        db,
        level="country",
        code=None,
        name=None,
        from_d=day - timedelta(days=WINDOW_DAYS),
        to_d=day,
        crop=None,
        allow_pixels=False,
        parcel_facts=facts,
    )
    await ensure_overview_cache_table(db)
    snapshots = aggregate_snapshots(facts, template, day)
    upsert_rows: list[dict[str, Any]] = []
    for out in snapshots:
        out.filters.update(
            data_status="partial" if partial else "complete", run_id=str(run.id)
        )
        upsert_rows.append(
            {
                "as_of": day,
                "level": out.region["level"],
                "region_code": out.filters["snapshot_region_key"],
                "region_name": out.region["name"],
                "parent_code": out.region["path"][-2]["code"]
                if len(out.region["path"]) > 1
                else None,
                "metric": json.dumps(out.model_dump(mode="json"), ensure_ascii=False),
                "window_from": day - timedelta(days=WINDOW_DAYS),
                "window_to": day,
                "crop": "",
            }
        )
    upsert_sql = text(_UPSERT_OVERVIEW_SQL)
    for offset in range(0, len(upsert_rows), OVERVIEW_UPSERT_BATCH_SIZE):
        # 先完成全部快照对象构造，再在同一事务内分批写入，避免留下半批结果。
        await db.execute(
            upsert_sql,
            upsert_rows[offset : offset + OVERVIEW_UPSERT_BATCH_SIZE],
        )
    run.status = "partial" if partial else "completed"
    run.finished_at = datetime.now(timezone.utc)
    run.error = (
        "部分下载失败、边界无效或结果入库超时，请检查批次任务" if partial else None
    )
    run.progress_json = {**progress, "phase": "finished", "regions": len(snapshots)}
    await db.commit()
    return run_summary(run)


async def read_daily_snapshot(
    db: AsyncSession,
    *,
    level: str,
    code: str | None,
    name: str | None,
    as_of: date | None,
) -> OverviewStatsOut | None:
    """历史只读取保存的快照；当天未产出时允许展示最近快照并明确其统计日。"""
    from app.routers.agri_overview import (
        _pad_adcode,
        _stats_from_cache_json,
        ensure_overview_cache_table,
    )

    if level != "country" and not code and not name:
        raise HTTPException(400, "请选择行政区")
    await ensure_overview_cache_table(db)
    row = (
        await db.execute(
            text("""
        SELECT metric_json, updated_at
        FROM agric_satellite.overview_stats_daily
        WHERE level = :level AND crop = ''
          AND metric_json->'filters'->>'snapshot' = 'true'
          AND window_to - window_from = :window
          AND ((:exact AND as_of_date = :day) OR (NOT :exact AND as_of_date <= :day))
          AND (:country OR (:has_code AND region_code = :code) OR (NOT :has_code AND region_name = :name))
        ORDER BY as_of_date DESC, updated_at DESC LIMIT 1
    """),
            {
                "level": level,
                "window": WINDOW_DAYS,
                "day": as_of or business_today(),
                "exact": as_of is not None,
                "country": level == "country",
                "has_code": bool(code),
                "code": _pad_adcode(level, code) or "",
                "name": name or "",
            },
        )
    ).first()
    if not row:
        return None
    payload = (
        json.loads(row.metric_json)
        if isinstance(row.metric_json, str)
        else row.metric_json
    )
    out = _stats_from_cache_json(payload)
    out.filters.update(cache_hit=True, cache_updated_at=row.updated_at.isoformat())
    return out


async def read_daily_history(
    db: AsyncSession,
    *,
    level: str,
    code: str | None,
    name: str | None,
    from_d: date,
    to_d: date,
) -> list[dict[str, Any]]:
    """趋势数据保留缺失日期，不插值为零，也不使用后来补入的影像重算历史。"""
    from app.routers.agri_overview import _pad_adcode, ensure_overview_cache_table

    if level != "country" and not code and not name:
        raise HTTPException(400, "请选择行政区")
    await ensure_overview_cache_table(db)
    rows = (
        await db.execute(
            text("""
        SELECT as_of_date, metric_json FROM agric_satellite.overview_stats_daily
        WHERE level = :level AND crop = '' AND as_of_date BETWEEN :start AND :end
          AND metric_json->'filters'->>'snapshot' = 'true' AND window_to - window_from = :window
          AND (:country OR (:has_code AND region_code = :code) OR (NOT :has_code AND region_name = :name))
        ORDER BY as_of_date
    """),
            {
                "level": level,
                "window": WINDOW_DAYS,
                "start": from_d,
                "end": to_d,
                "country": level == "country",
                "has_code": bool(code),
                "code": _pad_adcode(level, code) or "",
                "name": name or "",
            },
        )
    ).all()
    items = []
    for row in rows:
        payload = (
            json.loads(row.metric_json)
            if isinstance(row.metric_json, str)
            else row.metric_json
        )
        drought, flood = payload["drought"], payload["flood"]
        items.append(
            {
                "as_of_date": row.as_of_date.isoformat(),
                "parcel_count": payload["totals"]["parcel_count"],
                "drought_alert": sum(
                    drought[k] for k in ("severe", "moderate", "mild")
                ),
                "flood_alert": flood["flood"],
                "drought_unknown": drought["unknown"],
                "flood_unknown": flood["unknown"],
                "data_status": payload["filters"].get("data_status", "complete"),
            }
        )
    return items
