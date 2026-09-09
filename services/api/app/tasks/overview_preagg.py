"""Celery: daily pre-aggregation of China overview stats into agri.overview_stats_daily."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import text

from app.core.crops import normalize_crop_key
from app.core.database_sync import SyncSession
from app.worker import celery_app

logger = structlog.get_logger()

# Mirror router DDL (keep in sync with agri_overview._ENSURE_CACHE_SQL).
_ENSURE_CACHE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agri.overview_stats_daily (
    as_of_date date NOT NULL,
    level text NOT NULL,
    region_code text NOT NULL DEFAULT '',
    region_name text,
    parent_code text,
    metric_json jsonb NOT NULL,
    window_from date NOT NULL,
    window_to date NOT NULL,
    crop text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (as_of_date, level, region_code, window_from, window_to, crop)
)
"""
_ENSURE_CACHE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS overview_stats_daily_lookup_idx
    ON agri.overview_stats_daily (level, region_code, window_from, window_to, crop, updated_at DESC)
"""


def _ensure_table(session) -> None:
    session.execute(text(_ENSURE_CACHE_TABLE_SQL))
    session.execute(text(_ENSURE_CACHE_INDEX_SQL))
    session.commit()


def _upsert_row(
    session,
    *,
    as_of: date,
    level: str,
    region_code: str,
    region_name: str | None,
    parent_code: str | None,
    metric: dict[str, Any],
    window_from: date,
    window_to: date,
    crop: str,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO agri.overview_stats_daily (
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
            """
        ),
        {
            "as_of": as_of,
            "level": level,
            "region_code": region_code or "",
            "region_name": region_name,
            "parent_code": parent_code,
            "metric": json.dumps(metric, ensure_ascii=False, default=str),
            "window_from": window_from,
            "window_to": window_to,
            "crop": crop or "",
        },
    )


async def _compute_and_store_async(
    *,
    level: str,
    code: str | None,
    name: str | None,
    from_d: date,
    to_d: date,
    crop: str | None,
    parent_code: str | None = None,
) -> dict[str, Any]:
    """Run live stats via async session (same path as HTTP) and return payload."""
    from app.routers.agri_overview import _compute_live_stats

    # get_db is an async generator dependency — open session directly.
    from app.core.database import async_session

    allow_pixels = level != "country"
    async with async_session() as db:
        out = await _compute_live_stats(
            db,
            level=level,  # type: ignore[arg-type]
            code=code,
            name=name,
            from_d=from_d,
            to_d=to_d,
            crop=crop,
            allow_pixels=allow_pixels,
        )
        payload = out.model_dump(mode="json")
        # Sync upsert with SyncSession for DDL/DML simplicity
        session = SyncSession()
        try:
            _ensure_table(session)
            region = payload.get("region") or {}
            crop_key = normalize_crop_key(crop) if crop else ""
            _upsert_row(
                session,
                as_of=date.today(),
                level=level,
                region_code=str(code or region.get("code") or ""),
                region_name=region.get("name"),
                parent_code=parent_code,
                metric=payload,
                window_from=from_d,
                window_to=to_d,
                crop=crop_key or "",
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return {
            "level": level,
            "code": code,
            "name": region.get("name"),
            "parcel_count": (payload.get("totals") or {}).get("parcel_count"),
        }


def _list_provinces_sync() -> list[tuple[str | None, str]]:
    session = SyncSession()
    try:
        rows = session.execute(
            text(
                """
                SELECT province_code AS code, province_name AS name, count(*) AS n
                FROM agri.land_parcels
                WHERE province_name IS NOT NULL
                GROUP BY province_code, province_name
                ORDER BY n DESC, name
                """
            )
        ).fetchall()
        return [(str(r.code) if r.code else None, str(r.name)) for r in rows]
    finally:
        session.close()


@celery_app.task(name="app.tasks.overview_preagg.refresh_overview_stats")
def refresh_overview_stats(
    window_days: int = 60,
    crop: str | None = None,
) -> dict[str, Any]:
    """Pre-aggregate country + all provinces for default window (crop=null).

    Country uses scene_avg (no pixel blobs). Provinces may use pixel drought.
    Scheduled daily ~02:30 Asia/Shanghai (18:30 UTC) via beat.
    """
    to_d = date.today()
    from_d = to_d - timedelta(days=int(window_days))
    logger.info(
        "overview_preagg_start",
        from_d=from_d.isoformat(),
        to_d=to_d.isoformat(),
        crop=crop,
    )

    session = SyncSession()
    try:
        _ensure_table(session)
    finally:
        session.close()

    results: list[dict[str, Any]] = []

    async def _run_all() -> None:
        # Country first (scene_avg)
        results.append(
            await _compute_and_store_async(
                level="country",
                code=None,
                name=None,
                from_d=from_d,
                to_d=to_d,
                crop=crop,
            )
        )
        provinces = _list_provinces_sync()
        for code, name in provinces:
            try:
                results.append(
                    await _compute_and_store_async(
                        level="province",
                        code=code,
                        name=name,
                        from_d=from_d,
                        to_d=to_d,
                        crop=crop,
                        parent_code=None,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "overview_preagg_province_failed",
                    code=code,
                    name=name,
                    error=str(exc),
                )

    asyncio.run(_run_all())
    logger.info("overview_preagg_done", regions=len(results))
    return {
        "ok": True,
        "as_of": date.today().isoformat(),
        "window_from": from_d.isoformat(),
        "window_to": to_d.isoformat(),
        "regions": len(results),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sample": results[:3],
    }
