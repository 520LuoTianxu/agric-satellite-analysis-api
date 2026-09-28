"""Canonical land-parcel API.

The application has one parcel master only: agric_satellite.land_parcels.
Every downstream table and message uses its land_id directly; this module
does not translate to a legacy UUID or consult a second parcel table.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from shapely.geometry import MultiPolygon, mapping, shape
from shapely.errors import ShapelyError
from shapely.ops import transform
from shapely.validation import explain_validity
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.concurrency import run_in_threadpool

from agric_satellite_analysis_common.task_priority import MANUAL_TASK_PRIORITY
from app.core.crops import normalize_crop_key
from app.core.database import get_db
from app.core.logging import logger
from app.core.request_body import (
    read_limited_body,
    request_with_body,
    safe_upload_filename,
)
from app.core.rate_limit import limiter
from app.middleware.auth import OrgContext, require_roles
from app.models.tables import (
    AuditEvent,
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
_MAX_GEOJSON_IMPORT_BYTES = 20 * 1024 * 1024
_MAX_GEOJSON_IMPORT_FEATURES = 5000
_MAX_GEOJSON_IMPORT_ERRORS = 100
_GEOJSON_IMPORT_TEXT_LIMITS = {
    "tile_id": 128,
    "land_name": 255,
    "group_id": 64,
    "group_name": 255,
    "province_code": 32,
    "province_name": 64,
    "city_code": 32,
    "city_name": 64,
    "county_code": 32,
    "county_name": 64,
    "town_code": 32,
    "town_name": 64,
    "village_code": 32,
    "village_name": 128,
}


def _geojson_to_multi(geojson: dict[str, Any]) -> MultiPolygon:
    """校验GeoJSON地块边界并统一为MultiPolygon，保证后续投影和掩膜输入合法。"""
    geom = shape(geojson)
    if geom.geom_type == "Polygon":
        geom = MultiPolygon([geom])
    elif geom.geom_type != "MultiPolygon":
        raise ValueError(f"Expected Polygon or MultiPolygon, got {geom.geom_type}")
    if geom.is_empty:
        raise ValueError("Geometry is empty")
    min_lon, min_lat, max_lon, max_lat = geom.bounds
    # land_parcels固定以EPSG:4326存储；拒绝投影坐标或非有限范围，避免面积换算产生坏值。
    if not all(math.isfinite(value) for value in geom.bounds):
        raise ValueError("Geometry bounds must be finite")
    if min_lon < -180 or max_lon > 180 or min_lat < -90 or max_lat > 90:
        raise ValueError("Geometry coordinates must use WGS84 longitude/latitude")
    if not geom.is_valid:
        raise ValueError(f"Invalid geometry: {explain_validity(geom)}")
    return geom


def _geojson_import_text(
    value: Any, field: str, max_length: int | None = None
) -> str | None:
    """验证GeoJSON属性可安全写入文本列，避免坏要素令整批事务失败。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"properties.{field} must be a string or number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"properties.{field} must be finite")
    text_value = str(value).strip()
    try:
        text_value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"properties.{field} contains invalid Unicode") from exc
    if "\x00" in text_value:
        raise ValueError(f"properties.{field} contains a null character")
    if max_length is not None and len(text_value) > max_length:
        raise ValueError(
            f"properties.{field} exceeds the {max_length}-character limit"
        )
    return text_value or None


def _geojson_import_land_id(value: Any) -> str:
    """校验会参与OSS对象键和公开URL构造的稳定地块标识。"""
    land_id = _geojson_import_text(value, "land_id")
    if not land_id:
        raise ValueError("properties.land_id is required")
    if len(land_id) > 64:
        raise ValueError("properties.land_id exceeds the 64-character limit")
    if any(
        char in "/\\?#%"
        or char.isspace()
        or ord(char) < 32
        or ord(char) == 127
        for char in land_id
    ):
        raise ValueError("properties.land_id contains characters unsafe for storage keys")
    if land_id in {".", ".."}:
        raise ValueError("properties.land_id cannot be a path segment")
    return land_id


