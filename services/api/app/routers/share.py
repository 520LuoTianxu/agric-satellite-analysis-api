"""Share links router - create, list, revoke, public read, tile proxy."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from jose import jwt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agri_tags import parse_agri_land_id
from app.core.config import settings
from app.core.database import get_db
from app.core.logging import logger
from app.middleware.auth import OrgContext, get_org_context, require_roles, org_matches, org_scope
from app.models.tables import (
    Alert,
    AuditEvent,
    Field,
    FieldStat,
    RasterLayer,
    ScoutingObservation,
    ShareLink,
    SoilFieldSummary,
    WeatherDaily,
)
from app.schemas.monitoring import (
    ScoutingOut,
    ShareCreate,
    ShareOut,
    ShareReportOut,
    ShareStatPoint,
)

router = APIRouter()

# Dependency: restrict write operations to owner/admin/member (viewers are read-only)
_writer = require_roles("owner", "admin", "member")

# ── Tile proxy helpers ────────────────────────────────────────────────


def _make_empty_tile() -> bytes:
    """Generate a valid 256×256 transparent PNG at import time."""
    import struct
    import zlib

    width, height = 256, 256

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    scanline = b"\x00" + b"\x00\x00\x00\x00" * width
    raw = scanline * height
    idat = _chunk(b"IDAT", zlib.compress(raw))
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


_EMPTY_TILE = _make_empty_tile()


def _get_http_client(request) -> httpx.AsyncClient:
    """Return the app-managed httpx client from FastAPI state."""
    return request.app.state.http_client


def _mint_service_jwt() -> str:
    """Mint a short-lived JWT for internal TiTiler calls."""
    payload = {
        "sub": "service:share-proxy",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(
        payload, settings.openfarm_jwt_secret, algorithm=settings.jwt_algorithm
    )


# ── Agri RS helpers for share reports ─────────────────────────────────

_AGRI_INDEX_COLS: list[tuple[str, str, str]] = [
    # (index_type, column, sensor)
    ("NDVI", "ndvi_avg", "S2"),
    ("EVI", "evi_avg", "S2"),
    ("NDMI", "ndmi_avg", "S2"),
    ("NDRE", "ndre_avg", "S2"),
    ("MNDWI", "mndwi_avg", "S2"),
    ("CIRE", "cire_avg", "S2"),
    ("NDWI", "mndwi_avg", "S2"),  # MNDWI proxy when NDWI absent
    ("VV", "vv_avg", "S1"),
    ("VH", "vh_avg", "S1"),
]


def _quality_from_cloud(cloud: Any) -> float:
    if cloud is None:
        return 0.5
    try:
        return max(0.15, min(0.95, 1.0 - float(cloud) / 100.0))
    except (TypeError, ValueError):
        return 0.5


def _stat_point(
    *,
    field_id: uuid.UUID,
    d: Any,
    mean: float,
    quality: float,
    idx: str,
) -> ShareStatPoint:
    from datetime import date as date_cls

    if isinstance(d, date_cls):
        date_obj = d
    elif hasattr(d, "date") and callable(d.date):
        date_obj = d.date()
    else:
        date_obj = date_cls.fromisoformat(str(d)[:10])
    date_val = date_obj.isoformat()
    # Stable synthetic id so charts/keys stay consistent across refreshes
    sid = uuid.uuid5(uuid.NAMESPACE_URL, f"agri-share:{field_id}:{idx}:{date_val}")
    return ShareStatPoint(
        id=sid,
        field_id=field_id,
        date=date_obj,
        mean=mean,
        median=mean,
        min=mean,
        max=mean,
        p10=mean,
        p90=mean,
        stddev=None,
        quality_score=quality,
        created_at=datetime.now(timezone.utc),
    )


async def _load_agri_share_series(
    db: AsyncSession,
    field_id: uuid.UUID,
    land_id: str,
) -> tuple[list[str], dict[str, list[ShareStatPoint]], list[ShareStatPoint], bool]:
    """Load S1/S2 means from agri.parcel_scene_products into share chart series.

    Returns (available_index_types, stats_by_type, all_stats, heatmap_available).
    """
    try:
        rows = (
            (
                await db.execute(
                    text(
                        """
                    SELECT date, sensor,
                           ndvi_avg, evi_avg, ndmi_avg, ndre_avg,
                           mndwi_avg, cire_avg, vv_avg, vh_avg,
                           parcel_cloud_cover_pct, cloud_cover,
                           CASE
                             WHEN pixel_data->>'format' = 'lonlat_v1'
                              AND jsonb_typeof(pixel_data->'pixels') = 'array'
                             THEN jsonb_array_length(pixel_data->'pixels')
                             ELSE 0
                           END AS lonlat_pixels
                    FROM agri.parcel_scene_products
                    WHERE land_id = :land_id
                    ORDER BY date DESC
                    LIMIT 500
                    """
                    ),
                    {"land_id": land_id},
                )
            )
            .mappings()
            .all()
        )
    except Exception as exc:  # noqa: BLE001 — agri schema may be absent
        logger.warning("agri share series load failed land_id=%s: %s", land_id, exc)
        return [], {}, [], False

    stats_by_type: dict[str, list[ShareStatPoint]] = {}
    heatmap_available = False
    for r in rows:
        if int(r.get("lonlat_pixels") or 0) > 0:
            heatmap_available = True
        cloud = r.get("parcel_cloud_cover_pct")
        if cloud is None:
            cloud = r.get("cloud_cover")
        q = _quality_from_cloud(cloud)
        sensor = r.get("sensor")
        for idx, col, want_sensor in _AGRI_INDEX_COLS:
            if sensor != want_sensor:
                continue
            raw = r.get(col)
            if raw is None:
                continue
            try:
                mean = float(raw)
            except (TypeError, ValueError):
                continue
            pt = _stat_point(
                field_id=field_id, d=r["date"], mean=mean, quality=q, idx=idx
            )
            stats_by_type.setdefault(idx, []).append(pt)

    # Cap each series (newest first already); keep enough for seasonal charts
    for idx, pts in list(stats_by_type.items()):
        stats_by_type[idx] = pts[:120]

    available = sorted(stats_by_type.keys())
    all_stats: list[ShareStatPoint] = []
    for pts in stats_by_type.values():
        all_stats.extend(pts)
    all_stats.sort(key=lambda s: s.date, reverse=True)
    return available, stats_by_type, all_stats, heatmap_available


async def _resolve_share_link(db: AsyncSession, token: str) -> tuple[ShareLink, Field]:
    result = await db.execute(select(ShareLink).where(ShareLink.token == token))
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")
    now = datetime.now(timezone.utc)
    if link.revoked_at is not None:
        raise HTTPException(status_code=410, detail="Share link has been revoked")
    if link.expires_at is not None and link.expires_at < now:
        raise HTTPException(status_code=410, detail="Share link has expired")
    field = await db.get(Field, link.field_id)
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")
    return link, field


@router.get("/fields/{field_id}/share", response_model=list[ShareOut])
async def list_share_links(
    field_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ShareLink).where(
            ShareLink.field_id == field_id,
            org_scope(ShareLink.org_id, ctx),
            ShareLink.revoked_at.is_(None),
        )
    )
    # Filter out expired links
    now = datetime.now(timezone.utc)
    return [
        link
        for link in result.scalars().all()
        if link.expires_at is None or link.expires_at > now
    ]


@router.post(
    "/fields/{field_id}/share",
    response_model=ShareOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_share_link(
    field_id: uuid.UUID,
    body: ShareCreate,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    field = await db.get(Field, field_id)
    if not field or field.deleted_at is not None or not org_matches(field.org_id, ctx.org_id):
        raise HTTPException(status_code=404, detail="Field not found")

    expires_at = None
    if body.expires_in_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)

    link = ShareLink(
        org_id=ctx.org_id,
        field_id=field_id,
        token=secrets.token_urlsafe(32),
        scope=body.scope,
        expires_at=expires_at,
        created_by=ctx.user.id,
    )
    db.add(link)

    # Audit event: report_shared (per PRD Section 5.1)
    db.add(
        AuditEvent(
            org_id=ctx.org_id,
            user_id=ctx.user.id,
            event_type="report_shared",
            metadata_json={
                "field_id": str(field_id),
                "scope": body.scope,
                "token": link.token,
            },
        )
    )
    await db.flush()
    logger.info("report_shared", field_id=str(field_id), scope=body.scope)
    return link


@router.delete(
    "/fields/{field_id}/share/{token}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_share_link(
    field_id: uuid.UUID,
    token: str,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(ShareLink).where(
            ShareLink.field_id == field_id,
            org_scope(ShareLink.org_id, ctx),
            ShareLink.token == token,
        )
    )
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")

    link.revoked_at = datetime.now(timezone.utc)
    link.revoked_by = ctx.user.id
    await db.flush()


@router.get("/share/{token}", response_model=ShareReportOut)
async def get_shared_report(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Public endpoint - no auth required."""
    result = await db.execute(select(ShareLink).where(ShareLink.token == token))
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")

    now = datetime.now(timezone.utc)
    if link.revoked_at is not None:
        raise HTTPException(status_code=410, detail="Share link has been revoked")
    if link.expires_at is not None and link.expires_at < now:
        raise HTTPException(status_code=410, detail="Share link has expired")

    # Load field
    field = await db.get(Field, link.field_id)
    if not field:
        raise HTTPException(status_code=404, detail="Field not found")

    from geoalchemy2.shape import to_shape
    from shapely.geometry import mapping

    field_data = {
        "id": str(field.id),
        "name": field.name,
        "area_ha": float(field.area_ha) if field.area_ha else None,
        "crop_type": field.crop_type,
        "geom": mapping(to_shape(field.geom)) if field.geom else None,
    }

    land_id = parse_agri_land_id(field.tags_json)
    agri_land_id: str | None = land_id
    agri_heatmap_available = False
    rs_source: str | None = None

    # Available index types (distinct layer_type values) — classic OpenFarm COG path
    types_result = await db.execute(
        select(RasterLayer.layer_type)
        .where(RasterLayer.field_id == field.id)
        .distinct()
    )
    available_index_types: list[str] = sorted(t for (t,) in types_result.all())

    # Latest layer per index type
    layers_by_type: dict[str, RasterLayer] = {}
    for idx_type in available_index_types:
        lyr_result = await db.execute(
            select(RasterLayer)
            .where(
                RasterLayer.field_id == field.id,
                RasterLayer.layer_type == idx_type,
            )
            .order_by(RasterLayer.date.desc())
            .limit(1)
        )
        lyr = lyr_result.scalar_one_or_none()
        if lyr:
            layers_by_type[idx_type] = lyr

    # Backward-compat: latest NDVI layer
    latest_layer = layers_by_type.get("NDVI")

    # Stats (last 12 for each available index, merged & grouped)
    all_stats: list[Any] = []
    stats_by_type: dict[str, list[Any]] = {}
    for idx_type in available_index_types:
        stats_result = await db.execute(
            select(FieldStat)
            .join(RasterLayer, FieldStat.layer_id == RasterLayer.id)
            .where(
                FieldStat.field_id == field.id,
                RasterLayer.layer_type == idx_type,
            )
            .order_by(FieldStat.date.desc())
            .limit(12)
        )
        idx_stats = list(stats_result.scalars().all())
        stats_by_type[idx_type] = idx_stats
        all_stats.extend(idx_stats)
    # Sort descending by date
    all_stats.sort(key=lambda s: s.date, reverse=True)

    # Agri-tagged fields: RS truth lives in parcel_scene_products (lonlat_v1).
    # Prefer agri series for charts so outsiders see the same curves as the
    # authenticated Agri 遥感时序 panel (classic FieldStat may be thin/empty).
    if land_id:
        (
            agri_types,
            agri_stats_by_type,
            agri_all_stats,
            agri_heatmap_available,
        ) = await _load_agri_share_series(db, field.id, land_id)
        if agri_types:
            available_index_types = agri_types
            stats_by_type = agri_stats_by_type
            all_stats = agri_all_stats
            rs_source = "agri" if not layers_by_type else "mixed"
        elif layers_by_type:
            rs_source = "classic"
        else:
            rs_source = "agri"
    elif layers_by_type:
        rs_source = "classic"

    # Recent alerts (last 10)
    alerts_result = await db.execute(
        select(Alert)
        .where(Alert.field_id == field.id)
        .order_by(Alert.created_at.desc())
        .limit(10)
    )
    alerts = alerts_result.scalars().all()

    # Recent scouting (last 10)
    scouting_result = await db.execute(
        select(ScoutingObservation)
        .where(ScoutingObservation.field_id == field.id)
        .order_by(ScoutingObservation.created_at.desc())
        .limit(10)
    )
    scouting_entries = scouting_result.scalars().all()

    # Convert scouting WKBElement geom_point → GeoJSON dict (same as _obs_to_out in scouting router)
    scouting_out = []
    for obs in scouting_entries:
        geom_json = None
        if obs.geom_point is not None:
            try:
                geom_json = mapping(to_shape(obs.geom_point))
            except Exception:
                pass
        scouting_out.append(
            ScoutingOut(
                id=obs.id,
                field_id=obs.field_id,
                alert_id=obs.alert_id,
                geom_point=geom_json,
                title=obs.title,
                note=obs.note,
                tags=obs.tags_json,
                photo_uri=obs.photo_uri,
                weather_snapshot=obs.weather_snapshot,
                created_by=obs.created_by,
                created_at=obs.created_at,
            )
        )

    # Weather summary (last 30 days)
    weather_summary = None
    weather_result = await db.execute(
        select(WeatherDaily)
        .where(
            WeatherDaily.field_id == field.id,
            WeatherDaily.date >= (now.date() - timedelta(days=30)),
        )
        .order_by(WeatherDaily.date.desc())
    )
    weather_rows = weather_result.scalars().all()
    if weather_rows:
        latest_w = weather_rows[0]
        total_precip = sum(float(r.precipitation_sum or 0) for r in weather_rows)
        total_et0 = sum(float(r.et0_fao_mm or 0) for r in weather_rows)
        temps = [
            float(r.temperature_2m_mean)
            for r in weather_rows
            if r.temperature_2m_mean is not None
        ]
        weather_summary = {
            "period_days": len(weather_rows),
            "total_precip_mm": round(total_precip, 1),
            "total_et0_mm": round(total_et0, 1),
            "avg_temp_c": round(sum(temps) / len(temps), 1) if temps else None,
            "water_balance_mm": round(total_precip - total_et0, 1),
        }
        if latest_w.drought_index is not None:
            weather_summary["drought_index"] = round(float(latest_w.drought_index), 2)
        if latest_w.soil_moisture_0_1cm is not None:
            weather_summary["soil_moisture_top"] = round(
                float(latest_w.soil_moisture_0_1cm), 3
            )

    # Weather daily rows for chart overlay (last 90 days)
    weather_data_out: list[dict[str, Any]] = []
    wd_result = await db.execute(
        select(WeatherDaily)
        .where(
            WeatherDaily.field_id == field.id,
            WeatherDaily.date >= (now.date() - timedelta(days=90)),
        )
        .order_by(WeatherDaily.date.asc())
    )
    for wd in wd_result.scalars().all():
        weather_data_out.append(
            {
                "date": wd.date.isoformat(),
                "precipitation_sum": float(wd.precipitation_sum)
                if wd.precipitation_sum is not None
                else None,
                "et0_fao_mm": float(wd.et0_fao_mm)
                if wd.et0_fao_mm is not None
                else None,
                "temperature_2m_mean": float(wd.temperature_2m_mean)
                if wd.temperature_2m_mean is not None
                else None,
                "temperature_2m_min": float(wd.temperature_2m_min)
                if wd.temperature_2m_min is not None
                else None,
                "temperature_2m_max": float(wd.temperature_2m_max)
                if wd.temperature_2m_max is not None
                else None,
            }
        )

    # Soil summary
    soil_summary_out: dict[str, Any] | None = None
    soil_result = await db.execute(
        select(SoilFieldSummary).where(SoilFieldSummary.field_id == field.id)
    )
    soil_sum = soil_result.scalar_one_or_none()
    if soil_sum:
        soil_summary_out = {
            "dominant_texture": soil_sum.dominant_texture,
            "avg_ph": float(soil_sum.avg_ph) if soil_sum.avg_ph is not None else None,
            "total_soc_stock_t_ha": float(soil_sum.total_soc_stock_t_ha)
            if soil_sum.total_soc_stock_t_ha is not None
            else None,
            "rootzone_awc_mm": float(soil_sum.rootzone_awc_mm)
            if soil_sum.rootzone_awc_mm is not None
            else None,
            "drainage_class": soil_sum.drainage_class,
            "compaction_risk": float(soil_sum.compaction_risk)
            if soil_sum.compaction_risk is not None
            else None,
            "waterlogging_risk": float(soil_sum.waterlogging_risk)
            if soil_sum.waterlogging_risk is not None
            else None,
            "acidification_risk": float(soil_sum.acidification_risk)
            if soil_sum.acidification_risk is not None
            else None,
            "leaching_risk": float(soil_sum.leaching_risk)
            if soil_sum.leaching_risk is not None
            else None,
        }

    return ShareReportOut(
        field=field_data,
        latest_layer=latest_layer,
        layers_by_type=layers_by_type,
        available_index_types=available_index_types,
        stats=all_stats,
        stats_by_type=stats_by_type,
        alerts=alerts,
        scouting=scouting_out,
        weather_summary=weather_summary,
        weather_data=weather_data_out,
        soil_summary=soil_summary_out,
        rs_source=rs_source,
        agri_land_id=agri_land_id,
        agri_heatmap_available=agri_heatmap_available,
    )


