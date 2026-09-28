"""Agri-first APIs: 地块 (land_parcels) 与 S1/S2 scenes.

Primary product surface for agric-satellite-analysis. Scene data is keyed
directly by ``agric_satellite.land_parcels.land_id``; no parcel mapping is
performed here.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.agri_classify import parse_s1_relative_orbit
from app.core.database import get_db
from app.core.storage import ObjectTooLargeError, get_parcel_product_storage
from app.middleware.auth import OrgContext, require_roles
from app.schemas.agri import (
    AgriStatsOut,
    AgriTableCount,
    HarvestDetectOut,
    LandParcelOut,
    LandScenesSummaryOut,
    NdviDayGradeShareItem,
    NdviDayGradeSharesOut,
    SceneProductOut,
    SensorSceneSummary,
)
from app.schemas.common import PaginatedResponse

router = APIRouter(prefix="/agri", tags=["agri"])

logger = logging.getLogger(__name__)

_reader = require_roles("owner", "admin", "member", "viewer")
_SCENE_PIXEL_PAGE_LIMIT = 50
_SCENE_PIXEL_RESPONSE_LIMIT = 50_000
_SCENE_PIXEL_RESPONSE_LIMIT_BYTES = 16 * 1024 * 1024
_SCENE_PIXEL_STORAGE_LIMIT_BYTES = 12 * 1024 * 1024
_SCENE_OSS_JSON_LIMIT_BYTES = 8 * 1024 * 1024
_SCENE_OSS_PAGE_LIMIT_BYTES = 16 * 1024 * 1024

# Averages + meta; pixel_data excluded unless include_pixels=1
_SCENE_COLS = """
    land_id, tile_id, date, sensor, scene_id, land_name,
    cloud_cover, cloud_cover_over_30, parcel_cloud_cover_pct,
    json_oss_key, pixel_data_url, pixel_count,
    rgb_url, large_rgb_url, rgb_oss_key,
    ndvi_avg, ndvi_min, ndvi_max,
    evi_avg, evi_min, evi_max,
    ndmi_avg, ndmi_min, ndmi_max,
    ndre_avg, ndre_min, ndre_max,
    mndwi_avg, mndwi_min, mndwi_max,
    cire_avg, cire_min, cire_max,
    vv_avg, vv_min, vv_max,
    vh_avg, vh_min, vh_max,
    generated_at_shanghai, ingested_at,
    pixel_data->>'source' AS source,
    pixel_data->>'stac_item_id' AS stac_item_id,
    pixel_data->>'algorithm_version' AS algorithm_version,
    pixel_data->'analysis_grid' AS analysis_grid,
    pixel_data->'radiometric_calibration' AS radiometric_calibration,
    pixel_data->'quality_metrics' AS quality_metrics,
    pixel_data->>'decloud_quality' AS decloud_quality,
    NULLIF(pixel_data->>'decloud_score', '')::float AS decloud_score,
    pixel_data->'decloud_reasons' AS decloud_reasons,
    pixel_data->>'parcel_cloud_source' AS parcel_cloud_source,
    NULLIF(pixel_data->>'relative_orbit', '')::int AS relative_orbit