def _geojson_import_tags(value: Any) -> list[str] | None:
    """将导入标签规范为API约定的字符串数组，避免详情序列化时失败。"""
    if value is None:
        return None
    # 兼容部分GeoJSON把单个标签写成字符串的历史格式。
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(tag, str) for tag in values):
        raise ValueError("properties.tags_json must be a string or an array of strings")
    normalized: list[str] = []
    for tag in values:
        value_text = _geojson_import_text(tag, "tags_json")
        if value_text:
            normalized.append(value_text)
    return normalized


@lru_cache(maxsize=1)
def _equal_area_transformer():
    """复用固定投影变换器，避免每个地块重复构造PROJ资源。"""
    import pyproj

    return pyproj.Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)


def _geometry_values(
    geom: MultiPolygon,
) -> tuple[dict[str, Any], float, tuple[float, ...]]:
    """使用等面积投影计算公顷面积，同时保留API契约所需的WGS84边界。"""
    area_ha = transform(_equal_area_transformer().transform, geom).area / 10_000
    return mapping(geom), round(area_ha, 4), geom.bounds


def _prepare_geojson_import_features(
    features: list[Any], filename: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """在线程池解析各地块几何，单条坏要素只进入错误清单，不中断整批导入。"""
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    omitted_errors = 0
    for index, feature in enumerate(features):
        try:
            props = feature.get("properties") or {}
            if not isinstance(props, dict):
                raise TypeError("properties must be an object")
            land_id = _geojson_import_land_id(props.get("land_id"))
            tile_id = _geojson_import_text(
                props.get("tile_id"), "tile_id", _GEOJSON_IMPORT_TEXT_LIMITS["tile_id"]
            ) or f"manual_{land_id}"
            land_name = _geojson_import_text(
                props.get("land_name") or props.get("name"),
                "land_name",
                _GEOJSON_IMPORT_TEXT_LIMITS["land_name"],
            ) or land_id
            text_fields = {
                field: _geojson_import_text(
                    props.get(field), field, _GEOJSON_IMPORT_TEXT_LIMITS[field]
                )
                for field in _GEOJSON_IMPORT_TEXT_LIMITS
                if field not in {"tile_id", "land_name"} and props.get(field) is not None
            }
            crop_type = _geojson_import_text(props.get("crop_type"), "crop_type")
            tags_value = props.get("tags_json")
            if tags_value is None:
                tags_value = props.get("tags")
            multi = _geojson_to_multi(feature.get("geometry"))
            boundary, area_ha, bounds = _geometry_values(multi)
            records.append(
                {
                    "index": index,
                    "land_id": land_id,
                    "tile_id": tile_id,
                    "land_name": land_name,
                    "text_fields": text_fields,
                    "crop_type": normalize_crop_key(crop_type),
                    "season": _geojson_import_text(props.get("season"), "season"),
                    "tags_json": _geojson_import_tags(tags_value),
                    "boundary": boundary,
                    "area_ha": area_ha,
                    "bounds": bounds,
                    "filename": filename,
                }
            )
        except (TypeError, ValueError, AttributeError, ShapelyError) as exc:
            if len(errors) < _MAX_GEOJSON_IMPORT_ERRORS:
                errors.append(f"Feature {index}: {exc}")
            else:
                omitted_errors += 1
    if omitted_errors:
        errors.append(f"{omitted_errors} additional feature errors omitted")
    return records, errors


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
):
    """Create a parcel row and optionally start its data bootstrap."""
    land_id = body.land_id.strip()
    if not land_id:
        raise HTTPException(status_code=422, detail="land_id is required")
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
    await db.flush()
    await db.commit()

    from app.mq_publish import publish_api_task

    publish_api_task(
        type="land_bootstrap",
        land_id=land_id,
        extras={},
        priority=MANUAL_TASK_PRIORITY,
    )
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