@router.get("/share/{token}/tiles/{z}/{x}/{y}.png")
async def proxy_share_tile(
    request: Request,
    token: str,
    z: int,
    x: int,
    y: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    index_type: str = "NDVI",
):
    """Public tile proxy - validates share token, proxies to TiTiler."""
    result = await db.execute(select(ShareLink).where(ShareLink.token == token))
    link = result.scalar_one_or_none()

    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")

    now = datetime.now(timezone.utc)
    if link.revoked_at is not None or (
        link.expires_at is not None and link.expires_at < now
    ):
        raise HTTPException(status_code=410, detail="Share link expired or revoked")

    # Per-index colormap and rescale settings
    _INDEX_TILE_PARAMS: dict[str, tuple[str, str]] = {
        "NDVI": ("rdylgn", "-0.2,0.9"),
        "EVI": ("rdylgn", "-0.2,0.8"),
        "SAVI": ("rdylgn", "-0.2,0.8"),
        "NDWI": ("rdbu", "-0.5,0.5"),
    }
    idx_upper = index_type.upper()
    colormap, rescale = _INDEX_TILE_PARAMS.get(idx_upper, ("rdylgn", "-0.2,0.9"))

    # Find latest layer for the requested index type
    layer_result = await db.execute(
        select(RasterLayer)
        .where(
            RasterLayer.field_id == link.field_id, RasterLayer.layer_type == idx_upper
        )
        .order_by(RasterLayer.date.desc())
        .limit(1)
    )
    layer = layer_result.scalar_one_or_none()
    if not layer:
        raise HTTPException(status_code=404, detail=f"No {idx_upper} layer available")

    # Build TiTiler URL (internal)
    cog_uri = layer.cog_uri
    if cog_uri.startswith("s3://"):
        cog_uri = cog_uri.replace("s3://", "/vsis3/", 1)
    elif cog_uri.startswith("oss://"):
        cog_uri = cog_uri.replace("oss://", "/vsis3/", 1)
    encoded_url = quote(cog_uri, safe="")
    tiler_url = (
        f"{settings.titiler_internal_url}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
        f"?url={encoded_url}"
        f"&colormap_name={colormap}&rescale={rescale}"
    )

    # Forward request with a service JWT
    service_token = _mint_service_jwt()
    client = _get_http_client(request)
    try:
        resp = await client.get(
            tiler_url,
            headers={"Authorization": f"Bearer {service_token}"},
        )
        if resp.status_code != 200:
            # Return transparent tile for missing/out-of-bounds tiles
            return Response(
                content=_EMPTY_TILE,
                media_type="image/png",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                },
            )
        return Response(
            content=resp.content,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Tile server unavailable")


@router.get("/share/{token}/agri-pixels")
async def get_share_agri_pixels(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    index_type: str = "NDVI",
    scene_date: str | None = None,
):
    """Public agri lonlat pixels for share map overlay (no auth; gated by token)."""
    from app.routers.agri import _pixels_from_db_lonlat

    _link, field = await _resolve_share_link(db, token)
    land_id = parse_agri_land_id(field.tags_json)
    if not land_id:
        raise HTTPException(status_code=404, detail="Field is not agri-tagged")

    idx_upper = (index_type or "NDVI").upper()
    sensor = "S1" if idx_upper in ("VV", "VH") else "S2"

    params: dict[str, Any] = {"land_id": land_id, "sensor": sensor}
    date_clause = ""
    if scene_date:
        date_clause = "AND date = CAST(:scene_date AS date)"
        params["scene_date"] = scene_date

    row = (
        (
            await db.execute(
                text(
                    f"""
                SELECT date, sensor, ndvi_avg, evi_avg, ndmi_avg, ndre_avg,
                       mndwi_avg, cire_avg, vv_avg, vh_avg, pixel_data
                FROM agri.parcel_scene_products
                WHERE land_id = :land_id AND sensor = :sensor
                  {date_clause}
                  AND pixel_data->>'format' = 'lonlat_v1'
                  AND jsonb_typeof(pixel_data->'pixels') = 'array'
                  AND jsonb_array_length(pixel_data->'pixels') > 0
                ORDER BY date DESC
                LIMIT 1
                """
                ),
                params,
            )
        )
        .mappings()
        .first()
    )

    if not row:
        # Fallback: latest scene even without requiring pixels (means only)
        row = (
            (
                await db.execute(
                    text(
                        f"""
                    SELECT date, sensor, ndvi_avg, evi_avg, ndmi_avg, ndre_avg,
                           mndwi_avg, cire_avg, vv_avg, vh_avg, pixel_data
                    FROM agri.parcel_scene_products
                    WHERE land_id = :land_id AND sensor = :sensor
                      {date_clause}
                    ORDER BY date DESC
                    LIMIT 1
                    """
                    ),
                    params,
                )
            )
            .mappings()
            .first()
        )

    if not row:
        raise HTTPException(status_code=404, detail="No agri scene available")

    pixels = _pixels_from_db_lonlat(row.get("pixel_data")) or []
    mean_map = {
        "NDVI": row.get("ndvi_avg"),
        "EVI": row.get("evi_avg"),
        "NDMI": row.get("ndmi_avg"),
        "NDRE": row.get("ndre_avg"),
        "MNDWI": row.get("mndwi_avg"),
        "CIRE": row.get("cire_avg"),
        "NDWI": row.get("mndwi_avg"),
        "VV": row.get("vv_avg"),
        "VH": row.get("vh_avg"),
    }
    mean_raw = mean_map.get(idx_upper)
    mean_val = float(mean_raw) if mean_raw is not None else None
    d = row["date"]
    date_str = d.isoformat() if hasattr(d, "isoformat") else str(d)

    return {
        "land_id": land_id,
        "date": date_str,
        "sensor": row["sensor"],
        "index_type": idx_upper,
        "mean": mean_val,
        "pixel_count": len(pixels),
        "pixels_lonlat": pixels,
        "pixels_source": "db_lonlat" if pixels else None,
    }