"""


class _ScenePixelLimitExceeded(ValueError):
    """单次像元响应预算超限，供异步路由转换为明确的 413 响应。"""


class _SceneOssReadBudget:
    """限制一页历史预览/像元回退最多读取的OSS JSON总字节数。"""

    def __init__(self) -> None:
        self.remaining_bytes = _SCENE_OSS_PAGE_LIMIT_BYTES

    def get_bytes(self, storage: Any, key: str) -> bytes:
        # 每个旧对象仍有单独上限；整页共享剩余预算，避免50条历史行各读满8 MiB。
        read_limit = min(_SCENE_OSS_JSON_LIMIT_BYTES, self.remaining_bytes)
        if read_limit <= 0:
            raise _ScenePixelLimitExceeded("OSS scene page byte budget exhausted")
        try:
            raw = storage.get_bytes(key, max_bytes=read_limit)
        except ObjectTooLargeError as exc:
            # 存储层只会多读1字节确认超限；保守按本次上限扣减，严格保持整页有界。
            self.remaining_bytes = max(0, self.remaining_bytes - read_limit)
            if read_limit < _SCENE_OSS_JSON_LIMIT_BYTES:
                raise _ScenePixelLimitExceeded(
                    "OSS scene page byte budget exceeded"
                ) from exc
            raise
        self.remaining_bytes = max(0, self.remaining_bytes - len(raw))
        return raw


def _row_to_dict(row: Any) -> dict[str, Any]:
    from decimal import Decimal

    d = dict(row._mapping)
    for k, v in list(d.items()):
        if isinstance(v, Decimal):
            v = float(v)
        if isinstance(v, float) and not math.isfinite(v):
            # PostgreSQL浮点特殊值不是合法JSON数值；对外统一按缺测返回NULL。
            d[k] = None
        elif k in (
            "boundary_geojson",
            "source_properties",
            "pixel_data",
            "analysis_grid",
            "radiometric_calibration",
            "quality_metrics",
            "grid_json",
            "decloud_reasons",
        ) and isinstance(v, str):
            try:
                # Python默认会接受非标准NaN/Infinity常量；映射成JSON null，避免响应序列化失败。
                d[k] = json.loads(v, parse_constant=lambda _value: None)
            except json.JSONDecodeError:
                pass
        else:
            d[k] = v
    _enrich_scene_product(d)
    return d


def _enrich_scene_product(d: dict[str, Any]) -> None:
    """Normalize decloud_reasons and fill relative_orbit from scene_id."""
    reasons = d.get("decloud_reasons")
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except json.JSONDecodeError:
            reasons = [reasons] if reasons else []
    if reasons is None:
        d["decloud_reasons"] = None
    elif isinstance(reasons, list):
        d["decloud_reasons"] = [str(x) for x in reasons if x is not None]
    else:
        d["decloud_reasons"] = [str(reasons)]

    rel = d.get("relative_orbit")
    if rel is None and d.get("sensor") == "S1":
        parsed = parse_s1_relative_orbit(d.get("scene_id"))
        if parsed is not None:
            d["relative_orbit"] = parsed


def _normalize_lonlat_pixels(raw_pixels: Any) -> list[dict[str, Any]]:
    """筛掉缺坐标或坐标不可转数值的像元，避免坏数据进入地图栅格化。"""
    if not isinstance(raw_pixels, list):
        return []
    out: list[dict[str, Any]] = []
    for p in raw_pixels:
        if not isinstance(p, dict):
            continue
        lon = p.get("lon")
        lat = p.get("lat")
        if lon is None or lat is None or isinstance(lon, bool) or isinstance(lat, bool):
            continue
        try:
            lon_value = float(lon)
            lat_value = float(lat)
        except (TypeError, ValueError, OverflowError):
            continue
        # JSONB/历史OSS可能混有字符串、NaN或越界坐标；统一成有限WGS84数值，
        # 否则无效点会污染地图范围，甚至产生前端无法解析的JSON响应。
        if (
            not math.isfinite(lon_value)
            or not math.isfinite(lat_value)
            or not -180 <= lon_value <= 180
            or not -90 <= lat_value <= 90
        ):
            continue
        # 像元契约只暴露数值指标；剔除NaN、嵌套对象和无关大字符串，保证JSON有效且响应紧凑。
        normalized = {"lon": lon_value, "lat": lat_value}
        for key, value in p.items():
            if key in {"lon", "lat"}:
                continue
            if isinstance(value, bool):
                if key == "clear":
                    normalized[key] = int(value)
                continue
            if not isinstance(value, (int, float, str)):
                continue
            try:
                numeric_value = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(numeric_value):
                normalized[key] = numeric_value
        out.append(normalized)
    return out


def _normalize_oss_pixels(raw_pixels: Any) -> list[dict[str, Any]]:
    """Keep only dict lon/lat pixel objects from OSS JSON."""
    return _normalize_lonlat_pixels(raw_pixels)


def _pixels_from_db_lonlat(pixel_data: Any) -> list[dict[str, Any]] | None:
    """只接受带 lonlat_v1 标记的数据库像元，避免误把旧行列网格当经纬度。"""
    if not isinstance(pixel_data, dict):
        return None
    if pixel_data.get("format") != "lonlat_v1":
        return None
    pixels = _normalize_lonlat_pixels(pixel_data.get("pixels"))
    return pixels or None


def _oss_str_url(value: Any) -> str | None:
    """Accept non-empty string URLs from OSS JSON; reject other types."""
    if isinstance(value, str):
        s = value.strip()
        if s:
            return s
    return None


def _extract_oss_media_urls(obj: dict[str, Any]) -> dict[str, str | None]:
    """Pull preview image URLs from an OSS parcel product JSON object."""
    rgb_url = _oss_str_url(obj.get("rgb_url"))
    large_rgb_url = _oss_str_url(obj.get("large_rgb_url"))
    heatmap_url = _oss_str_url(obj.get("heatmap_url"))
    s2_heatmap_url = _oss_str_url(obj.get("s2_heatmap_url"))
    return {
        "rgb_url": rgb_url,
        "large_rgb_url": large_rgb_url,
        # 为旧版客户端保留单热图字段；优先专用热图，缺失时回退到S2图层。
        "heatmap_url": heatmap_url or s2_heatmap_url,
        "s2_heatmap_url": s2_heatmap_url,
    }


def _load_oss_scene_json(
    json_oss_key: str | None,
    *,
    read_budget: _SceneOssReadBudget | None = None,
) -> dict[str, Any] | None:
    """有界读取历史 OSS 产品 JSON；缺失、损坏或超限时回退数据库可用数据。"""
    if not json_oss_key or not isinstance(json_oss_key, str):
        return None
    key = json_oss_key.strip()
    if not key:
        return None
    try:
        storage = get_parcel_product_storage()
        # 旧格式把像元与预览元数据合在同一对象；在OSS流层设上限，超大对象不会先完整下载再丢弃。
        raw = (
            read_budget.get_bytes(storage, key)
            if read_budget is not None
            else storage.get_bytes(key, max_bytes=_SCENE_OSS_JSON_LIMIT_BYTES)
        )
        obj = json.loads(raw)
    except _ScenePixelLimitExceeded:
        raise
    except ObjectTooLargeError as exc:
        logger.warning("OSS scene JSON exceeds per-object read limit for %s: %s", key, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — fallback to DB grid is intentional
        logger.warning("OSS scene JSON fetch failed for %s: %s", key, exc)
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _load_oss_scene_media(
    json_oss_key: str | None,
    *,
    read_budget: _SceneOssReadBudget | None = None,
) -> dict[str, str | None] | None:
    """从旧版 OSS 产品 JSON 提取预览 URL。

    历史格式把预览元数据和像元放在同一 JSON 中，因此读取时仍会下载整个对象；
    该同步 I/O 只能在线程池调用，后续可用独立元数据列/小对象消除这次重复下载。
    """
    try:
        obj = _load_oss_scene_json(json_oss_key, read_budget=read_budget)
    except _ScenePixelLimitExceeded as exc:
        # 预览媒体可选；预算不足时保留可用像元结果，不再为旧图片字段扩读OSS。
        logger.warning("OSS scene media skipped by page byte budget for %s: %s", json_oss_key, exc)
        return None
    if obj is None:
        return None
    return _extract_oss_media_urls(obj)


def _load_oss_scene_pixels(
    json_oss_key: str | None,
    *,
    max_pixels: int | None = None,
    read_budget: _SceneOssReadBudget | None = None,
) -> dict[str, Any] | None:
    """读取旧版 OSS 像元并保留预览 URL；像元缺失时只返回媒体信息供兼容回退。"""
    obj = _load_oss_scene_json(json_oss_key, read_budget=read_budget)
    if obj is None:
        return None
    raw_pixels = obj.get("pixels")
    declared_count = obj.get("pixel_count")
    try:
        declared_count = int(declared_count or 0)
    except (TypeError, ValueError):
        declared_count = 0
    # OSS 回退先检查声明数量和数组长度，避免规范化明显超限的数据副本。
    if max_pixels is not None and (
        declared_count > max_pixels
        or (isinstance(raw_pixels, list) and len(raw_pixels) > max_pixels)
    ):
        raise _ScenePixelLimitExceeded("OSS scene pixel count exceeds the request budget")
    pixels = _normalize_oss_pixels(raw_pixels)
    if max_pixels is not None and len(pixels) > max_pixels:
        raise _ScenePixelLimitExceeded("OSS scene pixel count exceeds the request budget")
    media = _extract_oss_media_urls(obj)
    if not pixels:
        logger.warning(
            "OSS JSON %s has no lon/lat pixels; returning media URLs only", json_oss_key
        )
        return {"pixels_lonlat": None, "pixel_count": None, **media}
    return {
        "pixels_lonlat": pixels,
        "pixel_count": obj.get("pixel_count") or len(pixels),
        **media,
    }


def _clear_scene_media_urls(d: dict[str, Any]) -> None:
    d["rgb_url"] = None
    d["large_rgb_url"] = None
    d["heatmap_url"] = None
    d["s2_heatmap_url"] = None


def _sign_preview_url(key: str | None, fallback: str | None = None) -> str | None:
    """为私有桶预览生成24小时可读 URL；稳定 OSS key 优先，旧链接只作兼容回退。"""
    if isinstance(key, str) and key.strip():
        try:
            # 图片 URL 会下发到浏览器，短期签名可减少链接泄露后的长期访问窗口；
            # 页面重新请求场景时会用稳定 key 重新签名，不依赖客户端永久缓存 URL。
            return get_parcel_product_storage().presigned_get(
                key.strip(), expires=timedelta(hours=24)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("rgb_presign_failed key=%s err=%s", key[:120], exc)
    if isinstance(fallback, str) and fallback.strip():
        # 历史对象内已签名的链接没有稳定 key 可重签，只能按旧格式原样回传。
        return fallback.strip()
    return None


def _attach_scene_media_urls(d: dict[str, Any], media: dict[str, Any] | None) -> None:
    """补全预览地址：数据库稳定列优先，旧 OSS JSON 只补历史缺失字段。"""
    db_rgb = d.get("rgb_url")
    db_large = d.get("large_rgb_url")
    db_key = d.get("rgb_oss_key")
    if media:
        d["rgb_url"] = db_rgb or media.get("rgb_url")
        d["large_rgb_url"] = db_large or media.get("large_rgb_url")
        if not db_key:
            db_key = media.get("rgb_oss_key")
            if db_key:
                d["rgb_oss_key"] = db_key
        d["heatmap_url"] = media.get("heatmap_url")
        d["s2_heatmap_url"] = media.get("s2_heatmap_url")
    else:
        # 保留数据库中的RGB字段；只清理必须从旧OSS JSON补齐的热图字段。
        if not db_rgb and not db_large:
            _clear_scene_media_urls(d)
        else:
            d["heatmap_url"] = None
            d["s2_heatmap_url"] = None
    # 私有桶优先用稳定 key 重签，避免数据库存放的短期URL过期或直接访问失败。
    signed = _sign_preview_url(
        d.get("rgb_oss_key") if isinstance(d.get("rgb_oss_key"), str) else db_key,
        d.get("rgb_url"),
    )
    if signed:
        d["rgb_url"] = signed


def _build_scene_product_items(rows: list[Any], *, include_pixels: bool) -> list[SceneProductOut]:
    """构造场景响应；像元模式会访问 OSS，必须由异步路由放入线程池执行。"""
    items: list[SceneProductOut] = []
    total_pixels = 0
    oss_read_budget = _SceneOssReadBudget() if include_pixels else None
    for row in rows:
        data = _row_to_dict(row)
        if not include_pixels:
            for field in (
                "pixel_data",
                "pixels_lonlat",
                "rgb_url",
                "large_rgb_url",
                "heatmap_url",
                "s2_heatmap_url",
                "pixels_source",
            ):
                data.pop(field, None)
            items.append(SceneProductOut.model_validate(data))
            continue

        db_lonlat = _pixels_from_db_lonlat(data.get("pixel_data"))
        if db_lonlat:
            if total_pixels + len(db_lonlat) > _SCENE_PIXEL_RESPONSE_LIMIT:
                raise _ScenePixelLimitExceeded(
                    "database scene pixels exceed the request budget"
                )
            total_pixels += len(db_lonlat)
            data["pixels_lonlat"] = db_lonlat
            data["pixels_source"] = "db_lonlat"
            # DB 经纬度像元是权威数据；清掉网格副本，同时保留 OSS 预览图。
            data["pixel_data"] = None
            if not data.get("pixel_count"):
                data["pixel_count"] = len(db_lonlat)
            # 新产品的稳定预览对象键已单独入库，直接重签即可，避免为取RGB再次下载含像元的整份OSS JSON。
            media = (
                None
                if data.get("rgb_oss_key")
                else _load_oss_scene_media(
                    data.get("json_oss_key"), read_budget=oss_read_budget
                )
            )
            _attach_scene_media_urls(
                data, media
            )
        else:
            oss_payload = _load_oss_scene_pixels(
                data.get("json_oss_key"),
                max_pixels=_SCENE_PIXEL_RESPONSE_LIMIT - total_pixels,
                read_budget=oss_read_budget,
            )
            if oss_payload and oss_payload.get("pixels_lonlat"):
                total_pixels += len(oss_payload["pixels_lonlat"])
                data["pixels_lonlat"] = oss_payload["pixels_lonlat"]
                data["pixels_source"] = "oss"
                # OSS 经纬度像元优先于旧网格，避免前端把低精度回退数据当主结果。
                data["pixel_data"] = None
                if oss_payload.get("pixel_count") and not data.get("pixel_count"):
                    data["pixel_count"] = oss_payload["pixel_count"]
                _attach_scene_media_urls(data, oss_payload)
            else:
                data["pixels_lonlat"] = None
                _attach_scene_media_urls(data, oss_payload)
                if data.get("pixel_data"):
                    data["pixels_source"] = "db_grid"
                else:
                    data["pixels_source"] = None
        items.append(SceneProductOut.model_validate(data))
    return items


def _serialize_scene_page(
    items: list[SceneProductOut], *, total: int, limit: int, offset: int
) -> bytes:
    """在线程池中完成大像元响应的 Pydantic 包装与 JSON 序列化。"""
    payload = PaginatedResponse(
        items=items, total=int(total), limit=limit, offset=offset
    )
    serialized = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(serialized) > _SCENE_PIXEL_RESPONSE_LIMIT_BYTES:
        raise _ScenePixelLimitExceeded("serialized scene pixel page exceeds byte budget")
    return serialized


async def _agri_ready(db: AsyncSession) -> None:
    q = await db.execute(
        text(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'agric_satellite' LIMIT 1"
        )
    )
    if q.scalar() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agric_satellite schema not installed; run: make agri-seed (see scripts/agri_seed/README.md)",
        )


@router.get("/stats", response_model=AgriStatsOut)
@router.get("/admin/import-status", response_model=AgriStatsOut)
async def agri_stats(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Read-only row counts for agri tables (import health check)."""
    await _agri_ready(db)
    tables = [
        "land_parcels",
        "parcel_scene_products",
        "ingest_runs",
        "ingest_batch_stats",
        "ingested_oss_objects",
    ]
    counts: list[AgriTableCount] = []
    for t in tables:
        r = await db.execute(text(f"SELECT count(*) FROM agric_satellite.{t}"))  # noqa: S608
        counts.append(AgriTableCount(table=t, count=int(r.scalar() or 0)))
    return AgriStatsOut(
        tables=counts,
        note=(
            "parcel_scene_products seed dump is a 1000-row sample; "
            "other tables are full export. Primary APIs are under /v1/agri/*; "
            "/v1/lands is the canonical parcel API in this fork."
        ),
    )


