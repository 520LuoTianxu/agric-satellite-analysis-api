"""冬季小麦秋季出苗的读取、落库和出队。

计算在 API 本机：场景结果入库后只写 outbox，后台再读
``parcel_scene_products`` 的晴空像元。下载机不连 API Postgres。

已出苗（emerged_flag）后冻结该地块该季的摘要和像元对象，后续同季场景不再重算。
不修改 NDVI 列，不回写 ``s1s2_parcel/json``。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import uuid
from datetime import date, datetime, timezone
from typing import Any

import numpy as np

from agric_satellite_analysis_common.agri_classify import (
    CLOUD_MAX_PCT,
    is_official_optical_product,
    official_s2_sql,
)
from agric_satellite_analysis_common.lonlat_pixels import (
    extract_pixels,
    sql_lonlat_format_in,
    sql_lonlat_nonempty,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.autumn_emergence import (
    BASELINE_STATISTIC,
    DISCLAIMER_ZH,
    METHOD_VERSION,
    MIN_CLEAR_FRACTION,
    NOISE_STATISTIC,
    RULE_ZH,
    STATUS_ZH,
    DateStack,
    EmergenceResult,
    analyze,
    autumn_season_key,
    default_season_key,
    is_winter_wheat,
    object_key,
    pixels_lonlat_for_date,
    season_bounds,
)
from app.core.logging import logger

SENSOR = "S2"
OUTBOX_POLL_SECONDS = 10
OUTBOX_CLAIM_LIMIT = 2
OUTBOX_LEASE_SECONDS = 600
OUTBOX_DEBOUNCE_SECONDS = 30

_LOAD_LAND = text(
    """
    SELECT land_id, crop_type
    FROM agric_satellite.land_parcels
    WHERE land_id = :land_id AND deleted_at IS NULL
    """
)

_LOAD_ROW = text(
    """
    SELECT land_id, season_key, status, emerged_flag, frozen,
           baseline_start, baseline_end, baseline_statistic,
           noise_floor, noise_statistic, as_of_date, first_emerged_date,
           method_version, n_clear, n_pixels, median_increment,
           share_above_noise, share_sustained, share_ndvi_ge_0_30,
           ndre_share_above_noise, summary, timeseries, pixel_oss_key,
           computed_at, frozen_at
    FROM agric_satellite.parcel_autumn_emergence
    WHERE land_id = :land_id AND season_key = :season_key
    """
)

_LOAD_SCENES = text(
    f"""
    SELECT p.date, p.scene_id, p.pixel_count, p.pixel_data,
           p.parcel_cloud_cover_pct, p.cloud_cover, p.cloud_cover_over_30,
           COALESCE(
               NULLIF(BTRIM(p.product_source), ''),
               NULLIF(BTRIM(p.pixel_data->>'source'), '')
           ) AS source,
           COALESCE(
               NULLIF(BTRIM(p.decloud_quality), ''),
               NULLIF(BTRIM(p.pixel_data->>'decloud_quality'), '')
           ) AS decloud_quality,
           COALESCE(
               NULLIF(BTRIM(p.parcel_cloud_source), ''),
               NULLIF(BTRIM(p.pixel_data->>'parcel_cloud_source'), '')
           ) AS parcel_cloud_source
    FROM agric_satellite.parcel_scene_products AS p
    WHERE p.land_id = :land_id
      AND p.sensor = 'S2'
      AND p.date >= :date_from AND p.date <= :date_to
      AND {sql_lonlat_format_in("p.pixel_data")}
      AND {sql_lonlat_nonempty("p.pixel_data")}
      AND {official_s2_sql("p")}
    ORDER BY p.date ASC, p.scene_id ASC
    """
)

# 冻结行的 WHERE 使 RETURNING 为空，调用方不得再上传对象。
_UPSERT = text(
    """
    INSERT INTO agric_satellite.parcel_autumn_emergence (
        land_id, season_key, status, emerged_flag, frozen,
        baseline_start, baseline_end, baseline_statistic,
        noise_floor, noise_statistic, as_of_date, first_emerged_date,
        method_version, n_clear, n_pixels, median_increment,
        share_above_noise, share_sustained, share_ndvi_ge_0_30,
        ndre_share_above_noise, summary, timeseries, pixel_oss_key,
        computed_at, frozen_at
    ) VALUES (
        :land_id, :season_key, :status, :emerged_flag, :frozen,
        :baseline_start, :baseline_end, :baseline_statistic,
        :noise_floor, :noise_statistic, :as_of_date, :first_emerged_date,
        :method_version, :n_clear, :n_pixels, :median_increment,
        :share_above_noise, :share_sustained, :share_ndvi_ge_0_30,
        :ndre_share_above_noise, CAST(:summary AS jsonb),
        CAST(:timeseries AS jsonb), :pixel_oss_key,
        now(), :frozen_at
    )
    ON CONFLICT (land_id, season_key) DO UPDATE SET
        status = EXCLUDED.status,
        emerged_flag = EXCLUDED.emerged_flag,
        frozen = EXCLUDED.frozen,
        baseline_start = EXCLUDED.baseline_start,
        baseline_end = EXCLUDED.baseline_end,
        baseline_statistic = EXCLUDED.baseline_statistic,
        noise_floor = EXCLUDED.noise_floor,
        noise_statistic = EXCLUDED.noise_statistic,
        as_of_date = EXCLUDED.as_of_date,
        first_emerged_date = EXCLUDED.first_emerged_date,
        method_version = EXCLUDED.method_version,
        n_clear = EXCLUDED.n_clear,
        n_pixels = EXCLUDED.n_pixels,
        median_increment = EXCLUDED.median_increment,
        share_above_noise = EXCLUDED.share_above_noise,
        share_sustained = EXCLUDED.share_sustained,
        share_ndvi_ge_0_30 = EXCLUDED.share_ndvi_ge_0_30,
        ndre_share_above_noise = EXCLUDED.ndre_share_above_noise,
        summary = EXCLUDED.summary,
        timeseries = EXCLUDED.timeseries,
        pixel_oss_key = EXCLUDED.pixel_oss_key,
        computed_at = now(),
        frozen_at = EXCLUDED.frozen_at
    WHERE parcel_autumn_emergence.frozen = false
    RETURNING land_id
    """
)

_ENQUEUE = text(
    """
    INSERT INTO agric_satellite.parcel_autumn_emergence_outbox
        (land_id, scene_date, status, attempts, available_at, updated_at)
    VALUES (:land_id, :scene_date, 'pending', 0,
            now() + make_interval(secs => :delay), now())
    ON CONFLICT (land_id) DO UPDATE SET
        scene_date = CASE
            WHEN parcel_autumn_emergence_outbox.status = 'completed'
                THEN EXCLUDED.scene_date
            WHEN parcel_autumn_emergence_outbox.scene_date IS NULL
                THEN EXCLUDED.scene_date
            WHEN EXCLUDED.scene_date IS NULL
                THEN parcel_autumn_emergence_outbox.scene_date
            ELSE GREATEST(
                parcel_autumn_emergence_outbox.scene_date, EXCLUDED.scene_date
            )
        END,
        status = 'pending',
        attempts = 0,
        lease_owner = NULL,
        lease_until = NULL,
        last_error = NULL,
        available_at = EXCLUDED.available_at,
        completed_at = NULL,
        updated_at = now()
    """
)

_CLAIM = text(
    """
    WITH eligible AS (
        SELECT land_id
        FROM agric_satellite.parcel_autumn_emergence_outbox
        WHERE (status = 'pending' AND available_at <= now())
           OR (status = 'processing' AND lease_until <= now())
        ORDER BY available_at
        FOR UPDATE SKIP LOCKED
        LIMIT :limit
    )
    UPDATE agric_satellite.parcel_autumn_emergence_outbox AS outbox
    SET status = 'processing',
        attempts = outbox.attempts + 1,
        lease_owner = :worker_id,
        lease_until = now() + make_interval(secs => :lease_seconds),
        updated_at = now()
    FROM eligible
    WHERE outbox.land_id = eligible.land_id
    RETURNING outbox.land_id, outbox.scene_date, outbox.attempts
    """
)

_COMPLETE = text(
    """
    UPDATE agric_satellite.parcel_autumn_emergence_outbox
    SET status = 'completed', lease_owner = NULL, lease_until = NULL,
        last_error = NULL, completed_at = now(), updated_at = now()
    WHERE land_id = :land_id AND status = 'processing' AND lease_owner = :worker_id
    """
)

_RETRY = text(
    """
    UPDATE agric_satellite.parcel_autumn_emergence_outbox
    SET status = 'pending',
        available_at = now() + make_interval(
            secs => LEAST(3600, 30 * power(2, LEAST(attempts - 1, 7))::integer)
        ),
        lease_owner = NULL, lease_until = NULL, last_error = :error, updated_at = now()
    WHERE land_id = :land_id AND status = 'processing' AND lease_owner = :worker_id
    """
)


def autumn_emergence_enabled() -> bool:
    raw = os.getenv("AUTUMN_EMERGENCE_ENABLED", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def stacks_from_rows(rows: list[dict[str, Any]]) -> list[DateStack]:
    """同一天多景时保留晴空像元更多的一景；云多的日期不进入平台。"""
    best: dict[date, tuple[int, DateStack]] = {}
    for row in rows:
        if not is_official_optical_product(
            source=row.get("source"),
            scene_id=row.get("scene_id"),
            decloud_quality=row.get("decloud_quality"),
            parcel_cloud_cover_pct=_float(row.get("parcel_cloud_cover_pct")),
            cloud_cover=_float(row.get("cloud_cover")),
            cloud_cover_over_30=_bool(row.get("cloud_cover_over_30")),
            parcel_cloud_source=row.get("parcel_cloud_source"),
        ):
            continue
        day = _as_date(row.get("date"))
        if day is None or autumn_season_key(day) is None:
            continue
        stack, total = _stack_from_pixel_data(day, row.get("pixel_data"))
        if stack is None or total <= 0:
            continue
        clear_n = int(stack.ndvi.size)
        if clear_n / total < MIN_CLEAR_FRACTION:
            continue
        current = best.get(day)
        if current is None or clear_n > current[0]:
            best[day] = (clear_n, stack)
    return [item[1] for _, item in sorted(best.items())]


async def recompute_land(
    db: AsyncSession,
    land_id: str,
    scene_date: date | None = None,
    *,
    storage: Any | None = None,
    season_key: str | None = None,
) -> dict[str, Any]:
    """重算一季。已冻结则直接返回，不读像元、不写表、不上传。"""
    land = await _load_land(db, land_id)
    if land is None:
        return {"action": "skipped_missing", "land_id": land_id}
    if not is_winter_wheat(land.get("crop_type")):
        return {"action": "skipped_ineligible", "land_id": land_id}
    if (
        season_key is None
        and scene_date is not None
        and autumn_season_key(scene_date) is None
    ):
        # 返青窗口的新景不触发秋季出苗。
        return {"action": "skipped_window", "land_id": land_id}
    season = season_key or (
        autumn_season_key(scene_date) if scene_date is not None else None
    )
    if season is None:
        season = default_season_key(scene_date or date.today())
    existing = await _load_row(db, land_id, season)
    if _is_frozen(existing):
        return {
            "action": "skipped_frozen",
            "land_id": land_id,
            "season_key": season,
            "status": existing.get("status") if existing else None,
        }
    start, end = season_bounds(season)
    scene_rows = await _load_scene_rows(db, land_id, start, end)
    result = analyze(stacks_from_rows(scene_rows))
    if result.season_key not in {None, season}:
        return {"action": "skipped_window", "land_id": land_id, "season_key": season}
    payload = result.to_payload(land_id, season)
    frozen = bool(result.emerged_flag and payload)
    raw = _dumps(payload) if payload else None
    key = None
    if raw is not None:
        digest = hashlib.sha256(raw).hexdigest()[:16]
        key = object_key(land_id, season, frozen=frozen, digest=digest)
    wrote = await _upsert_result(
        db,
        land_id=land_id,
        season=season,
        result=result,
        frozen=frozen,
        pixel_oss_key=key,
    )
    if not wrote:
        return {"action": "skipped_frozen", "land_id": land_id, "season_key": season}
    if raw is not None and key is not None:
        store = storage if storage is not None else _default_storage()
        store.put_bytes(key, raw, content_type="application/json")
    return {
        "action": "updated",
        "land_id": land_id,
        "season_key": season,
        "status": result.status,
        "emerged_flag": frozen,
        "frozen": frozen,
        "pixel_oss_key": key,
        "noise_floor": result.noise_floor,
        "method_version": METHOD_VERSION,
    }


async def get_summary(
    db: AsyncSession,
    land_id: str,
    season_key: str | None = None,
    *,
    today: date | None = None,
    enqueue: bool = True,
) -> dict[str, Any] | None:
    """地块不存在返回 None。未计算的冬小麦返回 pending，并入队。"""
    land = await _load_land(db, land_id)
    if land is None:
        return None
    season = season_key or default_season_key(today or date.today())
    eligible = is_winter_wheat(land.get("crop_type"))
    base = _empty_view(land_id, season, eligible=eligible)
    if not eligible:
        return base
    row = await _load_row(db, land_id, season)
    if row is None:
        base["pending"] = True
        if enqueue and autumn_emergence_enabled():
            start, _end = season_bounds(season)
            await enqueue_lands(db, [land_id], start, delay_seconds=0)
        return base
    return _view_from_row(land_id, row)


async def get_pixels(
    db: AsyncSession,
    land_id: str,
    season_key: str | None = None,
    on: date | None = None,
    *,
    today: date | None = None,
    storage: Any | None = None,
) -> dict[str, Any] | None:
    """返回热力图点集。地块不存在为 None；尚无快照时 ``missing`` 为 True。"""
    land = await _load_land(db, land_id)
    if land is None:
        return None
    season = season_key or default_season_key(today or date.today())
    eligible = is_winter_wheat(land.get("crop_type"))
    if not eligible:
        return {
            "land_id": land_id,
            "field_id": land_id,
            "eligible": False,
            "season_key": season,
            "missing": False,
        }
    row = await _load_row(db, land_id, season)
    if row is None or not row.get("pixel_oss_key"):
        return {
            "land_id": land_id,
            "field_id": land_id,
            "eligible": True,
            "season_key": season,
            "missing": True,
            "as_of_date": _as_date(row.get("as_of_date")) if row else None,
        }
    store = storage if storage is not None else _default_storage()
    raw = store.get_bytes(str(row["pixel_oss_key"]))
    payload = json.loads(raw)
    try:
        points = pixels_lonlat_for_date(payload, on)
    except KeyError:
        return {
            "land_id": land_id,
            "field_id": land_id,
            "eligible": True,
            "season_key": season,
            "missing": False,
            "missing_date": True,
            "as_of_date": _as_date(row.get("as_of_date")),
            "obs_dates": list(payload.get("obs_dates") or []),
        }
    return {
        "land_id": land_id,
        "field_id": land_id,
        "eligible": True,
        "season_key": season,
        "missing": False,
        "missing_date": False,
        "as_of_date": _as_date(row.get("as_of_date")),
        "method_version": row.get("method_version") or METHOD_VERSION,
        "noise_floor": _float(row.get("noise_floor")),
        "frozen": bool(row.get("frozen")),
        "emerged_flag": bool(row.get("emerged_flag")),
        "status": row.get("status"),
        "status_zh": STATUS_ZH.get(str(row.get("status") or ""), None),
        "disclaimer_zh": DISCLAIMER_ZH,
        "source": "stored",
        **points,
    }


async def enqueue_lands(
    db: AsyncSession,
    land_ids: list[str] | Any,
    scene_date: date | None = None,
    *,
    delay_seconds: int = OUTBOX_DEBOUNCE_SECONDS,
) -> int:
    n = 0
    for land_id in dict.fromkeys(str(x) for x in land_ids if x):
        await db.execute(
            _ENQUEUE,
            {
                "land_id": land_id,
                "scene_date": scene_date,
                "delay": int(delay_seconds),
            },
        )
        n += 1
    return n


def emergence_target_from_scene_result(
    envelope: dict[str, Any], stats: dict[str, Any]
) -> tuple[str, date | None] | None:
    """只接已入库的 S2。S1 和返青季由入队函数再丢弃。"""
    stats = stats if isinstance(stats, dict) else {}
    envelope = envelope if isinstance(envelope, dict) else {}
    while isinstance(envelope.get("apply"), dict):
        envelope = envelope["apply"]
    while isinstance(stats.get("apply"), dict):
        stats = stats["apply"]
    oss = stats.get("oss") if isinstance(stats.get("oss"), dict) else {}
    if (
        max(int(stats.get("scene_upserts") or 0), int(oss.get("scene_upserts") or 0))
        <= 0
    ):
        return None
    sources = [
        envelope.get("extras"),
        envelope.get("result"),
        envelope.get("payload"),
        envelope.get("inline"),
        envelope,
    ]
    sources = [item for item in sources if isinstance(item, dict)]

    def first(key: str) -> Any:
        for item in sources:
            if item.get(key) not in (None, ""):
                return item.get(key)
        return None

    land_id = first("land_id")
    sensor = str(first("sensor") or "").upper()
    if not land_id or sensor != SENSOR:
        return None
    scene_date = _as_date(first("date"))
    return str(land_id), scene_date


async def enqueue_from_scene_result(
    envelope: dict[str, Any], stats: dict[str, Any]
) -> bool:
    """场景入库后的轻量钩子。返青季和未启用时不入队。"""
    if not autumn_emergence_enabled():
        return False
    target = emergence_target_from_scene_result(envelope, stats)
    if target is None:
        return False
    land_id, scene_date = target
    if scene_date is not None and autumn_season_key(scene_date) is None:
        return False
    from app.core.database import async_session

    async with async_session() as db:
        await enqueue_lands(db, [land_id], scene_date)
        await db.commit()
    return True


async def run_autumn_emergence_outbox() -> None:
    """API 进程后台：领取待算地块。已出苗的季在 recompute 里空过。"""
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    retry = OUTBOX_POLL_SECONDS
    while True:
        try:
            n = await _drain_once(worker_id)
            retry = OUTBOX_POLL_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("autumn_emergence_outbox_scan_failed", error=str(exc)[:300])
            await asyncio.sleep(retry)
            retry = min(300, retry * 2)
            continue
        if n < OUTBOX_CLAIM_LIMIT:
            await asyncio.sleep(OUTBOX_POLL_SECONDS)


async def _drain_once(worker_id: str) -> int:
    from app.core.database import async_session

    async with async_session() as db:
        claimed = [
            _as_dict(row)
            for row in (
                await db.execute(
                    _CLAIM,
                    {
                        "limit": OUTBOX_CLAIM_LIMIT,
                        "worker_id": worker_id,
                        "lease_seconds": OUTBOX_LEASE_SECONDS,
                    },
                )
            ).fetchall()
        ]
        await db.commit()
    for item in claimed:
        land_id = str(item["land_id"])
        scene_date = _as_date(item.get("scene_date"))
        try:
            async with async_session() as db:
                outcome = await recompute_land(db, land_id, scene_date)
                await db.execute(
                    _COMPLETE, {"land_id": land_id, "worker_id": worker_id}
                )
                await db.commit()
            logger.info(
                "autumn_emergence_recomputed",
                land_id=land_id,
                action=outcome.get("action"),
                status=outcome.get("status"),
                season_key=outcome.get("season_key"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("autumn_emergence_recompute_failed", land_id=land_id)
            async with async_session() as db:
                await db.execute(
                    _RETRY,
                    {
                        "land_id": land_id,
                        "worker_id": worker_id,
                        "error": f"{type(exc).__name__}: {exc}"[:500],
                    },
                )
                await db.commit()
    return len(claimed)


async def _load_land(db: AsyncSession, land_id: str) -> dict[str, Any] | None:
    row = (await db.execute(_LOAD_LAND, {"land_id": land_id})).fetchone()
    if row is None:
        return None
    return _as_dict(row)


async def _load_row(
    db: AsyncSession, land_id: str, season_key: str
) -> dict[str, Any] | None:
    row = (
        await db.execute(_LOAD_ROW, {"land_id": land_id, "season_key": season_key})
    ).fetchone()
    if row is None:
        return None
    return _as_dict(row)


async def _load_scene_rows(
    db: AsyncSession, land_id: str, start: date, end: date
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            _LOAD_SCENES,
            {
                "land_id": land_id,
                "date_from": start,
                "date_to": end,
                "cloud_max": CLOUD_MAX_PCT,
            },
        )
    ).fetchall()
    return [_as_dict(row) for row in rows]


def _is_frozen(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    return bool(row.get("frozen") or row.get("emerged_flag"))


async def _upsert_result(
    db: AsyncSession,
    *,
    land_id: str,
    season: str,
    result: EmergenceResult,
    frozen: bool,
    pixel_oss_key: str | None,
) -> bool:
    baseline_dates = result.baseline_dates
    params = {
        "land_id": land_id,
        "season_key": season,
        "status": result.status,
        "emerged_flag": frozen,
        "frozen": frozen,
        "baseline_start": baseline_dates[0] if baseline_dates else None,
        "baseline_end": baseline_dates[-1] if baseline_dates else None,
        "baseline_statistic": BASELINE_STATISTIC,
        "noise_floor": result.noise_floor,
        "noise_statistic": NOISE_STATISTIC,
        "as_of_date": result.as_of_date,
        "first_emerged_date": result.first_emerged_date if frozen else None,
        "method_version": METHOD_VERSION,
        "n_clear": result.n_clear,
        "n_pixels": result.n_pixels,
        "median_increment": result.median_increment,
        "share_above_noise": result.share_above_noise,
        "share_sustained": result.share_sustained,
        "share_ndvi_ge_0_30": result.share_ndvi_ge_0_30,
        "ndre_share_above_noise": result.ndre_share_above_noise,
        "summary": json.dumps(result.summary, ensure_ascii=False),
        "timeseries": json.dumps(result.timeseries, ensure_ascii=False),
        "pixel_oss_key": pixel_oss_key,
        "frozen_at": datetime.now(timezone.utc) if frozen else None,
    }
    row = (await db.execute(_UPSERT, params)).fetchone()
    return row is not None


def _empty_view(land_id: str, season: str, *, eligible: bool) -> dict[str, Any]:
    return {
        "land_id": land_id,
        "field_id": land_id,
        "eligible": eligible,
        "pending": False,
        "season_key": season,
        "status": None,
        "status_zh": None,
        "emerged_flag": False,
        "frozen": False,
        "baseline": {
            "dates": [],
            "start": None,
            "end": None,
            "statistic": None,
        },
        "noise_floor": None,
        "noise_statistic": None,
        "as_of_date": None,
        "first_emerged_date": None,
        "method_version": METHOD_VERSION,
        "summary": {},
        "timeseries": [],
        "computed_at": None,
        "frozen_at": None,
        "pixel_oss_key": None,
        "disclaimer_zh": DISCLAIMER_ZH,
        "rule_zh": RULE_ZH,
    }


def _view_from_row(land_id: str, row: dict[str, Any]) -> dict[str, Any]:
    summary = _jsonish(row.get("summary")) or {}
    if not isinstance(summary, dict):
        summary = {}
    timeseries = _jsonish(row.get("timeseries")) or []
    if not isinstance(timeseries, list):
        timeseries = []
    status = row.get("status")
    dates = summary.get("baseline_dates") or []
    return {
        "land_id": land_id,
        "field_id": land_id,
        "eligible": True,
        "pending": False,
        "season_key": row.get("season_key"),
        "status": status,
        "status_zh": STATUS_ZH.get(str(status), None),
        "emerged_flag": bool(row.get("emerged_flag")),
        "frozen": bool(row.get("frozen")),
        "baseline": {
            "dates": dates,
            "start": row.get("baseline_start"),
            "end": row.get("baseline_end"),
            "statistic": row.get("baseline_statistic"),
        },
        "noise_floor": _float(row.get("noise_floor")),
        "noise_statistic": row.get("noise_statistic"),
        "as_of_date": row.get("as_of_date"),
        "first_emerged_date": row.get("first_emerged_date"),
        "method_version": row.get("method_version") or METHOD_VERSION,
        "summary": {
            "n_clear": row.get("n_clear"),
            "n_pixels": row.get("n_pixels"),
            "median_increment": _float(row.get("median_increment")),
            "share_above_noise": _float(row.get("share_above_noise")),
            "share_sustained": _float(row.get("share_sustained")),
            "share_ndvi_ge_0_30": _float(row.get("share_ndvi_ge_0_30")),
            "ndre_share_above_noise": _float(row.get("ndre_share_above_noise")),
            "class_counts": summary.get("class_counts"),
            "reason": summary.get("reason"),
        },
        "timeseries": timeseries,
        "computed_at": row.get("computed_at"),
        "frozen_at": row.get("frozen_at"),
        "pixel_oss_key": row.get("pixel_oss_key"),
        "disclaimer_zh": DISCLAIMER_ZH,
        "rule_zh": RULE_ZH,
    }


def _stack_from_pixel_data(day: date, pixel_data: Any) -> tuple[DateStack | None, int]:
    if isinstance(pixel_data, str):
        try:
            pixel_data = json.loads(pixel_data)
        except json.JSONDecodeError:
            return None, 0
    pixels = extract_pixels(pixel_data)
    if not pixels:
        return None, 0
    lon: list[float] = []
    lat: list[float] = []
    ndvi: list[float] = []
    ndre: list[float] = []
    has_ndre = False
    for pix in pixels:
        if not _pixel_clear(pix):
            continue
        value = _ndvi_of(pix)
        lon_v = _float(pix.get("lon"))
        lat_v = _float(pix.get("lat"))
        if value is None or lon_v is None or lat_v is None:
            continue
        lon.append(lon_v)
        lat.append(lat_v)
        ndvi.append(value)
        ndre_v = _float(pix.get("NDRE"))
        if ndre_v is None:
            ndre_v = _float(pix.get("ndre"))
        if ndre_v is not None and -1.0 <= ndre_v <= 1.0:
            ndre.append(ndre_v)
            has_ndre = True
        else:
            ndre.append(float("nan"))
    if not lon:
        return None, len(pixels)
    return (
        DateStack(
            day=day,
            lon=np.asarray(lon, dtype=float),
            lat=np.asarray(lat, dtype=float),
            ndvi=np.asarray(ndvi, dtype=float),
            ndre=np.asarray(ndre, dtype=float) if has_ndre else None,
        ),
        len(pixels),
    )


def _pixel_clear(pix: dict[str, Any]) -> bool:
    if "clear" not in pix and "Clear" not in pix:
        return True
    raw = pix.get("clear", pix.get("Clear"))
    try:
        return int(raw) == 1
    except (TypeError, ValueError):
        return False


def _ndvi_of(pix: dict[str, Any]) -> float | None:
    raw = pix.get("NDVI", pix.get("ndvi"))
    value = _float(raw)
    if value is None or value < -1.0 or value > 1.0:
        return None
    return value


def _default_storage() -> Any:
    from app.core.storage import get_parcel_product_storage

    return get_parcel_product_storage()


def _dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _as_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text_value = str(value).strip().lower()
    if text_value in {"1", "true", "t", "yes"}:
        return True
    if text_value in {"0", "false", "f", "no"}:
        return False
    return None
