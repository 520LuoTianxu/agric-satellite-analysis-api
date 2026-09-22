"""下载机 Beat 的内部发现接口。Postgres 只在 API 机访问。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agric_satellite_analysis_common.scheduled_land_filter import (
    MAX_SCHEDULE_LAND_AREA_MU,
    scheduled_land_sql,
)
from agric_satellite_analysis_common.storage import get_storage
from app.core.config import settings
from app.core.database import get_db
from app.core.logging import logger
from app.middleware.internal_auth import InternalAuth
from app.models.tables import Job
from app.services.beat_schedule import (
    STAGGER_SECONDS,
    WEEKLY_INDEX_KEYS,
    index_task_name,
    weekly_date_window,
)
from app.services.overview_daily import business_today, finalize_daily, prepare_daily

router = APIRouter(prefix="/internal/schedule", tags=["internal-schedule"])

OVERVIEW_UPSERT_BATCH_SIZE = 200


class WeeklyIndexJobOut(BaseModel):
    land_id: str
    job_id: str
    task_name: str
    countdown: int = 0
    date_from: str
    date_to: str
    index: str


class WeeklyIndexPrepareOut(BaseModel):
    items: list[WeeklyIndexJobOut] = Field(default_factory=list)
    lands_checked: int = 0
    lands_dispatched: int = 0
    jobs_created: int = 0


class WeatherLandItemOut(BaseModel):
    land_id: str
    date_from: str | None = None
    date_to: str | None = None


class WeatherLandsOut(BaseModel):
    land_ids: list[str] = Field(default_factory=list)
    items: list[WeatherLandItemOut] = Field(default_factory=list)
    batch_size: int = 50


class OverviewRefreshFinalizeIn(BaseModel):
    """下载机只提交 OSS 结果对象的 key，避免把区域结果塞进 HTTP body。"""

    result_oss_key: str = Field(min_length=1, max_length=512)


def _overview_input_key(window_from: date, window_to: date) -> str:
    return (
        "overview/preagg/input/"
        f"{window_from.isoformat()}_{window_to.isoformat()}/{uuid.uuid4().hex}.json"
    )


@router.post("/daily-satellite")
async def prepare_daily_satellite(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    as_of: date | None = Query(None),
) -> dict[str, Any]:
    """每日发现全部有效地块，按5×5公里窗口派发增量S1/S2下载。"""
    from fastapi import HTTPException

    day = as_of or business_today()
    if day > business_today():
        raise HTTPException(400, "统计日期不能晚于北京时间当天")
    return await prepare_daily(db, day)


@router.post("/daily-satellite/{run_id}/finalize")
async def finalize_daily_satellite(
    run_id: uuid.UUID,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """核对下载和MQ入库状态，统一保存每日干旱、洪涝和弱长势快照。"""
    return await finalize_daily(db, run_id)


@router.post("/weekly-index", response_model=WeeklyIndexPrepareOut)
async def prepare_weekly_index(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """列出过期的统一地块，在 API 建 Job，返回给下载机执行。"""
    today = date.today()
    # 规范光学结果存于 parcel_scene_products；不再以历史 raster_layers
    # 作为另一套地块/遥感主链路。
    rows = (
        await db.execute(
            text(
                f"""
                SELECT l.land_id, max(p.date)::date AS latest_date
                FROM agric_satellite.land_parcels AS l
                LEFT JOIN agric_satellite.parcel_scene_products AS p
                  ON p.land_id = l.land_id
                WHERE l.deleted_at IS NULL
                  AND {scheduled_land_sql("l")}
                GROUP BY l.land_id
                ORDER BY l.land_id
                """,
            ),
            {"max_schedule_area_mu": MAX_SCHEDULE_LAND_AREA_MU},
        )
    ).all()

    items: list[WeeklyIndexJobOut] = []
    lands_dispatched = 0
    for land_id, latest in rows:
        window = weekly_date_window(latest, today=today)
        if window is None:
            continue
        date_from, date_to = window
        countdown = lands_dispatched * STAGGER_SECONDS
        for idx_key in WEEKLY_INDEX_KEYS:
            job = Job(
                land_id=land_id,
                type=idx_key,
                status="pending",
                params_json={
                    "date_from": date_from.isoformat(),
                    "date_to": date_to.isoformat(),
                },
            )
            db.add(job)
            await db.flush()
            items.append(
                WeeklyIndexJobOut(
                    land_id=land_id,
                    job_id=str(job.id),
                    task_name=index_task_name(idx_key),
                    countdown=countdown,
                    date_from=date_from.isoformat(),
                    date_to=date_to.isoformat(),
                    index=idx_key,
                )
            )
        lands_dispatched += 1

    await db.commit()
    return WeeklyIndexPrepareOut(
        items=items,
        lands_checked=len(rows),
        lands_dispatched=lands_dispatched,
        jobs_created=len(items),
    )


@router.get("/weather-lands", response_model=WeatherLandsOut)
async def list_weather_lands(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """返回天气地块及其与遥感一致的增量日期窗口。"""
    rows = (
        await db.execute(
            text(
                f"""
                SELECT l.land_id, max(p.date)::date AS latest_date
                FROM agric_satellite.land_parcels AS l
                LEFT JOIN agric_satellite.parcel_scene_products AS p
                  ON p.land_id = l.land_id
                WHERE l.deleted_at IS NULL
                  AND {scheduled_land_sql("l")}
                GROUP BY l.land_id
                ORDER BY l.land_id
                """,
            ),
            {"max_schedule_area_mu": MAX_SCHEDULE_LAND_AREA_MU},
        )
    ).all()
    today = date.today()
    items: list[WeatherLandItemOut] = []
    for land_id, latest in rows:
        window = weekly_date_window(latest, today=today)
        items.append(
            WeatherLandItemOut(
                land_id=str(land_id),
                date_from=window[0].isoformat() if window else None,
                date_to=window[1].isoformat() if window else None,
            )
        )
    return WeatherLandsOut(
        # 保留旧字段，便于滚动升级期间的旧下载机继续工作。
        land_ids=[item.land_id for item in items],
        items=items,
        batch_size=settings.weather_batch_size,
    )


async def _export_overview_batch(
    db: AsyncSession,
    *,
    window_days: int,
    crop: str | None,
    land_batch_size: int,
    after_land_id: str | None,
) -> dict[str, Any]:
    """查询一小批总览原始观测并上传 OSS，HTTP 只返回对象引用。"""
    from app.core.agri_classify import (
        CLOUD_MAX_PCT,
        PHENOLOGY_MONTHS,
        WEAK_NDVI_LT,
        official_s2_sql,
    )
    from app.core.crops import get_crop_season, normalize_crop_key
    from app.routers.agri_overview import _month_in_clause

    window_to = date.today()
    window_from = window_to - timedelta(days=int(window_days))
    crop_key = normalize_crop_key(crop) if crop else None
    if crop_key:
        phenology_months = sorted(get_crop_season(crop_key).season_months)
    else:
        phenology_months = list(PHENOLOGY_MONTHS)

    land_params: dict[str, Any] = {
        "max_schedule_area_mu": MAX_SCHEDULE_LAND_AREA_MU,
        "limit": land_batch_size,
    }
    cursor_clause = ""
    if after_land_id:
        cursor_clause = " AND p.land_id > :after_land_id"
        land_params["after_land_id"] = after_land_id

    land_rows = (
        await db.execute(
            text(
                f"""
                SELECT p.land_id,
                       coalesce(p.land_area_mu, 0)::float AS area_mu,
                       p.province_code, p.province_name,
                       p.city_code, p.city_name,
                       p.county_code, p.county_name
                FROM agric_satellite.land_parcels p
                WHERE p.deleted_at IS NULL
                  AND {scheduled_land_sql("p")}
                  {cursor_clause}
                ORDER BY p.land_id
                LIMIT :limit
                """
            ),
            land_params,
        )
    ).fetchall()

    if not land_rows:
        return {
            "ok": True,
            "status": "done",
            "window_from": window_from.isoformat(),
            "window_to": window_to.isoformat(),
            "crop": crop_key,
            "land_count": 0,
            "next_cursor": after_land_id,
            "done": True,
            "oss_url": None,
            "oss_key": None,
        }

    lands: dict[str, dict[str, Any]] = {}
    for row in land_rows:
        land_id = str(row.land_id)
        lands[land_id] = {
            "land_id": land_id,
            "area_mu": float(row.area_mu or 0),
            "province_code": str(row.province_code) if row.province_code else None,
            "province_name": row.province_name,
            "city_code": str(row.city_code) if row.city_code else None,
            "city_name": row.city_name,
            "county_code": str(row.county_code) if row.county_code else None,
            "county_name": row.county_name,
            "s2": None,
            "s1": [],
            "weak": False,
        }

    land_params = {
        f"land_{index}": land_id for index, land_id in enumerate(lands)
    }
    land_sql = ", ".join(f":{key}" for key in land_params)
    query_params: dict[str, Any] = {
        "from_d": window_from,
        "to_d": window_to,
        "cloud_max": CLOUD_MAX_PCT,
        "weak_ndvi": WEAK_NDVI_LT,
        **land_params,
    }

    s2_rows = (
        await db.execute(
            text(
                f"""
                SELECT DISTINCT ON (s.land_id)
                       s.land_id, s.date, s.ndvi_avg, s.ndmi_avg, s.pixel_data
                FROM agric_satellite.parcel_scene_products s
                JOIN agric_satellite.land_parcels p ON p.land_id = s.land_id
                WHERE s.land_id IN ({land_sql})
                  AND s.sensor = 'S2'
                  AND s.date >= :from_d AND s.date <= :to_d
                  AND {official_s2_sql("s")}
                ORDER BY s.land_id, s.date DESC, s.scene_id
                """
            ),
            query_params,
        )
    ).fetchall()
    for row in s2_rows:
        land = lands.get(str(row.land_id))
        if land is not None:
            land["s2"] = {
                "date": row.date.isoformat()
                if hasattr(row.date, "isoformat")
                else str(row.date),
                "ndvi_avg": row.ndvi_avg,
                "ndmi_avg": row.ndmi_avg,
                "pixel_data": row.pixel_data,
            }

    s1_rows = (
        await db.execute(
            text(
                f"""
                SELECT s.land_id, s.date, s.vv_avg, s.vh_avg, s.scene_id,
                       s.pixel_data->>'relative_orbit' AS relative_orbit
                FROM agric_satellite.parcel_scene_products s
                JOIN agric_satellite.land_parcels p ON p.land_id = s.land_id
                WHERE s.land_id IN ({land_sql})
                  AND s.sensor = 'S1'
                  AND s.date >= :from_d AND s.date <= :to_d
                ORDER BY s.land_id, s.date, s.scene_id
                """
            ),
            query_params,
        )
    ).fetchall()
    for row in s1_rows:
        land = lands.get(str(row.land_id))
        if land is not None:
            land["s1"].append(
                {
                    "date": str(row.date)[:10],
                    "vv": row.vv_avg,
                    "vh": row.vh_avg,
                    "scene_id": str(row.scene_id) if row.scene_id else None,
                    "relative_orbit": row.relative_orbit,
                }
            )

    month_params = dict(query_params)
    month_clause = _month_in_clause(phenology_months, month_params, prefix="overview_pm")
    weak_rows = (
        await db.execute(
            text(
                f"""
                SELECT s.land_id
                FROM agric_satellite.parcel_scene_products s
                JOIN agric_satellite.land_parcels p ON p.land_id = s.land_id
                WHERE s.land_id IN ({land_sql})
                  AND s.sensor = 'S2'
                  AND s.date >= :from_d AND s.date <= :to_d
                  AND {month_clause}
                  AND s.ndvi_avg IS NOT NULL
                  AND {official_s2_sql("s")}
                GROUP BY s.land_id
                HAVING avg(s.ndvi_avg) < :weak_ndvi
                """
            ),
            month_params,
        )
    ).fetchall()
    for row in weak_rows:
        land = lands.get(str(row.land_id))
        if land is not None:
            land["weak"] = True

    payload = {
        "schema_version": 1,
        "window_from": window_from.isoformat(),
        "window_to": window_to.isoformat(),
        "crop": crop_key,
        "lands": list(lands.values()),
    }
    raw = json.dumps(
        jsonable_encoder(payload), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    sha256 = hashlib.sha256(raw).hexdigest()
    key = _overview_input_key(window_from, window_to)

    # 只读查询已经完成，先释放数据库事务，再等待 OSS 上传，避免网络耗时占住 PG 连接。
    await db.rollback()

    def _upload() -> str:
        storage = get_storage()
        storage.put_bytes(key, raw, content_type="application/json")
        return storage.presigned_get(key, expires=timedelta(hours=24))

    oss_url = await asyncio.to_thread(_upload)
    next_cursor = str(land_rows[-1].land_id)
    return {
        "ok": True,
        "status": "batch",
        "window_from": window_from.isoformat(),
        "window_to": window_to.isoformat(),
        "crop": crop_key,
        "land_count": len(lands),
        "next_cursor": next_cursor,
        "done": len(lands) < land_batch_size,
        "oss_key": key,
        "oss_url": oss_url,
        "bytes": len(raw),
        "sha256": sha256,
    }


@router.post("/overview-refresh")
async def refresh_overview(
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
    window_days: int = Query(60, ge=1, le=365),
    crop: str | None = Query(default=None),
    land_batch_size: int = Query(
        10,
        ge=1,
        le=1000,
        description="每批上传 OSS 的地块数量；响应只返回 OSS 引用。",
    ),
    after_land_id: str | None = Query(
        default=None,
        description="按 land_id 游标读取下一批，避免单次请求遍历全部地块。",
    ),
) -> dict[str, Any]:
    """准备一批总览原始数据；下载机从 OSS 拉取并在本地计算。"""
    return await _export_overview_batch(
        db,
        window_days=window_days,
        crop=crop,
        land_batch_size=land_batch_size,
        after_land_id=after_land_id,
    )


@router.post("/overview-refresh/finalize")
async def finalize_overview(
    body: OverviewRefreshFinalizeIn,
    _: InternalAuth,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """读取下载机上传的 OSS 结果包并写入总览缓存表。"""
    key = body.result_oss_key.strip().lstrip("/")
    if not key.startswith("overview/preagg/output/"):
        raise HTTPException(400, "result_oss_key 必须位于 overview/preagg/output/ 下")

    try:
        raw = await asyncio.to_thread(get_storage().get_bytes, key)
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.exception("overview_result_oss_read_failed", key=key)
        raise HTTPException(502, f"读取总览 OSS 结果失败: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise HTTPException(400, "总览 OSS 结果格式无效")

    from app.routers.agri_overview import ensure_overview_cache_table

    window_from = payload.get("window_from")
    window_to = payload.get("window_to")
    crop_key = str(payload.get("crop") or "")
    if not window_from or not window_to:
        raise HTTPException(400, "总览 OSS 结果缺少窗口日期")
    try:
        window_from_date = date.fromisoformat(str(window_from))
        window_to_date = date.fromisoformat(str(window_to))
    except ValueError as exc:
        raise HTTPException(400, "总览 OSS 结果窗口日期无效") from exc

    await ensure_overview_cache_table(db)
    as_of = date.today()
    upsert_rows: list[dict[str, Any]] = []
    from app.schemas.agri import OverviewStatsOut

    try:
        # 先完成全部结果校验和转换，再分批写库；任一结果非法时不会留下半批快照。
        for item in payload["results"]:
            metric = OverviewStatsOut.model_validate(item).model_dump(
                mode="json", by_alias=True
            )
            region = metric.get("region") or {}
            path = region.get("path") or []
            parent_code = path[-2].get("code") if len(path) >= 2 else None
            upsert_rows.append(
                {
                    "as_of": as_of,
                    "level": region.get("level") or "country",
                    "region_code": region.get("code") or "",
                    "region_name": region.get("name") or "全国",
                    "parent_code": parent_code,
                    "metric": json.dumps(metric, ensure_ascii=False),
                    "window_from": window_from_date,
                    "window_to": window_to_date,
                    "crop": crop_key,
                }
            )

        region_count = len(upsert_rows)
        upsert_sql = text(_UPSERT_OVERVIEW_SQL)
        for offset in range(0, region_count, OVERVIEW_UPSERT_BATCH_SIZE):
            batch = upsert_rows[offset : offset + OVERVIEW_UPSERT_BATCH_SIZE]
            await db.execute(
                upsert_sql,
                batch,
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return {
        "ok": True,
        "status": "success",
        "as_of": as_of.isoformat(),
        "window_from": window_from_date.isoformat(),
        "window_to": window_to_date.isoformat(),
        "crop": crop_key or None,
        "regions": region_count,
        "land_count": int(payload.get("land_count") or 0),
        "batch_count": int(payload.get("batch_count") or 0),
        "result_oss_key": key,
    }


# 总览缓存表 upsert：同一窗口同一作物只保留最新一行
_UPSERT_OVERVIEW_SQL = """
INSERT INTO agric_satellite.overview_stats_daily (
    as_of_date, level, region_code, region_name, parent_code,
    metric_json, window_from, window_to, crop, updated_at
) VALUES (
    :as_of, :level, :region_code, :region_name, :parent_code,
    CAST(:metric AS jsonb), :window_from, :window_to, :crop, now()
)
ON CONFLICT (as_of_date, level, region_code, window_from, window_to, crop)
DO UPDATE SET
    region_name = EXCLUDED.region_name,
    parent_code = EXCLUDED.parent_code,
    metric_json = EXCLUDED.metric_json,
    updated_at = now()
