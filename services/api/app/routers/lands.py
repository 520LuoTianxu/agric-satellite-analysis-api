"""Canonical land-parcel API.

The application has one parcel master only: agric_satellite.land_parcels.
Every downstream table and message uses its land_id directly; this module
does not translate to a legacy UUID or consult a second parcel table.
"""

from __future__ import annotations

import json
import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from agric_satellite_analysis_common.scheduled_land_filter import (
    EXCLUDED_SCHEDULE_BASE_IDS,
    MAX_SCHEDULE_LAND_AREA_MU,
)
from shapely.geometry import MultiPolygon, mapping, shape
from shapely.ops import transform
from shapely.validation import explain_validity
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.task_priority import MANUAL_TASK_PRIORITY
from app.core.config import settings
from app.core.crops import normalize_crop_key
from app.core.database import get_db
from app.core.logging import logger
from app.core.rate_limit import limiter
from app.middleware.auth import OrgContext, require_roles
from app.models.tables import (
    AuditEvent,
    Alert,
    Farm,
    LandParcel,
    Job,
    SoilFieldSummary,
    WeatherDaily,
)
from app.schemas.common import PaginatedResponse
from app.schemas.farm import (
    BackfillIndicesRequest,
    BackfillIndicesResponse,
    BackfillStatusResponse,
    BoundaryReview,
    LandParcelCreate,
    LandParcelImportResponse,
    LandParcelOut,
    LandParcelUpdate,
)
from app.services.mysql_land_sync import sync_selected_lands

router = APIRouter()

_reader = require_roles("owner", "admin", "member", "viewer")
_writer = require_roles("owner", "admin", "member")
_admin = require_roles("owner", "admin")

_BACKFILL_STALE_HOURS = 6
_BACKFILL_WAVE_FALLBACK_HOURS = 48


def _geojson_to_multi(geojson: dict[str, Any]) -> MultiPolygon:
    """Validate a GeoJSON polygon and normalize it to MultiPolygon."""
    geom = shape(geojson)
    if geom.geom_type == "Polygon":
        geom = MultiPolygon([geom])
    elif geom.geom_type != "MultiPolygon":
        raise ValueError(f"Expected Polygon or MultiPolygon, got {geom.geom_type}")
    if not geom.is_valid:
        raise ValueError(f"Invalid geometry: {explain_validity(geom)}")
    return geom


def _geometry_values(
    geom: MultiPolygon,
) -> tuple[dict[str, Any], float, tuple[float, ...]]:
    """Return canonical boundary JSON, area in hectares, and WGS84 bounds."""
    import pyproj

    to_equal_area = pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:6933", always_xy=True
    ).transform
    area_ha = transform(to_equal_area, geom).area / 10_000
    return mapping(geom), round(area_ha, 4), geom.bounds


def _land_to_out(land: LandParcel) -> LandParcelOut:
    """Serialize the canonical parcel row without manufacturing another ID."""
    boundary = land.boundary_geojson if isinstance(land.boundary_geojson, dict) else {}
    return LandParcelOut(
        land_id=land.land_id,
        source_parcel_id=land.source_parcel_id,
        tile_id=land.tile_id,
        virtual_tile_id=land.virtual_tile_id,
        project_key=land.project_key,
        tile_assignment_type=land.tile_assignment_type,
        tile_anchor_land_id=land.tile_anchor_land_id,
        farm_id=land.farm_id,
        land_name=land.land_name,
        group_id=land.group_id,
        group_name=land.group_name,
        org_code=land.org_code,
        org_name=land.org_name,
        base_id=land.base_id,
        province_code=land.province_code,
        province_name=land.province_name,
        city_code=land.city_code,
        city_name=land.city_name,
        county_code=land.county_code,
        county_name=land.county_name,
        town_code=land.town_code,
        town_name=land.town_name,
        village_code=land.village_code,
        village_name=land.village_name,
        land_area_mu=(float(land.land_area_mu) if land.land_area_mu is not None else None),
        boundary_geojson=boundary,
        boundary_srid=land.boundary_srid,
        min_lon=land.min_lon,
        min_lat=land.min_lat,
        max_lon=land.max_lon,
        max_lat=land.max_lat,
        # 旧客户端仍读取 geom；这里返回同一份 JSONB 边界作为兼容别名。
        geom=boundary or None,
        area_ha=float(land.area_ha) if land.area_ha is not None else None,
        crop_type=land.crop_type,
        season=land.season,
        tags_json=land.tags_json,
        soil_property=land.soil_property,
        current_batch=land.current_batch,
        land_status=land.land_status,
        source_properties=land.source_properties,
        source_file=land.source_file,
        source_feature_index=land.source_feature_index,
        source_update_time=land.source_update_time,
        created_at=land.created_at,
        updated_at=land.updated_at,
    )


