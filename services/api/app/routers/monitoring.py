"""Monitoring router - raster layers, field stats, tile URLs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.quality_metrics import (
    PARCEL_VALID_FRACTION_V1,
    extract_quality_score_method,
)
from app.core.config import settings
from app.core.database import get_db
from app.middleware.auth import OrgContext, get_org_context, org_scope
from app.models.tables import FieldStat, RasterLayer
from app.schemas.common import PaginatedResponse
from app.schemas.monitoring import FieldStatOut, RasterLayerOut
from app.tasks.indices import INDEX_REGISTRY

router = APIRouter()


def _cog_uri_to_s3_path(cog_uri: str) -> str:
    """Convert s3:// or oss:// URIs to /vsis3/ paths for TiTiler/GDAL.

    Aliyun OSS is reached via the S3-compatible API when TiTiler's
    ``AWS_S3_ENDPOINT`` points at the OSS endpoint.
    """
    if cog_uri.startswith("s3://"):
        return cog_uri.replace("s3://", "/vsis3/", 1)
    if cog_uri.startswith("oss://"):
        return cog_uri.replace("oss://", "/vsis3/", 1)
    return cog_uri


def _layer_to_out(layer: RasterLayer) -> RasterLayerOut:
    """Convert ORM RasterLayer to RasterLayerOut with tile_url.

    Uses the index registry for per-index colormap and rescale values.
    """
    s3_path = _cog_uri_to_s3_path(layer.cog_uri)
    from urllib.parse import quote

    encoded_url = quote(s3_path, safe="")

    # Look up index-specific colormap/rescale from registry
    index_key = (layer.layer_type or "NDVI").lower()
    idx = INDEX_REGISTRY.get(index_key)
    if idx:
        colormap = idx.colormap
        rescale = f"{idx.rescale[0]},{idx.rescale[1]}"
    else:
        # Fallback to NDVI defaults
        colormap = "rdylgn"
        rescale = "-0.2,0.9"

    tile_url = (
        f"{settings.titiler_public_url}/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
        f"?url={encoded_url}"
        f"&colormap_name={colormap}&rescale={rescale}"
    )
    return RasterLayerOut(
        id=layer.id,
        land_id=layer.land_id,
        layer_type=layer.layer_type,
        satellite=layer.satellite,
        date=layer.date,
        cog_uri=layer.cog_uri,
        tile_url=tile_url,
        min=float(layer.min) if layer.min is not None else None,
        max=float(layer.max) if layer.max is not None else None,
        params_json=layer.params_json,
        provenance_json=layer.provenance_json,
        created_at=layer.created_at,
    )


@router.get(
    "/lands/{land_id}/layers", response_model=PaginatedResponse[RasterLayerOut]
)
async def list_layers(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: str = Query("NDVI"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    base = select(RasterLayer).where(
        RasterLayer.land_id == land_id,
        org_scope(None, ctx),
        RasterLayer.layer_type == type,
    )
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    result = await db.execute(
        base.order_by(RasterLayer.date.desc()).limit(limit).offset(offset)
    )
    layers = result.scalars().all()
    return PaginatedResponse(
        items=[_layer_to_out(layer) for layer in layers],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/lands/{land_id}/stats", response_model=PaginatedResponse[FieldStatOut])
async def list_stats(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
    type: str = Query("NDVI"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    base = (
        select(FieldStat, RasterLayer.provenance_json)
        .join(RasterLayer, FieldStat.layer_id == RasterLayer.id)
        .where(
            FieldStat.land_id == land_id,
            org_scope(None, ctx),
            RasterLayer.layer_type == type,
        )
    )
    total = (
        await db.execute(select(func.count()).select_from(base.subquery()))
    ).scalar() or 0
    result = await db.execute(
        base.order_by(FieldStat.date.asc()).limit(limit).offset(offset)
    )
    items = []
    for stat, provenance in result.all():
        method = extract_quality_score_method(provenance)
        # 历史质量分没有算法口径时不返回数值，避免前端把旧矩形比例显示成地块覆盖率。
        trusted_score = (
            float(stat.quality_score)
            if stat.quality_score is not None
            and method == PARCEL_VALID_FRACTION_V1
            else None
        )
        item = FieldStatOut.model_validate(stat).model_copy(
            update={
                "quality_score": trusted_score,
                "quality_score_method": method or "unknown",
            }
        )
        items.append(item)
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/lands/{land_id}/layers/types", response_model=list[str])
async def list_layer_types(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(get_org_context)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Return the distinct index types available for a field."""
    result = await db.execute(
        select(distinct(RasterLayer.layer_type)).where(
            RasterLayer.land_id == land_id, org_scope(None, ctx)
        )
    )
    return sorted(result.scalars().all())