@router.get("/lands/{land_id}", response_model=LandParcelOut)
async def get_land(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _agri_ready(db)
    row = (
        await db.execute(
            text("SELECT * FROM agric_satellite.land_parcels WHERE land_id = :land_id"),
            {"land_id": land_id},
        )
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Land parcel not found")
    return LandParcelOut.model_validate(_row_to_dict(row))


@router.get(
    "/lands/{land_id}/scenes", response_model=PaginatedResponse[SceneProductOut]
)
async def list_land_scenes(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    sensor: str | None = Query(None, pattern="^(S1|S2)$"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    include_pixels: int = Query(
        0,
        ge=0,
        le=1,
        description=(
            "If 1, prefer DB lonlat_v1 pixels (pixels_source=db_lonlat); "
            "else try OSS via json_oss_key; else legacy grid pixel_data (db_grid). "
            "Pixel pages are capped at 50 scenes and a bounded pixel/JSON budget."
        ),
    ),
    order: Literal["asc", "desc"] = Query(
        "asc",
        description=(
            "Sort by date (then sensor, scene_id). For timeseries UI prefer "
            "order=desc&limit=500 then reverse client-side, or "
            "order=asc&offset=max(0,total-limit)."
        ),
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """返回地块 S1/S2 时序；默认轻量统计，按需返回有界像元详情。

    Timeseries UI should load the newest window first: ``order=desc&limit=500``
    then reverse items ascending for charts, or
    ``order=asc&offset=max(0, total-limit)``. Single-day heatmap fetches
    (``from``/``to`` same day) can keep the default ``asc``.
    """
    await _agri_ready(db)
    exists = (
        await db.execute(
            text("SELECT 1 FROM agric_satellite.land_parcels WHERE land_id = :land_id"),
            {"land_id": land_id},
        )
    ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="Land parcel not found")

    where = ["land_id = :land_id"]
    params: dict[str, Any] = {
        "land_id": land_id,
        "limit": limit,
        "offset": offset,
    }
    if sensor:
        where.append("sensor = :sensor")
        params["sensor"] = sensor
    if date_from:
        where.append("date >= :date_from")
        params["date_from"] = date_from
    if date_to:
        where.append("date <= :date_to")
        params["date_to"] = date_to
    wh = " AND ".join(where)
    order_sql = "DESC" if order == "desc" else "ASC"

    total = (
        await db.execute(
            text(
                f"SELECT count(*) FROM agric_satellite.parcel_scene_products WHERE {wh}"
            ),
            params,
        )
    ).scalar() or 0

    page_limit = min(limit, _SCENE_PIXEL_PAGE_LIMIT) if include_pixels else limit
    params["limit"] = page_limit
    if include_pixels:
        # 先只让数据库汇总当前页的像元数和JSONB体积；超过预算时不把像元正文
        # 传到API进程，避免一个大地块或过宽日期窗口占满内存并拖慢响应。
        payload_budget = await db.execute(
            text(
                f"""
                SELECT
                    COALESCE(SUM(GREATEST(
                        COALESCE(pixel_count, 0),
                        CASE
                            WHEN pixel_data->>'format' = 'lonlat_v1'
                             AND jsonb_typeof(pixel_data->'pixels') = 'array'
                            THEN jsonb_array_length(pixel_data->'pixels')
                            ELSE 0
                        END
                    )), 0)::bigint AS pixel_count,
                    COALESCE(SUM(pg_column_size(pixel_data)), 0)::bigint AS payload_bytes
                FROM (
                    SELECT pixel_count, pixel_data
                    FROM agric_satellite.parcel_scene_products
                    WHERE {wh}
                    ORDER BY date {order_sql}, sensor {order_sql}, scene_id {order_sql}
                    LIMIT :limit OFFSET :offset
                ) AS page
                """
            ),
            params,
        )
        budget = payload_budget.mappings().one()
        if (
            int(budget["pixel_count"] or 0) > _SCENE_PIXEL_RESPONSE_LIMIT
            or int(budget["payload_bytes"] or 0) > _SCENE_PIXEL_STORAGE_LIMIT_BYTES
        ):
            raise HTTPException(
                status_code=413,
                detail=(
                    "像元详情超过单次响应上限，请缩小日期范围或分批请求"
                ),
            )

    cols = _SCENE_COLS + (", pixel_data" if include_pixels else "")
    rows = (
        await db.execute(
            text(
                f"""
                SELECT {cols}
                FROM agric_satellite.parcel_scene_products
                WHERE {wh}
                ORDER BY date {order_sql}, sensor {order_sql}, scene_id {order_sql}
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
    ).fetchall()
    # OSS SDK、JSON 解码和大响应序列化都是同步工作；像元模式统一放入线程池，
    # 避免请求高峰时阻塞 FastAPI 事件循环。普通时序仍走轻量同步组装。
    if include_pixels:
        try:
            items = await run_in_threadpool(
                _build_scene_product_items, rows, include_pixels=True
            )
            serialized = await run_in_threadpool(
                _serialize_scene_page,
                items,
                total=int(total),
                limit=page_limit,
                offset=offset,
            )
        except _ScenePixelLimitExceeded as exc:
            raise HTTPException(
                status_code=413,
                detail="像元详情超过单次响应上限，请缩小日期范围或分批请求",
            ) from exc
        return Response(content=serialized, media_type="application/json")
    else:
        items = _build_scene_product_items(rows, include_pixels=False)
    return {
        "items": [
            i.model_dump(
                exclude_none=False,
                exclude={
                    "pixel_data",
                    "pixels_lonlat",
                    "rgb_url",
                    "large_rgb_url",
                    "heatmap_url",
                    "s2_heatmap_url",
                    "pixels_source",
                },
            )
            for i in items
        ],
        "total": int(total),
        "limit": page_limit,
        "offset": offset,
    }


@router.get("/lands/{land_id}/scenes/summary", response_model=LandScenesSummaryOut)
async def land_scenes_summary(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await _agri_ready(db)
    exists = (
        await db.execute(
            text("SELECT 1 FROM agric_satellite.land_parcels WHERE land_id = :land_id"),
            {"land_id": land_id},
        )
    ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="Land parcel not found")

    rows = (
        await db.execute(
            text(
                """
                SELECT sensor,
                       count(*)::int AS count,
                       min(date) AS date_min,
                       max(date) AS date_max
                FROM agric_satellite.parcel_scene_products
                WHERE land_id = :land_id
                GROUP BY sensor
                ORDER BY sensor
                """
            ),
            {"land_id": land_id},
        )
    ).fetchall()

    sensors: list[SensorSceneSummary] = []
    total = 0
    for r in rows:
        d = _row_to_dict(r)
        total += int(d["count"])
        latest = (
            await db.execute(
                text(
                    """
                    SELECT date, ndvi_avg, evi_avg, vv_avg, vh_avg
                    FROM agric_satellite.parcel_scene_products
                    WHERE land_id = :land_id AND sensor = :sensor
                    ORDER BY date DESC
                    LIMIT 1
                    """
                ),
                {"land_id": land_id, "sensor": d["sensor"]},
            )
        ).fetchone()
        latest_d = _row_to_dict(latest) if latest else {}
        sensors.append(
            SensorSceneSummary(
                sensor=d["sensor"],
                count=int(d["count"]),
                date_min=d.get("date_min"),
                date_max=d.get("date_max"),
                latest_date=latest_d.get("date"),
                latest_ndvi_avg=latest_d.get("ndvi_avg"),
                latest_evi_avg=latest_d.get("evi_avg"),
                latest_vv_avg=latest_d.get("vv_avg"),
                latest_vh_avg=latest_d.get("vh_avg"),
            )
        )

    return LandScenesSummaryOut(land_id=land_id, total=total, sensors=sensors)


@router.get(
    "/lands/{land_id}/harvest-detect",
    response_model=HarvestDetectOut,
)
@router.post(
    "/lands/{land_id}/harvest-detect",
    response_model=HarvestDetectOut,
)
async def harvest_detect_for_land(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    crops: str | None = Query(
        None, description="Comma-separated crop keys (metadata; max 2)"
    ),
    label: str | None = Query(None),
    body: dict[str, Any] | None = Body(None),
):
    """Observation-only harvest day from official NDVI (soft-fail → uncertain)."""
    try:
        await _agri_ready(db)
        exists = (
            await db.execute(
                text(
                    "SELECT 1 FROM agric_satellite.land_parcels WHERE land_id = :land_id"
                ),
                {"land_id": land_id},
            )
        ).scalar()
        if not exists:
            raise HTTPException(status_code=404, detail="Land parcel not found")

        raw_window: dict[str, Any] = {}
        if isinstance(body, dict):
            raw_window.update(body.get("window") or body)
        if start_date:
            raw_window.setdefault("start_date", start_date.isoformat())
        if end_date:
            raw_window.setdefault("end_date", end_date.isoformat())
        if crops:
            raw_window.setdefault(
                "crops", [c.strip() for c in crops.split(",") if c.strip()]
            )
        if label:
            raw_window.setdefault("label", label)

        from app.core.growing_seasons import normalize_growing_seasons

        windows = normalize_growing_seasons(
            [raw_window] if raw_window else [],
            validate_crop_limits=bool(
                raw_window.get("crops") or raw_window.get("crop")
            ),
        )
        window = (
            windows[0]
            if windows
            else {
                k: raw_window[k]
                for k in ("start_date", "end_date", "crops", "label")
                if raw_window.get(k) is not None
            }
        )

        from app.core.date_utils import _as_date
        from app.core.harvest_detect import detect_harvest

        params: dict[str, Any] = {"land_id": land_id}
        where = ["land_id = :land_id", "sensor = 'S2'", "ndvi_avg IS NOT NULL"]
        # asyncpg needs datetime.date, not ISO strings from normalize_growing_seasons
        date_from = _as_date(window.get("start_date"))
        if date_from is not None:
            where.append("date >= :date_from")
            params["date_from"] = date_from
        date_to = _as_date(window.get("end_date"))
        if date_to is not None:
            where.append("date <= :date_to")
            params["date_to"] = date_to
        wh = " AND ".join(where)
        rows = (
            await db.execute(
                text(
                    f"""
                    SELECT {_SCENE_COLS}
                    FROM agric_satellite.parcel_scene_products
                    WHERE {wh}
                    ORDER BY date ASC, scene_id ASC
                    LIMIT 500
                    """
                ),
                params,
            )
        ).fetchall()

        from app.core.agri_classify import is_official_optical_product

        points: list[dict[str, Any]] = []
        for r in rows:
            d = _row_to_dict(r)
            official = is_official_optical_product(
                source=d.get("source"),
                scene_id=d.get("scene_id"),
                decloud_quality=d.get("decloud_quality"),
                parcel_cloud_cover_pct=d.get("parcel_cloud_cover_pct"),
                cloud_cover=d.get("cloud_cover"),
                cloud_cover_over_30=d.get("cloud_cover_over_30"),
                parcel_cloud_source=d.get("parcel_cloud_source"),
            )
            points.append(
                {
                    "date": str(d.get("date"))[:10],
                    "ndvi_avg": d.get("ndvi_avg"),
                    "scene_id": d.get("scene_id"),
                    "official": official,
                    "decloud_quality": d.get("decloud_quality"),
                    "source": d.get("source"),
                }
            )

        result = detect_harvest(points, window=window)
        return HarvestDetectOut(
            land_id=land_id,
            status=result.status,
            harvest_date=result.harvest_date,
            confidence=result.confidence,
            scene_id=result.scene_id,
            evidence=result.evidence or {},
            alternates=result.alternates or [],
            window=result.window or window,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("harvest_detect_failed land_id=%s", land_id)
        return HarvestDetectOut(
            land_id=land_id,
            status="uncertain",
            confidence="low",
            evidence={"reason": "soft_fail", "error": str(exc)[:200]},
            window={},
        )


@router.get(
    "/lands/{land_id}/ndvi-day-grade-shares",
    response_model=NdviDayGradeSharesOut,
)
async def list_ndvi_day_grade_shares(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=500),
):
    """Server-side 图一 grade shares per S2 date from DB lonlat_v1 pixels.

    Returns aggregated counts/pct/n/mean only (no raw pixels). Prefer clear=1
    pixels when present. Multiple scenes on one day → prefer official optical.
    """
    await _agri_ready(db)
    exists = (
        await db.execute(
            text("SELECT 1 FROM agric_satellite.land_parcels WHERE land_id = :land_id"),
            {"land_id": land_id},
        )
    ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="Land parcel not found")

    from app.core.agri_classify import is_official_optical_product
    from app.core.ndvi_day_grade import (
        NDVI_DAY_GRADE_RULE_ZH,
        compute_pixel_ndvi_day_grade_shares,
    )

    where = [
        "land_id = :land_id",
        "sensor = 'S2'",
        "pixel_data->>'format' = 'lonlat_v1'",
        "jsonb_typeof(pixel_data->'pixels') = 'array'",
        "jsonb_array_length(pixel_data->'pixels') > 0",
    ]
    params: dict[str, Any] = {"land_id": land_id, "limit": limit}
    if date_from is not None:
        where.append("date >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        where.append("date <= :date_to")
        params["date_to"] = date_to
    wh = " AND ".join(where)

    rows = (
        await db.execute(
            text(
                f"""
                SELECT date, scene_id, pixel_data, ndvi_avg,
                       pixel_data->>'source' AS source,
                       pixel_data->>'decloud_quality' AS decloud_quality,
                       parcel_cloud_cover_pct, cloud_cover, cloud_cover_over_30,
                       pixel_data->>'parcel_cloud_source' AS parcel_cloud_source
                FROM agric_satellite.parcel_scene_products
                WHERE {wh}
                ORDER BY date ASC, scene_id ASC
                LIMIT :limit
                """
            ),
            params,
        )
    ).fetchall()

    by_date: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        d = _row_to_dict(r)
        day = str(d.get("date"))[:10]
        by_date.setdefault(day, []).append(d)

    items: list[NdviDayGradeShareItem] = []
    for day in sorted(by_date.keys()):
        group = by_date[day]
        official = [
            s
            for s in group
            if is_official_optical_product(
                source=s.get("source"),
                scene_id=s.get("scene_id"),
                decloud_quality=s.get("decloud_quality"),
                parcel_cloud_cover_pct=s.get("parcel_cloud_cover_pct"),
                cloud_cover=s.get("cloud_cover"),
                cloud_cover_over_30=s.get("cloud_cover_over_30"),
                parcel_cloud_source=s.get("parcel_cloud_source"),
            )
        ]
        candidates = official or group
        chosen = None
        share = None
        for s in candidates:
            pixels = _pixels_from_db_lonlat(s.get("pixel_data"))
            if not pixels:
                continue
            share = compute_pixel_ndvi_day_grade_shares(pixels)
            if share:
                chosen = s
                break
        if not share or chosen is None:
            continue
        items.append(
            NdviDayGradeShareItem(
                date=day,
                counts=share["counts"],
                pct=share["pct"],
                n=share["n"],
                mean=share.get("mean"),
                scene_id=chosen.get("scene_id"),
            )
        )

    return NdviDayGradeSharesOut(
        land_id=land_id,
        items=items,
        rule_zh=NDVI_DAY_GRADE_RULE_ZH,
    )


# China overview (全国态势) — country/province/city/county stats
from app.routers.agri_overview import router as overview_router  # noqa: E402

router.include_router(overview_router)
