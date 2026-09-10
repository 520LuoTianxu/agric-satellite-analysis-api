"""Fields router - CRUD, import, with geometry handling.

Legacy in this fork: primary 地块 model is agri.land_parcels via /v1/agri/lands.
Use /v1/agri/lands/{land_id}/scenes for S1/S2 growth-curve indices.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, shape
from shapely.validation import explain_validity
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.geo import wkb_to_geojson
from app.core.logging import logger
from app.core.crops import normalize_crop_key as _norm_crop
from app.core.rate_limit import limiter
from app.middleware.auth import OrgContext, get_org_context, require_roles, org_scope
from app.models.tables import AuditEvent, Farm, Field, Job
from app.schemas.farm import (
    BackfillIndicesRequest,
    BackfillIndicesResponse,
    BackfillStatusResponse,
    FieldCreate,
    FieldImportResponse,
    FieldOut,
    FieldUpdate,
)

router = APIRouter()

# Dependency: restrict write operations to owner/admin/member (viewers are read-only)
_writer = require_roles("owner", "admin", "member")


def _geojson_to_multi(geojson: dict[str, Any]) -> MultiPolygon:
    """Convert GeoJSON geometry to Shapely MultiPolygon (auto-wrap Polygon)."""
    geom = shape(geojson)
    if geom.geom_type == "Polygon":
        geom = MultiPolygon([geom])
    elif geom.geom_type != "MultiPolygon":
        raise ValueError(f"Expected Polygon or MultiPolygon, got {geom.geom_type}")
    if not geom.is_valid:
        raise ValueError(f"Invalid geometry: {explain_validity(geom)}")
    return geom


def _field_to_out(field: Field) -> FieldOut:
    """Convert ORM Field to FieldOut with GeoJSON geometry."""
    return FieldOut(
        id=field.id,
        farm_id=field.farm_id,
        name=field.name,
        geom=wkb_to_geojson(field.geom),
        area_ha=float(field.area_ha) if field.area_ha else None,
        crop_type=field.crop_type,
        season=field.season,
        tags=field.tags_json,
        created_at=field.created_at,
        updated_at=field.updated_at,
    )


@router.post("/fields", response_model=FieldOut, status_code=status.HTTP_201_CREATED)
async def create_field(
    body: FieldCreate,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Verify farm belongs to org
    farm = await db.get(Farm, body.farm_id)
    if not farm or farm.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Farm not found")

    try:
        multi = _geojson_to_multi(body.geom)
    except (ValueError, Exception) as e:
        raise HTTPException(status_code=400, detail=f"Invalid geometry: {e}")

    from app.core.crops import require_crop_key

    try:
        crop_key = require_crop_key(body.crop_type)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    # Compute area in hectares (approximate using geodesic area)
    from shapely.ops import transform
    import pyproj

    project = pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:6933", always_xy=True
    ).transform
    area_m2 = transform(project, multi).area
    area_ha = area_m2 / 10_000

    field = Field(
        farm_id=body.farm_id,
        name=body.name,
        geom=from_shape(multi, srid=4326),
        area_ha=round(area_ha, 4),
        crop_type=crop_key,
        season=body.season,
        tags_json=body.tags,
    )
    db.add(field)
    await db.flush()

    # Audit event: field_created (per PRD Section 5.1)
    db.add(
        AuditEvent(
            event_type="field_created",
            metadata_json={
                "field_id": str(field.id),
                "farm_id": str(body.farm_id),
                "name": body.name,
            },
        )
    )
    from app.core.agri_tags import is_agri_tagged, parse_agri_land_id

    agri_field = is_agri_tagged(body.tags)
    agri_land_id = parse_agri_land_id(body.tags) if agri_field else None

    # Sentinel job only for classic COG index backfill progress tracking.
    # Agri fields skip RS backfill (truth = agri.parcel_scene_products).
    sentinel = None
    if not agri_field:
        sentinel = Job(
            field_id=field.id,
            type="backfill",
            status="pending",
            params_json={"is_backfill": True, "sentinel": True},
        )
        db.add(sentinel)
        await db.flush()

    logger.info(
        "field_created",
        field_id=str(field.id),
        farm_id=str(body.farm_id),
        name=body.name,
        agri_land_id=agri_land_id,
    )

    # Commit before MQ publish so workers can find the field row
    # in the DB (prevents race condition).
    await db.commit()

    # First-time provision via CloudAMQP field_bootstrap (consumer fans out Celery).
    from app.mq_publish import publish_api_task

    bootstrap_extras: dict[str, Any] = {}
    if agri_field:
        bootstrap_extras["skip_indices"] = True
        if agri_land_id:
            bootstrap_extras["land_id"] = str(agri_land_id)
        logger.info(
            "skip_index_backfill_agri_field",
            field_id=str(field.id),
            land_id=agri_land_id,
            reason="agri-first RS via parcel_scene_products; soil/weather still enqueued",
        )
    elif sentinel is not None:
        bootstrap_extras["sentinel_job_id"] = str(sentinel.id)

    publish_api_task(
        type="field_bootstrap",
        field_id=str(field.id),
        land_id=str(agri_land_id) if agri_land_id else None,
        extras=bootstrap_extras,
    )

    return _field_to_out(field)


@router.get("/fields/{field_id}", response_model=FieldOut)
async def get_field(
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    field = await db.get(Field, field_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Field not found")
    return _field_to_out(field)


@router.put("/fields/{field_id}", response_model=FieldOut)
async def update_field(
    field_id: uuid.UUID,
    body: FieldUpdate,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    field = await db.get(Field, field_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Field not found")

    if body.name is not None:
        field.name = body.name
    if body.crop_type is not None:
        from app.core.crops import require_crop_key

        raw = (
            body.crop_type.strip()
            if isinstance(body.crop_type, str)
            else body.crop_type
        )
        if raw == "" or raw is None:
            field.crop_type = None
        else:
            try:
                field.crop_type = require_crop_key(str(raw))
            except ValueError as e:
                raise HTTPException(status_code=422, detail=str(e)) from e
    if body.season is not None:
        field.season = body.season
    if body.tags is not None:
        field.tags_json = body.tags
    if body.geom is not None:
        try:
            multi = _geojson_to_multi(body.geom)
        except (ValueError, Exception) as e:
            raise HTTPException(status_code=400, detail=f"Invalid geometry: {e}")
        field.geom = from_shape(multi, srid=4326)

        # Recompute area
        from shapely.ops import transform
        import pyproj

        project = pyproj.Transformer.from_crs(
            "EPSG:4326", "EPSG:6933", always_xy=True
        ).transform
        area_m2 = transform(project, multi).area
        field.area_ha = round(area_m2 / 10_000, 4)

    await db.flush()
    return _field_to_out(field)


@router.delete("/fields/{field_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_field(
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    field = await db.get(Field, field_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Field not found")
    field.deleted_at = datetime.now(timezone.utc)
    await db.flush()


@router.post("/fields/import", response_model=FieldImportResponse)
async def import_fields(
    file: UploadFile,
    farm_id: uuid.UUID = Query(...),
    ctx: OrgContext = Depends(_writer),
    db: AsyncSession = Depends(get_db),
):
    """Bulk import fields from GeoJSON file."""
    farm = await db.get(Farm, farm_id)
    if not farm or farm.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Farm not found")

    content = await file.read()
    try:
        geojson = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    features = geojson.get("features", [])
    if not features:
        raise HTTPException(status_code=400, detail="No features found in GeoJSON")

    imported = 0
    errors: list[str] = []

    for i, feature in enumerate(features):
        try:
            geom = feature.get("geometry")
            props = feature.get("properties", {})
            name = props.get("name", f"Field {i + 1}")

            multi = _geojson_to_multi(geom)

            from shapely.ops import transform
            import pyproj

            project = pyproj.Transformer.from_crs(
                "EPSG:4326", "EPSG:6933", always_xy=True
            ).transform
            area_m2 = transform(project, multi).area
            area_ha = round(area_m2 / 10_000, 4)

            field = Field(
                farm_id=farm_id,
                name=name,
                geom=from_shape(multi, srid=4326),
                area_ha=area_ha,
                crop_type=_norm_crop(props.get("crop_type")),
                season=props.get("season"),
            )
            db.add(field)
            imported += 1
        except Exception as e:
            errors.append(f"Feature {i}: {e}")

    if imported:
        await db.flush()

    return FieldImportResponse(imported=imported, errors=errors)


# ── Backfill wave helpers ────────────────────────────────────────────

_BACKFILL_STALE_HOURS = 6
_BACKFILL_WAVE_FALLBACK_HOURS = 48


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
    """Short Chinese status string for the current backfill wave."""
    if phase == "idle":
        return "当前无进行中的遥感回填"
    if phase == "bridge":
        return "正在写入 agri lonlat（光学+雷达）…"
    if phase == "done":
        return f"遥感回填已完成（{completed}/{total}）"
    # stac
    done = completed
    return (
        f"正在拉取光学+雷达遥感数据… 已完成 {done}/{max(total, done + pending + running)}"
        f"（进行中 {running}，排队 {pending}"
        + (f"，失败 {failed}" if failed else "")
        + f"，约 {percent:.0f}%）"
    )


async def _fail_stale_backfill_jobs(db: AsyncSession, field_id: uuid.UUID) -> int:
    """Mark ancient pending/running backfill jobs as failed so UI can idle."""
    from sqlalchemy import update as sa_update

    cutoff = datetime.now(timezone.utc) - timedelta(hours=_BACKFILL_STALE_HOURS)
    result = await db.execute(
        sa_update(Job)
        .where(
            Job.field_id == field_id,
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


async def _wave_start_for_field(db: AsyncSession, field_id: uuid.UUID):
    """Prefer latest sentinel created_at; else last N hours."""
    from sqlalchemy import select as sa_select

    sentinel = (
        await db.execute(
            sa_select(Job)
            .where(
                Job.field_id == field_id,
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
    return datetime.now(timezone.utc) - timedelta(
        hours=_BACKFILL_WAVE_FALLBACK_HOURS
    ), None


# ── Manual index backfill ────────────────────────────────────────────

_admin = require_roles("owner", "admin")


@router.post(
    "/fields/{field_id}/backfill-indices",
    response_model=BackfillIndicesResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit("1/minute")
async def backfill_field_indices(
    request: Request,
    field_id: uuid.UUID,
    body: BackfillIndicesRequest | None = None,
    ctx: OrgContext = Depends(_admin),
    db: AsyncSession = Depends(get_db),
):
    """Trigger historical index backfill for one field (admin/owner only)."""
    from sqlalchemy import select as sa_select

    field = await db.get(Field, field_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Field not found")

    from app.core.agri_tags import is_agri_tagged, parse_agri_land_id

    is_agri = is_agri_tagged(field.tags_json)
    land_id = parse_agri_land_id(field.tags_json) if is_agri else None

    # Fail stale stuck jobs, then check current wave only
    await _fail_stale_backfill_jobs(db, field_id)
    wave_start, _ = await _wave_start_for_field(db, field_id)
    active_count = (
        (
            await db.execute(
                sa_select(Job.id).where(
                    Job.field_id == field_id,
                    Job.status.in_(["pending", "running"]),
                    Job.params_json["is_backfill"].as_boolean().is_(True),
                    Job.created_at >= wave_start,
                )
            )
        )
        .scalars()
        .all()
    )
    if active_count:
        raise HTTPException(
            status_code=409,
            detail=f"Backfill already in progress ({len(active_count)} jobs pending/running).",
        )

    months = body.months if body else 60
    force = body.force if body else False

    # Create sentinel job so status endpoint immediately reflects active backfill
    sentinel = Job(
        field_id=field_id,
        type="backfill",
        status="pending",
        params_json={
            "is_backfill": True,
            "sentinel": True,
            "allow_agri": is_agri,
            "force": force,
        },
    )
    db.add(sentinel)
    await db.flush()

    extras: dict[str, Any] = {
        "months": months,
        "sentinel_job_id": str(sentinel.id),
        "allow_agri": is_agri,
        "force": force,
        "with_bridge": False,
        "dispatch_alerts": False,
    }

    if is_agri:
        bridge_job = Job(
            field_id=field_id,
            type="agri_bridge",
            status="pending",
            params_json={
                "is_backfill": True,
                "phase": "bridge",
                "sentinel_job_id": str(sentinel.id),
                "land_id": str(land_id) if land_id is not None else None,
            },
        )
        db.add(bridge_job)
        await db.flush()
        extras["with_bridge"] = True
        extras["bridge_job_id"] = str(bridge_job.id)
        extras["dispatch_alerts"] = True
        if land_id is not None:
            extras["land_id"] = str(land_id)
        message = (
            f"已启动 {months} 个月遥感回填（光学+雷达，agri 地块）。"
            "将通过 STAC 拉取 Sentinel-2 指数与 Sentinel-1 VV/VH 到 OSS，"
            "再桥接/写入 agri lonlat_v1；完成后请刷新指数面板查看色斑。"
            "预警将按 agri 指数重跑。"
        )
    else:
        message = (
            f"Backfill of {months} months started. "
            "Data will appear over the next few hours."
        )

    # Commit Job sentinels before MQ so workers/status see them.
    await db.commit()

    from app.mq_publish import publish_api_task

    publish_api_task(
        type="satellite_analysis",
        field_id=str(field_id),
        land_id=str(land_id) if land_id is not None else None,
        extras=extras,
    )

    return BackfillIndicesResponse(
        field_id=field_id, status="dispatched", message=message
    )


@router.get("/fields/{field_id}/backfill-status", response_model=BackfillStatusResponse)
async def get_backfill_status(
    field_id: uuid.UUID,
    ctx: OrgContext = Depends(get_org_context),
    db: AsyncSession = Depends(get_db),
):
    """Check current-wave backfill progress for this field.

    Counts are scoped to the latest sentinel wave (or last 48h). Stale
    pending/running jobs older than 6h are auto-failed so the UI can idle.
    """
    from sqlalchemy import func, select as sa_select

    field = await db.get(Field, field_id)
    if not field or field.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Field not found")

    stale_n = await _fail_stale_backfill_jobs(db, field_id)
    if stale_n:
        await db.commit()

    wave_start, sentinel = await _wave_start_for_field(db, field_id)

    # Index / chunk jobs in this wave (exclude sentinel + bridge trackers)
    rows = (
        await db.execute(
            sa_select(
                func.count().filter(Job.status == "pending").label("pending"),
                func.count().filter(Job.status == "running").label("running"),
                func.count().filter(Job.status == "completed").label("completed"),
                func.count().filter(Job.status == "failed").label("failed"),
            ).where(
                Job.field_id == field_id,
                Job.params_json["is_backfill"].as_boolean().is_(True),
                Job.created_at >= wave_start,
                Job.type.notin_(["backfill", "agri_bridge"]),
            )
        )
    ).one()

    pending = int(rows.pending or 0)
    running = int(rows.running or 0)
    completed = int(rows.completed or 0)
    failed = int(rows.failed or 0)

    # Bridge tracker for agri fields
    bridge = (
        await db.execute(
            sa_select(Job)
            .where(
                Job.field_id == field_id,
                Job.type == "agri_bridge",
                Job.params_json["is_backfill"].as_boolean().is_(True),
                Job.created_at >= wave_start,
            )
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    sentinel_active = bool(
        sentinel is not None and sentinel.status in ("pending", "running")
    )
    bridge_active = bool(bridge is not None and bridge.status in ("pending", "running"))
    stac_active = (pending + running) > 0 or sentinel_active

    denom = pending + running + completed
    percent = (
        (100.0 * completed / denom)
        if denom > 0
        else (100.0 if not stac_active and not bridge_active else 0.0)
    )
    total_jobs = denom + failed

    if stac_active:
        phase = "stac"
    elif bridge_active:
        phase = "bridge"
        # Treat STAC as done while bridging
        if denom > 0:
            percent = 100.0
    elif (
        sentinel is not None
        or denom > 0
        or (bridge is not None and bridge.status == "completed")
    ):
        # Wave existed; now idle/done
        phase = (
            "done"
            if (bridge is None or bridge.status == "completed") and denom > 0
            else "idle"
        )
        if phase == "done" and denom > 0:
            percent = 100.0 * completed / denom
    else:
        phase = "idle"

    has_active = phase in ("stac", "bridge") or stac_active or bridge_active
    if phase == "done" and not has_active:
        # One-shot "done" for clients that just finished; treat as inactive for polling stop
        has_active_flag = False
    else:
        has_active_flag = has_active

    message = _backfill_wave_message(
        phase=phase if has_active_flag or phase == "done" else "idle",
        pending=pending,
        running=running,
        completed=completed,
        failed=failed,
        total=max(total_jobs, denom),
        percent=percent,
    )

    return BackfillStatusResponse(
        field_id=field_id,
        has_active_backfill=has_active_flag,
        pending_jobs=pending,
        running_jobs=running,
        completed_jobs=completed,
        failed_jobs=failed,
        total_jobs=max(total_jobs, denom),
        percent=round(percent, 1),
        phase=phase if has_active_flag else ("done" if phase == "done" else "idle"),
        message=message,
    )


@router.post("/admin/backfill-all-fields", status_code=status.HTTP_202_ACCEPTED)
async def backfill_all_fields(
    request: Request,
    body: BackfillIndicesRequest | None = None,
    ctx: OrgContext = Depends(require_roles("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Trigger backfill for ALL active fields (owner only, one-time migration)."""
    months = body.months if body else 60

    from app.celery_client import send_task

    send_task(
        "app.tasks.backfill.backfill_all_existing_fields", kwargs={"months": months}
    )

    return {
        "status": "dispatched",
        "message": f"Bulk backfill of {months} months dispatched for all active fields.",
    }