@router.post(
    "/lands/import",
    response_model=LandParcelImportResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def import_lands(
    request: Request,
    farm_id: str | None = Query(None),
    ctx: Annotated[OrgContext, Depends(_writer)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
):
    """批量导入稳定地块ID的GeoJSON；逐要素隔离坏数据并复用一次地块查询。"""
    if farm_id is not None:
        farm = await db.get(Farm, farm_id)
        if not farm or farm.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Farm not found")
    request_body = await read_limited_body(
        request, _MAX_GEOJSON_IMPORT_BYTES + 1024 * 1024
    )
    form = await request_with_body(request, request_body).form(
        max_files=1,
        max_fields=0,
        max_part_size=_MAX_GEOJSON_IMPORT_BYTES,
    )
    try:
        file = form.get("file")
        if not isinstance(file, StarletteUploadFile):
            raise HTTPException(status_code=400, detail="GeoJSON file is required")
        raw = await file.read(_MAX_GEOJSON_IMPORT_BYTES + 1)
        filename = safe_upload_filename(
            file.filename, "geojson_import", max_bytes=255
        )
    finally:
        await form.close()
    if len(raw) > _MAX_GEOJSON_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="GeoJSON file exceeds 20 MiB")
    try:
        geojson = await run_in_threadpool(json.loads, raw)
    except (TypeError, ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    features = geojson.get("features") if isinstance(geojson, dict) else None
    if not isinstance(features, list):
        raise HTTPException(status_code=400, detail="GeoJSON features must be an array")
    if not features:
        raise HTTPException(status_code=400, detail="No features found in GeoJSON")
    if len(features) > _MAX_GEOJSON_IMPORT_FEATURES:
        raise HTTPException(
            status_code=413,
            detail=f"GeoJSON may contain at most {_MAX_GEOJSON_IMPORT_FEATURES} features",
        )

    # 解析和投影计算耗CPU；批量预取地块行后，数据库往返从每要素一次降为一次集合查询。
    records, errors = await run_in_threadpool(
        _prepare_geojson_import_features,
        features,
        filename,
    )
    if records:
        land_ids = list(dict.fromkeys(record["land_id"] for record in records))
        existing_result = await db.execute(
            select(LandParcel).where(LandParcel.land_id.in_(land_ids))
        )
        lands_by_id = {
            land.land_id: land for land in existing_result.scalars().all()
        }
    else:
        lands_by_id = {}

    imported = 0
    for record in records:
        land_id = record["land_id"]
        land = lands_by_id.get(land_id)
        if land is None:
            land = LandParcel(
                land_id=land_id,
                source_parcel_id=land_id,
                tile_id=record["tile_id"],
                source_file=record["filename"],
                source_feature_index=record["index"],
            )
            lands_by_id[land_id] = land
            db.add(land)
        land.farm_id = farm_id
        land.land_name = record["land_name"]
        for attr, value in record["text_fields"].items():
            setattr(land, attr, value)
        land.boundary_geojson = record["boundary"]
        land.boundary_srid = 4326
        land.min_lon, land.min_lat, land.max_lon, land.max_lat = record["bounds"]
        land.area_ha = record["area_ha"]
        land.crop_type = record["crop_type"]
        land.season = record["season"]
        land.tags_json = record["tags_json"]
        land.source_properties = {
            "source": "geojson_import",
            "feature_index": record["index"],
        }
        land.source_file = record["filename"]
        land.source_feature_index = record["index"]
        imported += 1
    if imported:
        await db.commit()
    logger.info(
        "land_geojson_import",
        feature_count=len(features),
        imported=imported,
        invalid_features=len(errors),
        farm_id=farm_id,
    )
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
            Job.land_id == land_id,
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
    """Start a direct land-id backfill wave."""
    await _get_land_or_404(land_id, db)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"backfill:{land_id}"},
    )
    await _fail_stale_backfill_jobs(db, land_id)
    wave_start, _ = await _wave_start_for_land(db, land_id)
    active = (
        await db.execute(
            select(Job.id).where(
                Job.land_id == land_id,
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
    extras["sentinel_job_id"] = str(sentinel.id)
    await db.commit()

    from app.mq_publish import publish_api_task

    publish_api_task(
        type="satellite_analysis",
        land_id=land_id,
        extras=extras,
        priority=MANUAL_TASK_PRIORITY,
    )
    return BackfillIndicesResponse(
        land_id=land_id,
        status="dispatched",
        message=f"已启动 {land_id} 的遥感回填。",
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
                Job.land_id == land_id,
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
    from app.celery_client import send_task

    send_task(
        "app.tasks.backfill.backfill_all_existing_lands",
        kwargs={"months": body.months if body else 60},
    )
    return {"status": "dispatched", "message": "已为所有地块提交遥感回填。"}


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