WHERE COALESCE(overview_stats_daily.metric_json->'filters'->>'snapshot', 'false') <> 'true'
"""


async def _refresh_overview_preagg(
    db: AsyncSession,
    *,
    window_days: int,
    crop: str | None,
    land_batch_size: int = 10,
) -> dict[str, Any]:
    """先算全国，再按省逐个计算并写入缓存。"""
    from app.core.crops import normalize_crop_key
    from app.routers.agri_overview import (
        _compute_live_stats,
        _resolve_region_label,
        ensure_overview_cache_table,
    )

    to_d = date.today()
    from_d = to_d - timedelta(days=int(window_days))
    crop_key = normalize_crop_key(crop) if crop else None
    await ensure_overview_cache_table(db)

    results: list[dict[str, Any]] = []

    async def _one(
        *,
        level: str,
        code: str | None,
        name: str | None,
        parent_code: str | None = None,
    ) -> None:
        # 全国只用景均值；省一级才允许像素干旱
        allow_pixels = level != "country"
        out = await _compute_live_stats(
            db,
            level=level,  # type: ignore[arg-type]
            code=code,
            name=name,
            from_d=from_d,
            to_d=to_d,
            crop=crop,
            allow_pixels=allow_pixels,
            parcel_batch_size=land_batch_size,
        )
        payload = out.model_dump(mode="json")
        resolved_code, resolved_name = await _resolve_region_label(
            db,
            level,
            code,
            name,  # type: ignore[arg-type]
        )
        await db.execute(
            text(_UPSERT_OVERVIEW_SQL),
            {
                "as_of": to_d,
                "level": level,
                "region_code": resolved_code or "",
                "region_name": resolved_name,
                "parent_code": parent_code,
                "metric": json.dumps(payload, ensure_ascii=False, default=str),
                "window_from": from_d,
                "window_to": to_d,
                "crop": crop_key or "",
            },
        )
        results.append(
            {
                "level": level,
                "code": resolved_code,
                "name": resolved_name,
                "parcel_count": (payload.get("totals") or {}).get("parcel_count"),
            }
        )

    await _one(level="country", code=None, name=None)
    provinces = (
        await db.execute(
            text(
                f"""
                SELECT p.province_code AS code, p.province_name AS name, count(*) AS n
                FROM agric_satellite.land_parcels AS p
                WHERE p.province_name IS NOT NULL
                  AND p.deleted_at IS NULL
                  AND {scheduled_land_sql("p")}
                GROUP BY p.province_code, p.province_name
                ORDER BY n DESC, name
                """,
            ),
            {"max_schedule_area_mu": MAX_SCHEDULE_LAND_AREA_MU},
        )
    ).fetchall()
    for row in provinces:
        try:
            await _one(
                level="province",
                code=str(row.code) if row.code else None,
                name=str(row.name),
                parent_code=None,
            )
        except Exception as exc:
            logger.warning(
                "overview_preagg_province_failed",
                code=str(row.code) if row.code else None,
                name=str(row.name),
                error=str(exc),
            )
            continue

    await db.commit()
    return {
        "ok": True,
        "as_of": to_d.isoformat(),
        "window_from": from_d.isoformat(),
        "window_to": to_d.isoformat(),
        "regions": len(results),
        "results": results,
    }