async def _get_land_or_404(land_id: str, db: AsyncSession) -> LandParcel:
    """Load one active parcel by its only public identity."""
    land = await db.get(LandParcel, land_id)
    if not land or land.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Land parcel not found")
    return land


async def _get_land_or_sync(land_id: str, db: AsyncSession) -> LandParcel:
    """查询地块；主库没有时同步 Smart 后再读取一次。"""
    land = await db.get(LandParcel, land_id)
    if land and land.deleted_at is None:
        return land

    try:
        # 这里必须等待 Smart 同步完成，保证本次 GET 能直接拿到 farms 和
        # land_parcels 的最新数据，而不是把首次查询转成异步后台任务后仍返回 404。
        summary = await sync_selected_lands([land_id])
    except Exception as exc:
        logger.exception("land_lookup_smart_sync_failed", land_id=land_id)
        raise HTTPException(status_code=503, detail="Smart 地块数据同步失败") from exc

    # 首次查询可能已经开启了当前会话事务；同步使用独立会话提交后，先结束旧事务，
    # 再读取新提交的数据，也能正确恢复此前被软删除的地块。
    await db.rollback()
    land = await db.get(LandParcel, land_id)
    if land and land.deleted_at is None:
        return land

    sync_status = summary.get("status")
    if sync_status == "skipped_locked":
        raise HTTPException(status_code=409, detail="Smart 地块同步正在进行，请稍后重试")
    if sync_status == "disabled":
        raise HTTPException(
            status_code=503,
            detail="Smart/MySQL source is not enabled; set MYSQL_SOURCE_ENABLED=true",
        )
    if sync_status in {"filtered", "invalid"}:
        raise HTTPException(status_code=422, detail="Smart 地块数据无法同步")
    raise HTTPException(status_code=404, detail="Land parcel not found in Smart source")


