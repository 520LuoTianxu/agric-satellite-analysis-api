"""全国每日遥感发现、结果入库检查及各级不可变快照，只在API机访问数据库。"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.tables import Job, LandParcel
from app.mq_publish import publish_api_task
from app.schemas.agri import OverviewStatsOut
from app.services.satellite_batch import group_satellite_lands, satellite_land_geometry

WINDOW_DAYS = 60
LOOKBACK_DAYS = 7
RUN_TYPE = "overview_daily"
CHINA_TZ = timezone(timedelta(hours=8))


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


def aggregate_snapshots(
    facts: dict[str, dict[str, Any]], template: OverviewStatsOut, day: date
) -> list[OverviewStatsOut]:
    """一次遍历地块分类，按当天区划和相同口径汇总全国到县级，不重复读取像元。"""
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
            node = {"level": level, "code": code, "name": name}
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
            children.append(
                {
                    **group_node(outputs[child_key]),
                    **out.totals.model_dump(),
                    "drought_severe": out.drought.severe,
                    "drought_alert": out.drought.severe
                    + out.drought.moderate
                    + out.drought.mild,
                    "flood": out.flood.flood,
                    "flood_alert": out.flood.flood,
                    "weak_growth": out.weak_growth.parcel_count,
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
    """下载任务完成后核对发布结果已入库，最后原子保存所有区划快照。"""
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
    pending = (
        sum(job.status not in ("completed", "failed", "cancelled") for job in jobs)
        + len(ids)
        - len(jobs)
    )
    failed = sum(job.status in ("failed", "cancelled") for job in jobs)
    expected = set()
    unreported = 0
    for job in jobs:
        progress = job.progress_json or {}
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
    expired = datetime.now(timezone.utc) - run.started_at > timedelta(hours=23)
    progress = {
        **(run.progress_json or {}),
        "pending_jobs": pending,
        "failed_jobs": failed,
        "results_pending": missing,
        "phase": "downloading" if pending else "waiting_results",
    }
    run.progress_json = progress
    if (pending or missing) and not expired:
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
    for out in snapshots:
        out.filters.update(
            data_status="partial" if partial else "complete", run_id=str(run.id)
        )
        await db.execute(
            text(_UPSERT_OVERVIEW_SQL),
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
            },
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
