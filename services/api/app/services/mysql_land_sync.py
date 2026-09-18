"""API 机侧 MySQL 农业地块同步。

MySQL 是外部业务源，PostgreSQL ``agric_satellite`` 是遥感分析主库。
本模块只在 API 机运行：下载机不读取 MySQL，也不负责地块主数据写入。
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from shapely.geometry import MultiPolygon, Polygon, mapping
from shapely.ops import transform as shapely_transform
from pyproj import Transformer
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.config import settings
from app.core.logging import logger
from app.models.tables import AuditEvent, Farm, Job, LandParcel

SOURCE_SYSTEM = "agriculture_mysql"
SOURCE_FILE = "mysql:agriculture_land"
SYNC_LOCK_KEY = "agric-satellite:mysql-land-sync"
JOB_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "agric-satellite/mysql-land-sync")

# 源查询只返回有效地块；同步完成后才允许按 source_properties 软删缺失源记录。
SOURCE_SQL = text(
    """
    SELECT
        CAST(lg.group_id AS CHAR) AS farms_id,
        lg.group_name AS farms_name,
        'CN' AS country,
        NULL AS region,
        'Asia/Shanghai' AS timezone,
        CAST(al.base_id AS CHAR) AS base_id,
        al.province_code,
        al.province_name,
        al.city_code,
        al.city_name,
        al.county_code,
        al.county_name,
        al.town_code,
        al.town_name,
        al.village_code,
        al.village_name,
        lg.org_code,
        lg.org_name,
        CAST(lg.group_id AS CHAR) AS group_id,
        lg.group_name,
        CAST(al.land_id AS CHAR) AS land_id,
        al.land_name,
        al.land_area,
        lg.planting_type,
        lg.business_category,
        al.status AS source_status,
        al.wgs_land_path
    FROM agriculture_land AS al
    INNER JOIN agriculture_land_group AS lg
        ON al.group_id = lg.group_id
    WHERE al.del_flag = 0
      AND lg.del_flag = 0
      AND lg.base_id <> 46
    ORDER BY lg.group_id, al.land_id
    """
)


@dataclass(frozen=True)
class SourceParcel:
    """Normalized row used by both the master-data upsert and RS dispatch."""

    land_id: str
    group_id: str
    group_name: str
    country: str
    region: str | None
    timezone: str
    base_id: str | None
    province_code: str | None
    province_name: str | None
    city_code: str | None
    city_name: str | None
    county_code: str | None
    county_name: str | None
    town_code: str | None
    town_name: str | None
    village_code: str | None
    village_name: str | None
    org_code: str | None
    org_name: str | None
    land_name: str
    land_area_mu: float | None
    source_status: str | None
    planting_type: Any
    business_category: Any
    boundary_geojson: dict[str, Any]
    area_ha: float
    bounds: tuple[float, float, float, float]
    source_properties: dict[str, Any]
    sync_hash: str
    rs_hash: str


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _required_id(value: Any, field: str) -> str:
    value = _string_or_none(value)
    if not value:
        raise ValueError(f"{field} is required")
    if len(value) > 64:
        raise ValueError(f"{field} exceeds 64 characters")
    return value


def _json_safe(value: Any) -> Any:
    """把 MySQL Decimal 等值转为 JSONB 可保存的普通类型。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def parse_wgs_land_path(value: str) -> MultiPolygon:
    """解析 ``lon,lat|lon,lat|...``，并标准化为有效 WGS84 MultiPolygon。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("wgs_land_path is empty")

    points: list[tuple[float, float]] = []
    for index, item in enumerate(value.split("|")):
        parts = [part.strip() for part in item.split(",")]
        if len(parts) != 2:
            raise ValueError(f"invalid coordinate at index {index}")
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError as exc:
            raise ValueError(f"invalid coordinate at index {index}") from exc
        if (
            not math.isfinite(lon)
            or not math.isfinite(lat)
            or not -180 <= lon <= 180
            or not -90 <= lat <= 90
        ):
            raise ValueError(f"coordinate out of WGS84 range at index {index}")
        points.append((lon, lat))

    if len(points) < 3:
        raise ValueError("wgs_land_path needs at least 3 points")
    if points[0] != points[-1]:
        points.append(points[0])

    polygon = Polygon(points)
    if polygon.is_empty or polygon.area <= 0 or not polygon.is_valid:
        raise ValueError("wgs_land_path is not a valid polygon")
    return MultiPolygon([polygon])


def _geometry_values(
    geometry: MultiPolygon,
) -> tuple[dict[str, Any], float, tuple[float, float, float, float]]:
    """生成目标库要求的 GeoJSON、几何面积和 WGS84 bbox。"""
    to_equal_area = Transformer.from_crs(
        "EPSG:4326", "EPSG:6933", always_xy=True
    ).transform
    area_ha = shapely_transform(to_equal_area, geometry).area / 10_000
    boundary = json.loads(json.dumps(mapping(geometry), ensure_ascii=False))
    return boundary, round(float(area_ha), 4), tuple(float(v) for v in geometry.bounds)


def _number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("land_area is not numeric") from exc


def normalize_source_row(row: dict[str, Any]) -> SourceParcel:
    """把 MySQL 联表行转成目标主表语义，保留未经解释的源业务字段。"""
    land_id = _required_id(row.get("land_id"), "land_id")
    group_id = _required_id(row.get("group_id") or row.get("farms_id"), "group_id")
    group_name = _string_or_none(row.get("group_name") or row.get("farms_name")) or group_id
    land_name = _string_or_none(row.get("land_name")) or land_id
    path = row.get("wgs_land_path")
    geometry = parse_wgs_land_path(path)
    boundary, area_ha, bounds = _geometry_values(geometry)

    direct_fields = (
        "base_id",
        "province_code",
        "province_name",
        "city_code",
        "city_name",
        "county_code",
        "county_name",
        "town_code",
        "town_name",
        "village_code",
        "village_name",
        "org_code",
        "org_name",
    )
    normalized = {field: _string_or_none(row.get(field)) for field in direct_fields}
    planting_type = _json_safe(row.get("planting_type"))
    business_category = _json_safe(row.get("business_category"))
    source_status = _string_or_none(row.get("source_status"))

    # crop_type 是平台作物标准字段；planting_type 语义未确认，因此只做源字段留存。
    source_properties: dict[str, Any] = {
        "source_system": SOURCE_SYSTEM,
        "source_table": "agriculture_land",
        "source_group_table": "agriculture_land_group",
        "source_land_id": land_id,
        "source_group_id": group_id,
        "planting_type": planting_type,
        "business_category": business_category,
        "source_status": source_status,
        "source_del_flag": "0",
        "wgs_land_path": str(path),
    }
    sync_payload = {
        "land_id": land_id,
        "group_id": group_id,
        "group_name": group_name,
        "land_name": land_name,
        "land_area_mu": _number_or_none(row.get("land_area")),
        **normalized,
        "planting_type": planting_type,
        "business_category": business_category,
        "source_status": source_status,
        "boundary_geojson": boundary,
    }
    rs_payload = {
        "land_id": land_id,
        "group_id": group_id,
        "boundary_geojson": boundary,
        "processing_window_km": settings.mysql_sync_processing_window_km,
    }
    sync_hash = _hash_payload(sync_payload)
    rs_hash = _hash_payload(rs_payload)
    source_properties["sync_hash"] = sync_hash
    source_properties["rs_hash"] = rs_hash

    return SourceParcel(
        land_id=land_id,
        group_id=group_id,
        group_name=group_name,
        country="CN",
        region=None,
        timezone="Asia/Shanghai",
        base_id=normalized["base_id"],
        province_code=normalized["province_code"],
        province_name=normalized["province_name"],
        city_code=normalized["city_code"],
        city_name=normalized["city_name"],
        county_code=normalized["county_code"],
        county_name=normalized["county_name"],
        town_code=normalized["town_code"],
        town_name=normalized["town_name"],
        village_code=normalized["village_code"],
        village_name=normalized["village_name"],
        org_code=normalized["org_code"],
        org_name=normalized["org_name"],
        land_name=land_name,
        land_area_mu=_number_or_none(row.get("land_area")),
        source_status=source_status,
        planting_type=planting_type,
        business_category=business_category,
        boundary_geojson=boundary,
        area_ha=area_ha,
        bounds=bounds,
        source_properties=source_properties,
        sync_hash=sync_hash,
        rs_hash=rs_hash,
    )


def subtract_months(day: date, months: int) -> date:
    """按自然月计算回填起点，避免用 30 天近似造成窗口漂移。"""
    if months < 0:
        raise ValueError("months must be non-negative")
    zero_based = day.year * 12 + day.month - 1 - months
    year, month_index = divmod(zero_based, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _job_id_for(parcel: SourceParcel, date_from: date, date_to: date) -> uuid.UUID:
    return uuid.uuid5(
        JOB_NAMESPACE,
        f"{parcel.land_id}:{parcel.rs_hash}:{date_from.isoformat()}:{date_to.isoformat()}",
    )


def next_sync_at(now: datetime | None = None) -> datetime:
    """返回下一次北京时间 23:00 对应的 UTC 时间。"""
    zone = ZoneInfo(settings.mysql_sync_timezone)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(zone)
    target = local_now.replace(hour=23, minute=0, second=0, microsecond=0)
    if target <= local_now:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def _mysql_engine() -> AsyncEngine:
    url = settings.mysql_source_url.strip()
    if not url:
        raise RuntimeError("MYSQL_SOURCE_URL is required when MySQL sync is enabled")
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_recycle=3600,
        connect_args={"connect_timeout": 10},
    )


def _farm_values(records: list[SourceParcel], now: datetime) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in records:
        by_id[record.group_id] = {
            "id": record.group_id,
            "name": record.group_name,
            "country": record.country,
            "region": record.region,
            "timezone": record.timezone,
            "updated_at": now,
        }
    return list(by_id.values())


def _land_values(
    record: SourceParcel,
    existing: LandParcel | None,
    now: datetime,
) -> dict[str, Any]:
    # 既有 tile_id 影响遥感产品元数据，历史值必须保持；新源数据才使用稳定兜底值。
    tile_id = (existing.tile_id if existing and existing.tile_id else None) or (
        f"mysql_group_{record.group_id}"
    )
    min_lon, min_lat, max_lon, max_lat = record.bounds
    return {
        "land_id": record.land_id,
        "source_parcel_id": record.land_id,
        "tile_id": tile_id,
        "project_key": record.group_id,
        "tile_assignment_type": "source_group",
        "farm_id": record.group_id,
        "land_name": record.land_name,
        "group_id": record.group_id,
        "group_name": record.group_name,
        "org_code": record.org_code,
        "org_name": record.org_name,
        "base_id": record.base_id,
        "province_code": record.province_code,
        "province_name": record.province_name,
        "city_code": record.city_code,
        "city_name": record.city_name,
        "county_code": record.county_code,
        "county_name": record.county_name,
        "town_code": record.town_code,
        "town_name": record.town_name,
        "village_code": record.village_code,
        "village_name": record.village_name,
        "land_status": "1",
        "boundary_geojson": record.boundary_geojson,
        "boundary_srid": 4326,
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "area_ha": record.area_ha,
        "source_properties": record.source_properties,
        "source_file": SOURCE_FILE,
        "source_feature_index": 0,
        "land_area_mu": record.land_area_mu,
        "updated_at": now,
        "deleted_at": None,
    }


async def _apply_batch(
    db: AsyncSession,
    records: list[SourceParcel],
    *,
    run_id: uuid.UUID,
    date_from: date,
    date_to: date,
    summary: dict[str, Any],
) -> None:
    ids = [record.land_id for record in records]
    existing_rows = (
        await db.execute(select(LandParcel).where(LandParcel.land_id.in_(ids)))
    ).scalars().all()
    existing = {str(row.land_id): row for row in existing_rows}

    job_ids = {_job_id_for(record, date_from, date_to) for record in records}
    job_rows = (
        await db.execute(select(Job).where(Job.id.in_(job_ids)))
    ).scalars().all()
    jobs = {row.id: row for row in job_rows}
    now = datetime.now(timezone.utc)

    farm_insert = pg_insert(Farm).values(_farm_values(records, now))
    await db.execute(
        farm_insert.on_conflict_do_update(
            index_elements=[Farm.id],
            set_={
                "name": farm_insert.excluded.name,
                "country": farm_insert.excluded.country,
                "region": farm_insert.excluded.region,
                "timezone": farm_insert.excluded.timezone,
                "updated_at": now,
                "deleted_at": None,
            },
        )
    )

    land_insert = pg_insert(LandParcel).values(
        [_land_values(record, existing.get(record.land_id), now) for record in records]
    )
    land_update_fields = (
        "source_parcel_id",
        "project_key",
        "tile_assignment_type",
        "farm_id",
        "land_name",
        "group_id",
        "group_name",
        "org_code",
        "org_name",
        "base_id",
        "province_code",
        "province_name",
        "city_code",
        "city_name",
        "county_code",
        "county_name",
        "town_code",
        "town_name",
        "village_code",
        "village_name",
        "land_status",
        "boundary_geojson",
        "boundary_srid",
        "min_lon",
        "min_lat",
        "max_lon",
        "max_lat",
        "area_ha",
        "source_properties",
        "source_file",
        "source_feature_index",
        "land_area_mu",
        "updated_at",
        "deleted_at",
    )
    await db.execute(
        land_insert.on_conflict_do_update(
            index_elements=[LandParcel.land_id],
            set_={field: getattr(land_insert.excluded, field) for field in land_update_fields},
        )
    )

    dispatch: list[tuple[SourceParcel, uuid.UUID, Job]] = []
    for record in records:
        old = existing.get(record.land_id)
        old_props = old.source_properties if old and isinstance(old.source_properties, dict) else {}
        geometry_changed = old is None or old.deleted_at is not None or old_props.get("rs_hash") != record.rs_hash
        job_id = _job_id_for(record, date_from, date_to)
        job = jobs.get(job_id)
        if not geometry_changed and job is None:
            # 历史导入数据没有同步任务记录时，仍补建一次近两年任务。
            geometry_changed = True
        if not geometry_changed and job and job.status == "completed":
            continue
        if job is None:
            job = Job(
                id=job_id,
                land_id=record.land_id,
                type="backfill",
                status="pending",
                params_json={
                    "is_backfill": True,
                    "sentinel": True,
                    "months": settings.mysql_sync_rs_months,
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "source": SOURCE_SYSTEM,
                    "sync_run_id": str(run_id),
                    "rs_hash": record.rs_hash,
                    "processing_window_km": settings.mysql_sync_processing_window_km,
                },
            )
            db.add(job)
            jobs[job_id] = job
        elif job.status == "failed":
            # 失败哨兵重新置为 pending，避免旧任务状态阻止后续重试。
            job.status = "pending"
            job.error = None
            job.finished_at = None
            job.params_json = {**(job.params_json or {}), "dispatch_status": None}
        if job.status == "running":
            continue
        if (job.params_json or {}).get("dispatch_status") == "queued":
            continue
        dispatch.append((record, job_id, job))

    await db.commit()

    for record, job_id, job in dispatch:
        try:
            from app.mq_publish import publish_api_task

            # 任务 ID 与源几何 hash 稳定绑定，claim/MQ 重试不会无限创建新任务。
            await asyncio.to_thread(
                publish_api_task,
                type="satellite_analysis",
                land_id=record.land_id,
                task_id=str(job_id),
                extras={
                    "months": settings.mysql_sync_rs_months,
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                    "force": False,
                    "with_bridge": False,
                    "source": SOURCE_SYSTEM,
                    "sync_run_id": str(run_id),
                    "sentinel_job_id": str(job_id),
                    "processing_window_km": settings.mysql_sync_processing_window_km,
                },
            )
        except Exception as exc:
            summary["dispatch_failed"] += 1
            logger.exception(
                "mysql_land_sync_rs_dispatch_failed",
                land_id=record.land_id,
                job_id=str(job_id),
                error=str(exc),
            )
            continue
        job.params_json = {**(job.params_json or {}), "dispatch_status": "queued"}
        summary["rs_dispatched"] += 1

    if dispatch:
        await db.commit()


async def _soft_delete_missing_source_lands(
    db: AsyncSession,
    seen_land_ids: set[str],
    *,
    summary: dict[str, Any],
) -> None:
    """只软删本同步器标记过的源地块，绝不影响 API 手工创建的地块。"""
    if not seen_land_ids:
        summary["soft_delete_skipped"] = "empty_source_snapshot"
        return
    condition = [
        LandParcel.deleted_at.is_(None),
        LandParcel.source_properties["source_system"].as_string() == SOURCE_SYSTEM,
        LandParcel.land_id.notin_(seen_land_ids),
    ]
    result = await db.execute(
        update(LandParcel)
        .where(*condition)
        .values(deleted_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
    )
    summary["soft_deleted"] += int(result.rowcount or 0)


async def _record_audit(summary: dict[str, Any]) -> None:
    from app.core.database import async_session

    async with async_session() as db:
        db.add(
            AuditEvent(
                event_type="mysql_land_sync",
                metadata_json=_json_safe(summary),
            )
        )
        await db.commit()


async def run_land_sync(*, today: date | None = None) -> dict[str, Any]:
    """执行一次完整 MySQL→PostgreSQL 快照同步。"""
    if not settings.mysql_source_enabled:
        return {"status": "disabled"}

    from app.core.database import async_session, engine as target_engine

    run_id = uuid.uuid4()
    business_day = today or datetime.now(ZoneInfo(settings.mysql_sync_timezone)).date()
    date_from = subtract_months(business_day, settings.mysql_sync_rs_months)
    summary: dict[str, Any] = {
        "status": "running",
        "run_id": str(run_id),
        "business_day": business_day.isoformat(),
        "date_from": date_from.isoformat(),
        "date_to": business_day.isoformat(),
        "source_rows": 0,
        "upserted_lands": 0,
        "invalid_rows": 0,
        "invalid_land_ids": [],
        "rs_dispatched": 0,
        "dispatch_failed": 0,
        "soft_deleted": 0,
    }
    source_engine = _mysql_engine()
    seen_land_ids: set[str] = set()

    try:
        # 会话级 advisory lock 跨越分批 commit，避免多副本同时同步。
        async with target_engine.connect() as lock_conn:
            locked = await lock_conn.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:lock_key))"),
                {"lock_key": SYNC_LOCK_KEY},
            )
            if not locked:
                summary["status"] = "skipped_locked"
                return summary

            try:
                async with source_engine.connect() as source_conn:
                    async with async_session() as target_db:
                        batch: list[SourceParcel] = []
                        async_result = await source_conn.stream(SOURCE_SQL)
                        async for row in async_result.mappings():
                            summary["source_rows"] += 1
                            raw_land_id = _string_or_none(row.get("land_id"))
                            if raw_land_id:
                                seen_land_ids.add(raw_land_id)
                            try:
                                record = normalize_source_row(dict(row))
                            except (TypeError, ValueError) as exc:
                                summary["invalid_rows"] += 1
                                if raw_land_id and len(summary["invalid_land_ids"]) < 100:
                                    summary["invalid_land_ids"].append(raw_land_id)
                                logger.warning(
                                    "mysql_land_sync_invalid_row",
                                    land_id=raw_land_id,
                                    error=str(exc),
                                )
                                continue
                            batch.append(record)
                            if len(batch) >= max(int(settings.mysql_sync_batch_size), 1):
                                await _apply_batch(
                                    target_db,
                                    batch,
                                    run_id=run_id,
                                    date_from=date_from,
                                    date_to=business_day,
                                    summary=summary,
                                )
                                summary["upserted_lands"] += len(batch)
                                batch.clear()
                        if batch:
                            await _apply_batch(
                                target_db,
                                batch,
                                run_id=run_id,
                                date_from=date_from,
                                date_to=business_day,
                                summary=summary,
                            )
                            summary["upserted_lands"] += len(batch)

                        # 只有完整读完源快照后才做缺失源地块软删。
                        if summary["source_rows"] > 0:
                            await _soft_delete_missing_source_lands(
                                target_db, seen_land_ids, summary=summary
                            )
                            await target_db.commit()
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:lock_key))"),
                    {"lock_key": SYNC_LOCK_KEY},
                )

        summary["status"] = "partial" if summary["invalid_rows"] or summary["dispatch_failed"] else "completed"
        await _record_audit(summary)
        logger.info("mysql_land_sync_completed", **summary)
        return summary
    except Exception:
        summary["status"] = "failed"
        try:
            await _record_audit(summary)
        except Exception:
            logger.exception("mysql_land_sync_audit_failed", run_id=str(run_id))
        logger.exception("mysql_land_sync_failed", run_id=str(run_id))
        raise
    finally:
        await source_engine.dispose()


async def run_scheduler() -> None:
    """API 机单实例调度器；容器重启后按下一个北京时间 23:00 继续。"""
    while True:
        target = next_sync_at()
        while True:
            delay = (target - datetime.now(timezone.utc)).total_seconds()
            if delay <= 0:
                break
            await asyncio.sleep(min(delay, 300))
        try:
            await run_land_sync()
        except Exception:
            # 失败留给下一次定时任务和人工 --once 重试，避免调度容器退出后无人拉起。
            logger.exception("mysql_land_sync_scheduler_run_failed")
        await asyncio.sleep(1)
