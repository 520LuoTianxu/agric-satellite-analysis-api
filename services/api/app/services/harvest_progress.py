"""地块逐日收获占比：读取 S2 像元、计算、落库，以及新影像入库后的 outbox 重算。

链路（全部在 API 机，下载机不碰 Postgres）：

1. ``scene_result_cache`` 场景结果入库成功后调用 :func:`enqueue_from_scene_result`，
   把 (land_id, 最早受影响日期) 合并写入 ``parcel_harvest_progress_outbox``；
2. API 进程内 :func:`run_harvest_progress_outbox` 周期按租约领取地块，调用
   :func:`recompute_land` 幂等重算该地块受影响日期到今天的序列；
3. 历史补算走 ``POST /v1/internal/harvest-progress/backfill``（批量入队）或
   ``python -m app.services.harvest_progress --land-id ... --from ...``（直接计算）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import uuid
from datetime import date, timedelta
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agri_classify import is_official_optical_product
from app.core.harvest_progress import (
    HARVEST_PROGRESS_METHOD_VERSION,
    HarvestProgressThresholds,
    compute_harvest_series,
)
from app.core.logging import logger

SENSOR = "S2"
DEFAULT_RECENT_DAYS = 60
OUTBOX_POLL_SECONDS = 10
OUTBOX_CLAIM_LIMIT = 10
OUTBOX_LEASE_SECONDS = 600
# 同一地块一天内常有多景/多次回执，短暂延迟合并成一次重算。
OUTBOX_DEBOUNCE_SECONDS = 30


def harvest_progress_enabled() -> bool:
    raw = os.getenv("HARVEST_PROGRESS_ENABLED", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value, parse_constant=lambda _v: None)
        except json.JSONDecodeError:
            return None
    return value


_LOAD_AREA = text(
    """
    SELECT COALESCE(land_area_mu, area_ha * 15) AS area_mu
    FROM agric_satellite.land_parcels
    WHERE land_id = :land_id
    """
)

# 只取像元数组和质量字段，避免把整份产品 JSON 拉进内存。
_LOAD_SCENES = text(
    """
    SELECT date, scene_id,
           pixel_data->'pixels' AS pixels,
           COALESCE(NULLIF(BTRIM(product_source), ''), NULLIF(BTRIM(pixel_data->>'source'), '')) AS source,
           COALESCE(NULLIF(BTRIM(decloud_quality), ''), NULLIF(BTRIM(pixel_data->>'decloud_quality'), '')) AS decloud_quality,
           parcel_cloud_cover_pct, cloud_cover, cloud_cover_over_30,
           COALESCE(NULLIF(BTRIM(parcel_cloud_source), ''), NULLIF(BTRIM(pixel_data->>'parcel_cloud_source'), '')) AS parcel_cloud_source
    FROM agric_satellite.parcel_scene_products
    WHERE land_id = :land_id
      AND sensor = 'S2'
      AND date >= :date_from AND date <= :date_to
      AND pixel_data->>'format' = 'lonlat_v1'
      AND jsonb_typeof(pixel_data->'pixels') = 'array'
    ORDER BY date ASC, scene_id ASC
    """
)

_DELETE_RANGE = text(
    """
    DELETE FROM agric_satellite.parcel_harvest_progress
    WHERE land_id = :land_id AND sensor = :sensor AND method_version = :method_version
      AND obs_date >= :date_from AND obs_date <= :date_to
    """
)

_UPSERT_ROW = text(
    """
    INSERT INTO agric_satellite.parcel_harvest_progress (
        land_id, obs_date, sensor, method_version, scene_id, status,
        harvested_pct, newly_harvested_pct, harvested_area_mu, parcel_area_mu,
        valid_pct, valid_pixel_count, harvested_pixel_count, total_pixel_count,
        mean_ndvi, peak_ndvi, peak_date, official, params, updated_at
    ) VALUES (
        :land_id, :obs_date, :sensor, :method_version, :scene_id, :status,
        :harvested_pct, :newly_harvested_pct, :harvested_area_mu, :parcel_area_mu,
        :valid_pct, :valid_pixel_count, :harvested_pixel_count, :total_pixel_count,
        :mean_ndvi, :peak_ndvi, :peak_date, :official, CAST(:params AS jsonb), now()
    )
    ON CONFLICT (land_id, obs_date, sensor, method_version) DO UPDATE SET
        scene_id = EXCLUDED.scene_id,
        status = EXCLUDED.status,
        harvested_pct = EXCLUDED.harvested_pct,
        newly_harvested_pct = EXCLUDED.newly_harvested_pct,
        harvested_area_mu = EXCLUDED.harvested_area_mu,
        parcel_area_mu = EXCLUDED.parcel_area_mu,
        valid_pct = EXCLUDED.valid_pct,
        valid_pixel_count = EXCLUDED.valid_pixel_count,
        harvested_pixel_count = EXCLUDED.harvested_pixel_count,
        total_pixel_count = EXCLUDED.total_pixel_count,
        mean_ndvi = EXCLUDED.mean_ndvi,
        peak_ndvi = EXCLUDED.peak_ndvi,
        peak_date = EXCLUDED.peak_date,
        official = EXCLUDED.official,
        params = EXCLUDED.params,
        updated_at = now()
    """
)


async def load_land_area_mu(db: AsyncSession, land_id: str) -> tuple[bool, float | None]:
    row = (await db.execute(_LOAD_AREA, {"land_id": land_id})).first()
    if row is None:
        return False, None
    area = row[0]
    return True, float(area) if area is not None else None


async def load_scene_inputs(
    db: AsyncSession, land_id: str, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            _LOAD_SCENES,
            {"land_id": land_id, "date_from": date_from, "date_to": date_to},
        )
    ).fetchall()
    scenes: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r._mapping)
        pixels = _jsonish(d.get("pixels"))
        if not isinstance(pixels, list) or not pixels:
            continue
        scenes.append(
            {
                "date": d.get("date"),
                "scene_id": d.get("scene_id"),
                "pixels": pixels,
                "official": is_official_optical_product(
                    source=d.get("source"),
                    scene_id=d.get("scene_id"),
                    decloud_quality=d.get("decloud_quality"),
                    parcel_cloud_cover_pct=d.get("parcel_cloud_cover_pct"),
                    cloud_cover=d.get("cloud_cover"),
                    cloud_cover_over_30=d.get("cloud_cover_over_30"),
                    parcel_cloud_source=d.get("parcel_cloud_source"),
                ),
            }
        )
    return scenes


async def compute_land_series(
    db: AsyncSession,
    land_id: str,
    date_from: date,
    date_to: date,
    *,
    area_mu: float | None = None,
    thresholds: HarvestProgressThresholds | None = None,
) -> list[dict[str, Any]]:
    """计算 [date_from, date_to] 内的序列；额外读取回看窗口作为季节峰值与上期上下文。"""
    thr = thresholds or HarvestProgressThresholds.from_env()
    context_from = date_from - timedelta(days=thr.lookback_days)
    scenes = await load_scene_inputs(db, land_id, context_from, date_to)
    series = compute_harvest_series(scenes, parcel_area_mu=area_mu, thresholds=thr)
    lo, hi = date_from.isoformat(), date_to.isoformat()
    return [row for row in series if lo <= row["date"] <= hi]


async def recompute_land(
    db: AsyncSession,
    land_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    *,
    thresholds: HarvestProgressThresholds | None = None,
) -> int:
    """幂等重算并覆盖写入一个地块的区间结果；返回写入行数（不提交事务）。"""
    thr = thresholds or HarvestProgressThresholds.from_env()
    date_to = date_to or date.today()
    date_from = date_from or (date_to - timedelta(days=DEFAULT_RECENT_DAYS))
    exists, area_mu = await load_land_area_mu(db, land_id)
    if not exists:
        return 0
    rows = await compute_land_series(
        db, land_id, date_from, date_to, area_mu=area_mu, thresholds=thr
    )
    base = {
        "land_id": land_id,
        "sensor": SENSOR,
        "method_version": HARVEST_PROGRESS_METHOD_VERSION,
    }
    # 先删区间再写入：影像被替换或不再满足有效像元要求时不会残留旧结果。
    await db.execute(_DELETE_RANGE, {**base, "date_from": date_from, "date_to": date_to})
    params_json = json.dumps(thr.to_dict(), separators=(",", ":"))
    for row in rows:
        await db.execute(
            _UPSERT_ROW,
            {
                **base,
                "obs_date": date.fromisoformat(row["date"]),
                "scene_id": row.get("scene_id"),
                "status": row["status"],
                "harvested_pct": row["harvested_pct"],
                "newly_harvested_pct": row["newly_harvested_pct"],
                "harvested_area_mu": row.get("harvested_area_mu"),
                "parcel_area_mu": row.get("parcel_area_mu"),
                "valid_pct": row["valid_pct"],
                "valid_pixel_count": row["valid_pixel_count"],
                "harvested_pixel_count": row["harvested_pixel_count"],
                "total_pixel_count": row["total_pixel_count"],
                "mean_ndvi": row.get("mean_ndvi"),
                "peak_ndvi": row.get("peak_ndvi"),
                "peak_date": date.fromisoformat(row["peak_date"])
                if row.get("peak_date")
                else None,
                "official": bool(row.get("official", True)),
                "params": params_json,
            },
        )
    return len(rows)


_LIST_STORED = text(
    """
    SELECT obs_date, sensor, scene_id, status, harvested_pct, newly_harvested_pct,
           harvested_area_mu, parcel_area_mu, valid_pct, mean_ndvi, peak_ndvi,
           peak_date, official
    FROM agric_satellite.parcel_harvest_progress
    WHERE land_id = :land_id AND sensor = :sensor AND method_version = :method_version
      AND obs_date >= :date_from AND obs_date <= :date_to
    ORDER BY obs_date ASC
    """
)


async def list_stored(
    db: AsyncSession, land_id: str, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            _LIST_STORED,
            {
                "land_id": land_id,
                "sensor": SENSOR,
                "method_version": HARVEST_PROGRESS_METHOD_VERSION,
                "date_from": date_from,
                "date_to": date_to,
            },
        )
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r._mapping)
        out.append(
            {
                "date": d["obs_date"].isoformat(),
                "sensor": d["sensor"],
                "scene_id": d.get("scene_id"),
                "status": d["status"],
                "harvested_pct": float(d["harvested_pct"]),
                "newly_harvested_pct": float(d["newly_harvested_pct"]),
                "harvested_area_mu": _num(d.get("harvested_area_mu")),
                "parcel_area_mu": _num(d.get("parcel_area_mu")),
                "valid_pct": float(d["valid_pct"]),
                "mean_ndvi": _num(d.get("mean_ndvi"), 4),
                "peak_ndvi": _num(d.get("peak_ndvi"), 4),
                "peak_date": d["peak_date"].isoformat() if d.get("peak_date") else None,
                "official": bool(d.get("official")),
            }
        )
    return out


def _num(value: Any, digits: int = 2) -> float | None:
    # real 列回读会带单精度尾数，统一按展示精度取整。
    return round(float(value), digits) if value is not None else None


# ── outbox ───────────────────────────────────────────────────────────

_ENQUEUE = text(
    """
    INSERT INTO agric_satellite.parcel_harvest_progress_outbox
        (land_id, date_from, status, attempts, available_at, updated_at)
    VALUES (:land_id, :date_from, 'pending', 0,
            now() + make_interval(secs => :delay), now())
    ON CONFLICT (land_id) DO UPDATE SET
        date_from = CASE
            WHEN parcel_harvest_progress_outbox.status = 'completed'
                THEN EXCLUDED.date_from
            ELSE LEAST(parcel_harvest_progress_outbox.date_from, EXCLUDED.date_from)
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
        FROM agric_satellite.parcel_harvest_progress_outbox
        WHERE (status = 'pending' AND available_at <= now())
           OR (status = 'processing' AND lease_until <= now())
        ORDER BY available_at
        FOR UPDATE SKIP LOCKED
        LIMIT :limit
    )
    UPDATE agric_satellite.parcel_harvest_progress_outbox AS outbox
    SET status = 'processing',
        attempts = outbox.attempts + 1,
        lease_owner = :worker_id,
        lease_until = now() + make_interval(secs => :lease_seconds),
        updated_at = now()
    FROM eligible
    WHERE outbox.land_id = eligible.land_id
    RETURNING outbox.land_id, outbox.date_from, outbox.attempts
    """
)

# 处理期间若有新影像再次入队，状态会被改回 pending 且租约清空，这里就不会误标完成。
_COMPLETE = text(
    """
    UPDATE agric_satellite.parcel_harvest_progress_outbox
    SET status = 'completed', lease_owner = NULL, lease_until = NULL,
        last_error = NULL, completed_at = now(), updated_at = now()
    WHERE land_id = :land_id AND status = 'processing' AND lease_owner = :worker_id
    """
)

_RETRY = text(
    """
    UPDATE agric_satellite.parcel_harvest_progress_outbox
    SET status = 'pending',
        available_at = now() + make_interval(
            secs => LEAST(3600, 30 * power(2, LEAST(attempts - 1, 7))::integer)
        ),
        lease_owner = NULL, lease_until = NULL, last_error = :error, updated_at = now()
    WHERE land_id = :land_id AND status = 'processing' AND lease_owner = :worker_id
    """
)


async def enqueue_lands(
    db: AsyncSession,
    land_ids: Iterable[str],
    date_from: date | None = None,
    *,
    delay_seconds: int = OUTBOX_DEBOUNCE_SECONDS,
) -> int:
    """把地块标记为待重算（不提交事务）；date_from 为空时重算近期窗口。"""
    start = date_from or (date.today() - timedelta(days=DEFAULT_RECENT_DAYS))
    n = 0
    for land_id in dict.fromkeys(str(x) for x in land_ids if x):
        await db.execute(
            _ENQUEUE,
            {"land_id": land_id, "date_from": start, "delay": int(delay_seconds)},
        )
        n += 1
    return n


def harvest_target_from_scene_result(
    envelope: dict[str, Any], stats: dict[str, Any]
) -> tuple[str, date | None] | None:
    """从场景结果回执中取 (land_id, 场景日期)；非 S2 或未入库时返回 None。"""
    while isinstance(envelope.get("apply"), dict):
        envelope = envelope["apply"]
    while isinstance(stats.get("apply"), dict):
        stats = stats["apply"]
    oss = stats.get("oss") if isinstance(stats.get("oss"), dict) else {}
    if max(int(stats.get("scene_upserts") or 0), int(oss.get("scene_upserts") or 0)) <= 0:
        return None
    sources = [
        envelope.get("extras"),
        envelope.get("result"),
        envelope.get("payload"),
        envelope.get("inline"),
        envelope,
    ]
    sources = [s for s in sources if isinstance(s, dict)]

    def first(key: str) -> Any:
        for s in sources:
            if s.get(key) not in (None, ""):
                return s.get(key)
        return None

    land_id = first("land_id")
    sensor = str(first("sensor") or "").upper()
    if not land_id or (sensor and sensor != SENSOR):
        return None
    raw_date = first("date")
    scene_date = None
    if raw_date:
        try:
            scene_date = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            scene_date = None
    return str(land_id), scene_date


async def enqueue_from_scene_result(
    envelope: dict[str, Any], stats: dict[str, Any]
) -> bool:
    """场景入库后的轻量钩子：只写一条 outbox，不在结果消费链路里做计算。"""
    if not harvest_progress_enabled():
        return False
    target = harvest_target_from_scene_result(envelope, stats)
    if target is None:
        return False
    land_id, scene_date = target
    from app.core.database import async_session

    async with async_session() as db:
        await enqueue_lands(db, [land_id], scene_date)
        await db.commit()
    return True


async def _drain_once(worker_id: str) -> int:
    from app.core.database import async_session

    async with async_session() as db:
        claimed = [
            dict(r._mapping)
            for r in (
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
        land_id = item["land_id"]
        try:
            async with async_session() as db:
                n = await recompute_land(db, land_id, item.get("date_from"))
                await db.execute(_COMPLETE, {"land_id": land_id, "worker_id": worker_id})
                await db.commit()
            logger.info(
                "harvest_progress_recomputed",
                land_id=land_id,
                date_from=str(item.get("date_from")),
                rows=n,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("harvest_progress_recompute_failed", land_id=land_id)
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


async def run_harvest_progress_outbox() -> None:
    """API 进程后台循环：按租约领取待重算地块，满批立即继续，空闲时轮询。"""
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    retry = OUTBOX_POLL_SECONDS
    while True:
        try:
            n = await _drain_once(worker_id)
            retry = OUTBOX_POLL_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("harvest_progress_outbox_scan_failed", error=str(exc)[:300])
            await asyncio.sleep(retry)
            retry = min(300, retry * 2)
            continue
        if n < OUTBOX_CLAIM_LIMIT:
            await asyncio.sleep(OUTBOX_POLL_SECONDS)


async def _cli(args: argparse.Namespace) -> None:
    from app.core.database import async_session

    date_from = date.fromisoformat(args.date_from) if args.date_from else None
    date_to = date.fromisoformat(args.date_to) if args.date_to else None
    async with async_session() as db:
        if args.enqueue:
            n = await enqueue_lands(db, args.land_id, date_from, delay_seconds=0)
            await db.commit()
            print(f"enqueued {n} lands")
            return
        for land_id in args.land_id:
            n = await recompute_land(db, land_id, date_from, date_to)
            await db.commit()
            print(f"{land_id}: {n} rows")


def main() -> None:
    parser = argparse.ArgumentParser(description="补算地块逐日收获占比")
    parser.add_argument("--land-id", action="append", required=True)
    parser.add_argument("--from", dest="date_from")
    parser.add_argument("--to", dest="date_to")
    parser.add_argument(
        "--enqueue", action="store_true", help="只入队，由 API 后台 outbox 计算"
    )
    asyncio.run(_cli(parser.parse_args()))


if __name__ == "__main__":
    main()


__all__ = [
    "compute_land_series",
    "enqueue_from_scene_result",
    "enqueue_lands",
    "harvest_progress_enabled",
    "harvest_target_from_scene_result",
    "list_stored",
    "recompute_land",
    "run_harvest_progress_outbox",
]