@router.post("/admin/ensure-agri-soil-weather", status_code=status.HTTP_202_ACCEPTED)
async def ensure_agri_soil_weather(
    ctx: OrgContext = Depends(require_roles("owner")),
    db: AsyncSession = Depends(get_db),
    farm_id: uuid.UUID | None = Query(None, description="Optional farm filter"),
):
    """Enqueue soil + weather backfill for agri-tagged fields in this org.

    Does not touch RS / COG backfill. Geom sync (if missing) is handled by
    scripts/agri_seed/ensure_agri_field_soil_weather.py.
    """
    from sqlalchemy import select as sa_select

    from app.celery_client import send_task
    from app.core.agri_tags import is_agri_tagged, parse_agri_land_id
    from app.models.tables import SoilFieldSummary

    q = sa_select(Field).where(org_scope(None, ctx), Field.deleted_at.is_(None))
    if farm_id is not None:
        q = q.where(Field.farm_id == farm_id)

    fields = (await db.execute(q)).scalars().all()
    soil_enqueued = 0
    weather_enqueued = 0
    scanned = 0
    items: list[dict[str, Any]] = []

    for field in fields:
        if not is_agri_tagged(field.tags_json):
            continue
        scanned += 1
        land_id = parse_agri_land_id(field.tags_json)
        summary = (
            await db.execute(
                sa_select(SoilFieldSummary.id).where(
                    SoilFieldSummary.field_id == field.id
                )
            )
        ).scalar_one_or_none()
        need_soil = summary is None
        # Always refresh weather if caller hits this admin path for agri fields
        # with zero weather rows — cheap check via exists on weather_daily.
        from app.models.tables import WeatherDaily

        weather_exists = (
            await db.execute(
                sa_select(WeatherDaily.id)
                .where(WeatherDaily.field_id == field.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        need_weather = weather_exists is None

        if need_soil:
            send_task("app.tasks.soil.fetch_soil_for_field", args=[str(field.id)])
            soil_enqueued += 1
        if need_weather:
            send_task(
                "app.tasks.weather.backfill_weather_for_field", args=[str(field.id)]
            )
            weather_enqueued += 1

        items.append(
            {
                "field_id": str(field.id),
                "name": field.name,
                "land_id": land_id,
                "soil_enqueued": need_soil,
                "weather_enqueued": need_weather,
            }
        )

    logger.info(
        "ensure_agri_soil_weather",
        scanned=scanned,
        soil_enqueued=soil_enqueued,
        weather_enqueued=weather_enqueued,
    )
    return {
        "status": "dispatched",
        "scanned": scanned,
        "soil_enqueued": soil_enqueued,
        "weather_enqueued": weather_enqueued,
        "items": items,
    }