def _normalized_crop(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        return normalize_crop_key(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/lands", response_model=PaginatedResponse[LandParcelOut])
async def list_lands(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    farm_id: str | None = Query(None),
    group_id: str | None = Query(None, description="Filter by planting group_id"),
    q: str | None = Query(None, description="Search land_id or land_name"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List active canonical parcels."""
    filters = [LandParcel.deleted_at.is_(None)]
    if farm_id is not None:
        filters.append(LandParcel.farm_id == farm_id)
    if group_id and group_id.strip():
        filters.append(LandParcel.group_id == group_id.strip())
    if q and q.strip():
        needle = f"%{q.strip()}%"
        filters.append(
            (LandParcel.land_id.ilike(needle) | LandParcel.land_name.ilike(needle))
        )
    base = select(LandParcel).where(*filters)
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    rows = (
        await db.execute(
            base.order_by(LandParcel.created_at.desc(), LandParcel.land_id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return PaginatedResponse(
        items=[_land_to_out(row) for row in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.post("/lands", response_model=LandParcelOut, status_code=status.HTTP_201_CREATED)
async def create_land(
    body: LandParcelCreate,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
    hr_base_id: Annotated[str | None, Header(alias="Hr-Base-Id")] = None,
):
    """Create a parcel row and optionally start its data bootstrap."""
    land_id = body.land_id.strip()
    if not land_id:
        raise HTTPException(status_code=422, detail="land_id is required")
    raw_base_id = (hr_base_id or "").strip()
    if raw_base_id and (
        not raw_base_id.isascii() or not raw_base_id.isdecimal() or int(raw_base_id) <= 0
    ):
        raise HTTPException(status_code=400, detail="Invalid Hr-Base-Id")
    base_id = str(int(raw_base_id)) if raw_base_id else None
    if body.farm_id is not None:
        farm = await db.get(Farm, body.farm_id)
        if not farm or farm.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Farm not found")
    try:
        multi = _geojson_to_multi(body.boundary_geojson)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid boundary: {exc}") from exc
    boundary, area_ha, bounds = _geometry_values(multi)
    min_lon, min_lat, max_lon, max_lat = bounds
    land = LandParcel(
        land_id=land_id,
        source_parcel_id=land_id,
        tile_id=body.tile_id or f"manual_{land_id}",
        farm_id=body.farm_id,
        # 预警列表按基地隔离；新建地块继承当前基地，避免预警被租户过滤条件隐藏。
        base_id=base_id,
        land_name=body.land_name,
        group_id=body.group_id,
        group_name=body.group_name,
        province_code=body.province_code,
        province_name=body.province_name,
        city_code=body.city_code,
        city_name=body.city_name,
        county_code=body.county_code,
        county_name=body.county_name,
        town_code=body.town_code,
        town_name=body.town_name,
        village_code=body.village_code,
        village_name=body.village_name,
        boundary_geojson=boundary,
        boundary_srid=4326,
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        area_ha=area_ha,
        crop_type=_normalized_crop(body.crop_type),
        season=body.season,
        tags_json=body.tags_json,
        source_properties={"source": "api"},
        source_file="api",
        source_feature_index=0,
    )
    db.add(land)
    db.add(
        AuditEvent(
            event_type="land_created",
            metadata_json={"land_id": land_id, "farm_id": str(body.farm_id or "")},
        )
    )
    review: BoundaryReview | None = body.boundary_review
    if review is not None:
        # OSM 建筑/居民区是边界复核线索；矢量底图缺少要素时不伪造“无建筑”结论。
        alert_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if review.building_count > 0:
            db.add(
                Alert(
                    land_id=land_id,
                    date=alert_date,
                    severity="high" if review.building_count >= 5 else "medium",
                    rule_name="boundary_building_overlap",
                    rule_params_json={
                        "source": review.source,
                        "building_count": review.building_count,
                    },
                    message=(
                        f"所画地块边界与地图中的 {review.building_count} 处建筑轮廓重叠；"
                        "建筑数据可能不完整，请核对房屋是否被圈入农田。"
                    ),
                    status="open",
                    index_type=None,
                )
            )
        if review.residential_overlap:
            db.add(
                Alert(
                    land_id=land_id,
                    date=alert_date,
                    severity="medium",
                    rule_name="boundary_residential_overlap",
                    rule_params_json={"source": review.source},
                    message="所画地块边界与地图标记的居民区重叠；请核对是否把房区圈入农田地块。",
                    status="open",
                    index_type=None,
                )
            )
    await db.flush()
    await db.commit()

    from app.mq_publish import publish_api_task

    publish_api_task(
        type="land_bootstrap",
        land_id=land_id,
        extras={"skip_indices": True},
        priority=MANUAL_TASK_PRIORITY,
    )
    # 新建地块的历史遥感回填也走同一项目区窗口，land_bootstrap 只负责天气/土壤。
    try:
        from app.services.virtual_area_service import backfill_virtual_area_history

        await backfill_virtual_area_history(
            land_ids=[land_id],
            years=5,
            sensors=("S1", "S2"),
        )
    except Exception:
        logger.exception("land_created_vpa10_history_dispatch_failed", land_id=land_id)
    logger.info("land_created", land_id=land_id, farm_id=str(body.farm_id or ""))
    return _land_to_out(land)


@router.get("/lands/{land_id}", response_model=LandParcelOut)
async def get_land(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _land_to_out(await _get_land_or_sync(land_id, db))


@router.put("/lands/{land_id}", response_model=LandParcelOut)
async def update_land(
    land_id: str,
    body: LandParcelUpdate,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    land = await _get_land_or_404(land_id, db)
    if body.farm_id is not None:
        farm = await db.get(Farm, body.farm_id)
        if not farm or farm.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Farm not found")
        land.farm_id = body.farm_id
    if body.land_name is not None:
        land.land_name = body.land_name
    if body.tile_id is not None:
        land.tile_id = body.tile_id
    for attr in (
        "group_id",
        "group_name",
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
    ):
        value = getattr(body, attr)
        if value is not None:
            setattr(land, attr, value)
    if body.crop_type is not None:
        land.crop_type = _normalized_crop(body.crop_type)
    if body.season is not None:
        land.season = body.season
    if body.tags_json is not None:
        land.tags_json = body.tags_json
    if body.boundary_geojson is not None:
        try:
            multi = _geojson_to_multi(body.boundary_geojson)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid boundary: {exc}") from exc
        boundary, area_ha, bounds = _geometry_values(multi)
        land.boundary_geojson = boundary
        land.boundary_srid = 4326
        land.min_lon, land.min_lat, land.max_lon, land.max_lat = bounds
        land.area_ha = area_ha
    land.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(land)
    return _land_to_out(land)


@router.delete("/lands/{land_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_land(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    land = await _get_land_or_404(land_id, db)
    land.deleted_at = datetime.now(timezone.utc)
    await db.commit()


@router.post("/lands/import", response_model=LandParcelImportResponse)
async def import_lands(
    file: UploadFile,
    farm_id: str | None = Query(None),
    ctx: Annotated[OrgContext, Depends(_writer)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Import GeoJSON features; every feature must provide a stable land_id."""
    if farm_id is not None:
        farm = await db.get(Farm, farm_id)
        if not farm or farm.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Farm not found")
    try:
        geojson = json.loads(await file.read())
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    features = geojson.get("features", []) if isinstance(geojson, dict) else []
    if not features:
        raise HTTPException(status_code=400, detail="No features found in GeoJSON")

    imported = 0
    errors: list[str] = []
    for index, feature in enumerate(features):
        try:
            props = feature.get("properties") or {}
            land_id = str(props.get("land_id") or "").strip()
            if not land_id:
                raise ValueError("properties.land_id is required")
            multi = _geojson_to_multi(feature.get("geometry"))
            boundary, area_ha, bounds = _geometry_values(multi)
            existing = await db.get(LandParcel, land_id)
            land = existing or LandParcel(
                land_id=land_id,
                source_parcel_id=land_id,
                tile_id=str(props.get("tile_id") or f"manual_{land_id}"),
                source_file=file.filename or "geojson_import",
                source_feature_index=index,
            )
            land.farm_id = farm_id
            land.land_name = str(props.get("land_name") or props.get("name") or land_id)
            for attr in (
                "group_id",
                "group_name",
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
            ):
                if props.get(attr) is not None:
                    setattr(land, attr, props[attr])
            land.boundary_geojson = boundary
            land.boundary_srid = 4326
            land.min_lon, land.min_lat, land.max_lon, land.max_lat = bounds
            land.area_ha = area_ha
            land.crop_type = _normalized_crop(props.get("crop_type"))
            land.season = props.get("season")
            land.tags_json = props.get("tags_json") or props.get("tags")
            land.source_properties = {"source": "geojson_import", "feature_index": index}
            land.source_file = file.filename or "geojson_import"
            land.source_feature_index = index
            if existing is None:
                db.add(land)
            imported += 1
        except (TypeError, ValueError, AttributeError) as exc:
            errors.append(f"Feature {index}: {exc}")
    if imported:
        await db.commit()
    return LandParcelImportResponse(imported=imported, errors=errors)


def _backfill_wave_message(
    *,
    phase: str,
    pending: int,
    running: int,
    completed: int,
    failed: int,
    total: int,
    percent: float,
) -> str:
    if phase == "idle":
        return "当前无进行中的遥感回填"
    if phase == "bridge":
        return "正在写入遥感结果"
    if phase == "done":
        return f"遥感回填已完成（分片任务 {completed}/{total}）"
    return (
        f"正在拉取遥感数据… 分片任务 {completed}/{max(total, completed + pending + running)}"
        f"（进行中 {running}，排队 {pending}"
        + (f"，失败 {failed}" if failed else "")
        + f"，约 {percent:.0f}%）"
    )


async def _fail_stale_backfill_jobs(db: AsyncSession, land_id: str) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_BACKFILL_STALE_HOURS)
    result = await db.execute(
        Job.__table__.update()
        .where(
            or_(
                Job.land_id == land_id,
                Job.params_json["land_ids"].contains([land_id]),
            ),
            Job.status.in_(["pending", "running"]),
            Job.params_json["is_backfill"].as_boolean().is_(True),
            Job.created_at < cutoff,
        )
        .values(
            status="failed",
            error=f"Stale backfill auto-cancelled after {_BACKFILL_STALE_HOURS}h",
            finished_at=datetime.now(timezone.utc),
        )
    )
    return result.rowcount or 0


async def _wave_start_for_land(db: AsyncSession, land_id: str):
    sentinel = (
        await db.execute(
            select(Job)
            .where(
                Job.land_id == land_id,
                Job.type == "backfill",
                Job.params_json["is_backfill"].as_boolean().is_(True),
                Job.params_json["sentinel"].as_boolean().is_(True),
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sentinel is not None:
        return sentinel.created_at, sentinel
    return datetime.now(timezone.utc) - timedelta(hours=_BACKFILL_WAVE_FALLBACK_HOURS), None


@router.post(
    "/lands/{land_id}/backfill-indices",
    response_model=BackfillIndicesResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("5/minute")
async def backfill_land_indices(
    request: Request,
    land_id: str,
    body: BackfillIndicesRequest | None = None,
    ctx: Annotated[OrgContext, Depends(_admin)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """将单地块回填纳入虚拟项目区共享下载，并保持原有状态查询语义。"""
    land = await _get_land_or_404(land_id, db)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"backfill:{land_id}"},
    )
    await _fail_stale_backfill_jobs(db, land_id)
    wave_start, _ = await _wave_start_for_land(db, land_id)
    active = (
        await db.execute(
            select(Job.id).where(
                or_(
                    Job.land_id == land_id,
                    Job.params_json["land_ids"].contains([land_id]),
                ),
                Job.status.in_(["pending", "running"]),
                Job.params_json["is_backfill"].as_boolean().is_(True),
                Job.created_at >= wave_start,
            )
        )
    ).scalars().all()
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Backfill already in progress ({len(active)} jobs pending/running).",
        )

    months = body.months if body else 24
    extras: dict[str, Any] = {
        "months": months,
        "force": bool(body.force) if body else False,
        "with_bridge": False,
        "dispatch_alerts": True,
    }
    if body:
        if body.date_from:
            extras["date_from"] = str(body.date_from)[:10]
        if body.date_to:
            extras["date_to"] = str(body.date_to)[:10]
        if body.growing_seasons:
            extras["growing_seasons"] = [
                item.model_dump(exclude_none=True) for item in body.growing_seasons
            ]
        if body.season_months:
            extras["season_months"] = body.season_months

    sentinel = Job(
        land_id=land_id,
        type="backfill",
        status="pending",
        params_json={"is_backfill": True, "sentinel": True, **extras},
    )
    db.add(sentinel)
    await db.flush()
    sentinel_id = sentinel.id
    extras["sentinel_job_id"] = str(sentinel_id)
    await db.commit()

    date_to = date.fromisoformat(extras.get("date_to") or date.today().isoformat())
    date_from = date.fromisoformat(
        extras.get("date_from")
        or (date_to - timedelta(days=months * 30)).isoformat()
    )
    from app.services.virtual_area_service import build_vpa10_satellite_jobs

    try:
        _, jobs, _ = await build_vpa10_satellite_jobs(
            db,
            [land],
            date_from=date_from,
            date_to=date_to,
            sensors=("S1", "S2"),
            force=bool(extras["force"]),
            parent_job_id=sentinel_id,
            job_land_id=land_id,
            assigned_by="manual-land-backfill",
            chunk_days=settings.index_backfill_chunk_days,
            extra_params={
                "is_backfill": True,
                "sentinel_job_id": str(sentinel_id),
                "season_months": extras.get("season_months"),
                "growing_seasons": extras.get("growing_seasons"),
            },
        )
    except ValueError as exc:
        await db.rollback()
        sentinel = await db.get(Job, sentinel_id)
        if sentinel is not None:
            sentinel.status = "failed"
            sentinel.error = str(exc)[:3900]
            sentinel.finished_at = datetime.now(timezone.utc)
            await db.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # sentinel 只标记任务拆分完成，实际进度由它下面的 S1/S2 项目区 Job 汇总。
    sentinel.status = "completed"
    sentinel.finished_at = datetime.now(timezone.utc)
    await db.commit()

    from app.mq_publish import publish_api_task

    for job in jobs:
        try:
            await asyncio.to_thread(
                publish_api_task,
                type="satellite_batch",
                land_id=job.land_id,
                task_id=str(job.id),
                extras={"job_id": str(job.id)},
                priority=MANUAL_TASK_PRIORITY,
            )
        except Exception as exc:
            job.status = "failed"
            job.error = f"项目区回填任务派发失败：{str(exc)[:3900]}"
    # 遥感已由 VPA10 共享任务负责；天气仍按地块拉取并复用同一回填时间窗。
    try:
        await asyncio.to_thread(
            publish_api_task,
            type="weather_backfill",
            land_id=land_id,
            task_id=str(uuid.uuid5(sentinel_id, "weather-backfill")),
            extras={
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
            },
            priority=MANUAL_TASK_PRIORITY,
        )
    except Exception:
        logger.exception("manual_backfill_weather_dispatch_failed", land_id=land_id)
    await db.commit()
    return BackfillIndicesResponse(
        land_id=land_id,
        status="dispatched" if any(job.status != "failed" for job in jobs) else "failed",
        message=(
            f"已启动 {land_id} 的虚拟项目区遥感回填。"
            if any(job.status != "failed" for job in jobs)
            else f"{land_id} 的遥感回填任务未能派发。"
        ),
    )


@router.get(
    "/lands/{land_id}/backfill-status", response_model=BackfillStatusResponse
)
async def get_backfill_status(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _get_land_or_404(land_id, db)
    await _fail_stale_backfill_jobs(db, land_id)
    wave_start, sentinel = await _wave_start_for_land(db, land_id)
    row = (
        await db.execute(
            select(
                func.count().filter(Job.status == "pending").label("pending"),
                func.count().filter(Job.status == "running").label("running"),
                func.count().filter(Job.status == "completed").label("completed"),
                func.count().filter(Job.status == "failed").label("failed"),
            ).where(
                or_(
                    Job.land_id == land_id,
                    Job.params_json["land_ids"].contains([land_id]),
                ),
                Job.params_json["is_backfill"].as_boolean().is_(True),
                Job.created_at >= wave_start,
                Job.type.notin_(["backfill", "agri_bridge"]),
            )
        )
    ).one()
    pending, running = int(row.pending or 0), int(row.running or 0)
    completed, failed = int(row.completed or 0), int(row.failed or 0)
    denom = pending + running + completed
    sentinel_active = bool(sentinel and sentinel.status in ("pending", "running"))
    active = sentinel_active or pending > 0 or running > 0
    phase = "stac" if active else ("done" if denom else "idle")
    percent = 100.0 * completed / denom if denom else (0.0 if active else 100.0)
    return BackfillStatusResponse(
        land_id=land_id,
        has_active_backfill=active,
        pending_jobs=pending,
        running_jobs=running,
        completed_jobs=completed,
        failed_jobs=failed,
        total_jobs=denom + failed,
        percent=round(percent, 1),
        phase=phase,
        message=_backfill_wave_message(
            phase=phase,
            pending=pending,
            running=running,
            completed=completed,
            failed=failed,
            total=denom + failed,
            percent=percent,
        ),
    )


@router.post("/admin/backfill-all-lands", status_code=status.HTTP_202_ACCEPTED)
async def backfill_all_lands(
    body: BackfillIndicesRequest | None = None,
    ctx: Annotated[OrgContext, Depends(require_roles("owner"))] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    lands = (
        await db.execute(
            select(LandParcel.land_id)
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
    ).scalars().all()
    end_date = date.today()
    months = body.months if body else 60
    start_date = end_date - timedelta(days=months * 30)
    from app.services.virtual_area_service import backfill_virtual_area_history

    result = await backfill_virtual_area_history(
        land_ids=[str(value) for value in lands],
        date_from=start_date,
        date_to=end_date,
        sensors=("S1", "S2"),
        force=bool(body.force) if body else False,
    )
    return {
        **result,
        "land_count": len(lands),
        "message": "已按10×10公里虚拟项目区提交历史遥感回填。",
    }


@router.post("/admin/ensure-soil-weather", status_code=status.HTTP_202_ACCEPTED)
async def ensure_soil_weather(
    farm_id: str | None = Query(None),
    ctx: Annotated[OrgContext, Depends(require_roles("owner"))] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """Ensure soil/weather data for every canonical parcel; tags are not required."""
    from app.celery_client import send_task

    query = select(LandParcel).where(LandParcel.deleted_at.is_(None))
    if farm_id is not None:
        query = query.where(LandParcel.farm_id == farm_id)
    lands = (await db.execute(query)).scalars().all()
    items: list[dict[str, Any]] = []
    for land in lands:
        soil_exists = (
            await db.execute(
                select(SoilFieldSummary.id)
                .where(SoilFieldSummary.land_id == land.land_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        weather_exists = (
            await db.execute(
                select(WeatherDaily.id)
                .where(WeatherDaily.land_id == land.land_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if soil_exists is None:
            send_task("app.tasks.soil.fetch_soil_for_land", args=[land.land_id])
        if weather_exists is None:
            send_task("app.tasks.weather.backfill_weather_for_land", args=[land.land_id])
        items.append(
            {
                "land_id": land.land_id,
                "land_name": land.land_name,
                "soil_enqueued": soil_exists is None,
                "weather_enqueued": weather_exists is None,
            }
        )
    return {"status": "dispatched", "scanned": len(lands), "items": items}
