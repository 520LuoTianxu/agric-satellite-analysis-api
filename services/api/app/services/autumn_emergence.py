"""秋季出苗的读取、人工触发和地块已出苗标记。

计算在下载机：官方 S2 产品写入日指标 JSON 的 autumn_emergence 字段后，
回执入库。API 只记录 land_parcels 上的本季已出苗标记，并从
parcel_scene_products.pixel_data 读取逐日结果。不上传独立出苗对象，
不 mkdir /data，也不改 crop_type。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any

from agric_satellite_analysis_common.agri_classify import official_s2_sql
from agric_satellite_analysis_common.autumn_emergence import (
    DAILY_FIELD_KEY,
    DISCLAIMER_ZH,
    METHOD_VERSION,
    RULE_ZH,
    STATUS_ZH,
    autumn_season_key,
    daily_field_of,
    default_season_key,
    is_winter_wheat,
    pixels_from_daily_field,
    season_bounds,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import logger

SENSOR = "S2"
PULL_DISPLAY_NAME = "出苗"
PULL_PURPOSE = "autumn_emergence"
PULL_CLAIM_TYPE = "satellite_batch"

_LOAD_LAND = text(
    """
    SELECT land_id, crop_type,
           autumn_emerged, autumn_emerged_season, autumn_emerged_date,
           autumn_emerged_at, autumn_sowing_date, autumn_sowing_season
    FROM agric_satellite.land_parcels
    WHERE land_id = :land_id AND deleted_at IS NULL
    """
)

_SAVE_SOWING = text(
    """
    UPDATE agric_satellite.land_parcels
    SET autumn_sowing_date = :sowing_date,
        autumn_sowing_season = :season_key,
        updated_at = now()
    WHERE land_id = :land_id
      AND deleted_at IS NULL
      AND NOT (
          COALESCE(autumn_emerged, false)
          AND autumn_emerged_season IS NOT DISTINCT FROM :season_key
      )
    """
)

_LOAD_DAILY = text(
    f"""
    SELECT p.date, p.scene_id, p.pixel_data, p.json_oss_key,
           p.parcel_cloud_cover_pct, p.cloud_cover, p.product_source
    FROM agric_satellite.parcel_scene_products AS p
    WHERE p.land_id = :land_id
      AND p.sensor = 'S2'
      AND p.date >= :date_from AND p.date <= :date_to
      AND {official_s2_sql("p")}
    ORDER BY p.date ASC, p.scene_id ASC
    """
)

_JOB_PROGRESS = text(
    """
    SELECT j.id::text AS id,
           j.status,
           j.error,
           j.progress_json,
           j.params_json,
           j.created_at,
           w.status AS work_status,
           w.lease_owner,
           w.last_claimed_by,
           w.error AS work_error
    FROM agric_satellite.jobs AS j
    LEFT JOIN LATERAL (
        SELECT status, lease_owner, last_claimed_by, error
        FROM agric_satellite.work_items
        WHERE parent_job_id = j.id
          AND type = 'satellite_batch'
        ORDER BY updated_at DESC
        LIMIT 1
    ) AS w ON true
    WHERE j.id::text = ANY(CAST(:job_ids AS text[]))
    """
)

_LAND_JOBS = text(
    """
    SELECT j.id::text AS id,
           j.status,
           j.error,
           j.progress_json,
           j.params_json,
           j.created_at,
           w.status AS work_status,
           w.lease_owner,
           w.last_claimed_by,
           w.error AS work_error
    FROM agric_satellite.jobs AS j
    LEFT JOIN LATERAL (
        SELECT status, lease_owner, last_claimed_by, error
        FROM agric_satellite.work_items
        WHERE parent_job_id = j.id
          AND type = 'satellite_batch'
        ORDER BY updated_at DESC
        LIMIT 1
    ) AS w ON true
    WHERE j.land_id = :land_id
      AND (
        j.params_json->>'purpose' = 'autumn_emergence'
        OR j.params_json->>'display_type' = 'autumn_emergence'
        OR j.params_json->>'display_name' = '出苗'
        OR COALESCE(j.params_json->>'autumn_emergence_manual', '')
            IN ('true', '1')
      )
    ORDER BY j.created_at DESC
    """
)

_OPEN_JOBS = text(
    """
    SELECT id::text AS id
    FROM agric_satellite.jobs
    WHERE id::text = ANY(CAST(:job_ids AS text[]))
      AND status IN ('pending', 'running')
    """
)


class EmergenceScheduleError(Exception):
    """人工触发无法排期。路由转成 4xx，不在 API 机下载影像。"""

    def __init__(self, detail: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def autumn_emergence_enabled() -> bool:
    raw = os.getenv("AUTUMN_EMERGENCE_ENABLED", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def is_emergence_pull(params: dict[str, Any] | None) -> bool:
    if not isinstance(params, dict):
        return False
    if params.get("autumn_emergence_manual") in {True, "true", "1", 1}:
        return True
    return str(params.get("purpose") or params.get("display_type") or "") == PULL_PURPOSE


def land_frozen(land: dict[str, Any] | None, season: str) -> bool:
    if not land:
        return False
    return bool(land.get("autumn_emerged")) and str(
        land.get("autumn_emerged_season") or ""
    ) == season


async def get_summary(
    db: AsyncSession,
    land_id: str,
    season_key: str | None = None,
    *,
    today: date | None = None,
    enqueue: bool = True,
) -> dict[str, Any] | None:
    """地块不存在返回 None。enqueue 保留兼容，API 不再入计算出队。"""
    del enqueue
    land = await _load_land(db, land_id)
    if land is None:
        return None
    season = season_key or default_season_key(today or date.today())
    sowing = _sowing_for_season(land, season)
    jobs = await _load_season_jobs(db, land_id, season)
    pull = await _pull_progress_from_jobs(db, jobs, sowing=sowing)
    manual = sowing is not None or bool(jobs)
    eligible = is_winter_wheat(land.get("crop_type")) or manual
    frozen = land_frozen(land, season)
    view = _empty_view(land_id, season, eligible=eligible)
    view["sowing_date"] = sowing
    view.update(pull)
    if not eligible:
        return view
    days = await _load_daily_fields(db, land_id, season, sowing=sowing)
    if frozen:
        view["emerged_flag"] = True
        view["frozen"] = True
        view["status"] = "emerged"
        view["status_zh"] = STATUS_ZH["emerged"]
        view["first_emerged_date"] = _as_date(land.get("autumn_emerged_date"))
        view["frozen_at"] = land.get("autumn_emerged_at")
    if days:
        _apply_daily_summary(view, days, frozen=frozen, land=land)
        view["pending"] = False
    else:
        job_status = str(pull.get("job_status") or "")
        view["pending"] = job_status in {"pending", "running"}
    return view


async def get_pixels(
    db: AsyncSession,
    land_id: str,
    season_key: str | None = None,
    on: date | None = None,
    *,
    today: date | None = None,
    storage: Any | None = None,
) -> dict[str, Any] | None:
    """热力图点集，只读官方日产品里的 autumn_emergence 字段，不访问 OSS。"""
    del storage
    land = await _load_land(db, land_id)
    if land is None:
        return None
    season = season_key or default_season_key(today or date.today())
    sowing = _sowing_for_season(land, season)
    jobs = await _load_season_jobs(db, land_id, season)
    manual = sowing is not None or bool(jobs)
    eligible = is_winter_wheat(land.get("crop_type")) or manual
    if not eligible:
        return {
            "land_id": land_id,
            "field_id": land_id,
            "eligible": False,
            "season_key": season,
            "missing": False,
        }
    days = await _load_daily_fields(db, land_id, season, sowing=sowing)
    if not days:
        return {
            "land_id": land_id,
            "field_id": land_id,
            "eligible": True,
            "season_key": season,
            "missing": True,
            "as_of_date": None,
        }
    wanted = on.isoformat() if on is not None else None
    field = days.get(wanted) if wanted else None
    if field is None:
        last = list(days)[-1]
        if wanted and wanted not in days:
            return {
                "land_id": land_id,
                "field_id": land_id,
                "eligible": True,
                "season_key": season,
                "missing": False,
                "missing_date": True,
                "as_of_date": _as_date(days[last].get("as_of_date")) or _as_date(last),
                "obs_dates": list(days),
            }
        field = days[last]
    points = pixels_from_daily_field(field)
    frozen = land_frozen(land, season)
    return {
        "land_id": land_id,
        "field_id": land_id,
        "eligible": True,
        "season_key": season,
        "missing": False,
        "missing_date": False,
        "frozen": frozen,
        "emerged_flag": frozen or bool(field.get("emerged_flag") or points.get("emerged_flag")),
        "status": field.get("status") or points.get("status"),
        "status_zh": field.get("status_zh") or points.get("status_zh"),
        **points,
    }


async def schedule_manual_emergence(
    db: AsyncSession,
    land_id: str,
    sowing_date: date,
    season_key: str | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any] | None:
    """按播种日派发一条可领取的出苗拉取。地块不存在返回 None。

    不论 crop_type 是否为空，都创建 satellite_batch。不修改 crop_type。
    已冻结的季不派发。
    """
    land = await _load_land(db, land_id)
    if land is None:
        return None
    today = today or date.today()
    season = season_key or default_season_key(today)
    start, end = season_bounds(season)
    if sowing_date < start or sowing_date > end:
        raise EmergenceScheduleError(
            f"播种日须落在 {season} 内（{start.isoformat()} 至 {end.isoformat()}）"
        )
    if sowing_date > today:
        raise EmergenceScheduleError("播种日不能晚于今天")
    window_end = min(today, end)
    if land_frozen(land, season):
        stored = _sowing_for_season(land, season)
        return _schedule_payload(
            land_id=land_id,
            season=season,
            sowing_date=stored,
            status="frozen",
            date_from=stored or start,
            date_to=end,
            sensors=[],
            job_ids=[],
            message="该季已出苗并冻结，未改播种日，也未派发下载。请 GET 读取结果。",
        )
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": f"autumn-emergence:{land_id}"},
    )
    existing_jobs = await _load_season_jobs(db, land_id, season)
    open_ids = [
        str(row["id"])
        for row in existing_jobs
        if str(row.get("status") or "") in {"pending", "running"}
    ]
    if open_ids:
        pull = await _pull_progress_from_jobs(db, existing_jobs, sowing=sowing_date)
        if not pull.get("job_ids"):
            pull = _fresh_pull(open_ids)
            pull["date_from"] = sowing_date
            pull["date_to"] = window_end
        return _schedule_payload(
            land_id=land_id,
            season=season,
            sowing_date=_sowing_for_season(land, season) or sowing_date,
            status="already_running",
            date_from=_as_date((existing_jobs[0].get("params_json") or {}).get("date_from"))
            or sowing_date,
            date_to=_as_date((existing_jobs[0].get("params_json") or {}).get("date_to"))
            or window_end,
            sensors=["S2"],
            job_ids=open_ids,
            message=(
                "已有未完成的出苗拉取，未重复派发。"
                "进度请查询这些 job_ids 的 GET /v1/jobs/{job_id}。"
            ),
            pull=pull,
        )
    job_ids = await dispatch_s2_download(
        db,
        land_id,
        date_from=sowing_date,
        date_to=window_end,
        season_key=season,
        sowing_date=sowing_date,
    )
    await db.execute(
        _SAVE_SOWING,
        {"land_id": land_id, "sowing_date": sowing_date, "season_key": season},
    )
    return _schedule_payload(
        land_id=land_id,
        season=season,
        sowing_date=sowing_date,
        status="dispatched",
        date_from=sowing_date,
        date_to=window_end,
        sensors=["S2"],
        job_ids=job_ids,
        message=_dispatch_message(sowing_date, window_end),
        pull=_fresh_pull(job_ids),
    )


async def dispatch_s2_download(
    db: AsyncSession,
    land_id: str,
    *,
    date_from: date,
    date_to: date,
    season_key: str,
    sowing_date: date,
) -> list[str]:
    """走更新影像同一构造器。传感器只有 S2，date_from 是播种日。不改 crop_type。"""
    from agric_satellite_analysis_common.scheduled_land_filter import (
        BYPASS_SCHEDULE_LAND_FILTER_PARAM,
    )
    from agric_satellite_analysis_common.task_priority import MANUAL_TASK_PRIORITY

    from app.core.config import settings
    from app.models.tables import LandParcel
    from app.services.satellite_batch import create_satellite_batch_jobs

    land = await db.get(LandParcel, land_id)
    if land is None or getattr(land, "deleted_at", None) is not None:
        raise EmergenceScheduleError("地块不存在或已删除", 404)
    identity = {
        "display_name": PULL_DISPLAY_NAME,
        "display_type": PULL_PURPOSE,
        "purpose": PULL_PURPOSE,
    }
    try:
        _groups, jobs, _meta = await create_satellite_batch_jobs(
            db,
            [land],
            date_from=date_from,
            date_to=date_to,
            sensors=("S2",),
            force=False,
            job_land_id=land_id,
            chunk_days=settings.index_backfill_chunk_days,
            dispatch_priority=MANUAL_TASK_PRIORITY,
            dispatch_extras=identity,
            extra_params={
                BYPASS_SCHEDULE_LAND_FILTER_PARAM: True,
                "autumn_emergence_manual": True,
                "sowing_date": sowing_date.isoformat(),
                "season_key": season_key,
                **identity,
            },
        )
    except ValueError as exc:
        raise EmergenceScheduleError(str(exc), 422) from exc
    if not jobs:
        raise EmergenceScheduleError("未能派发 S2 下载任务", 422)
    return [str(job.id) for job in jobs]


async def land_context(db: AsyncSession, land_id: str) -> dict[str, Any] | None:
    """下载机读取本季标记、播种日和已有官方 S2 日期。"""
    land = await _load_land(db, land_id)
    if land is None:
        return None
    today = date.today()
    season = default_season_key(today)
    sowing = _sowing_for_season(land, season)
    start, end = season_bounds(season)
    if sowing is not None:
        start = max(start, sowing)
    rows = (await db.execute(_LOAD_DAILY, {"land_id": land_id, "date_from": start, "date_to": end})).fetchall()
    dates = []
    seen = set()
    for row in rows:
        item = _as_dict(row)
        day = _as_date(item.get("date"))
        if day is None or day.isoformat() in seen:
            continue
        seen.add(day.isoformat())
        dates.append(day.isoformat())
    return {
        "land_id": land_id,
        "crop_type": land.get("crop_type"),
        "autumn_emerged": bool(land.get("autumn_emerged")),
        "autumn_emerged_season": land.get("autumn_emerged_season"),
        "autumn_emerged_date": _iso(land.get("autumn_emerged_date")),
        "autumn_sowing_date": _iso(sowing),
        "autumn_sowing_season": land.get("autumn_sowing_season"),
        "official_s2_dates": dates,
    }


def apply_land_emerged_flag_sql() -> str:
    return """
    UPDATE agric_satellite.land_parcels
    SET autumn_emerged = true,
        autumn_emerged_season = :season_key,
        autumn_emerged_date = COALESCE(
            :emerged_date::date,
            autumn_emerged_date
        ),
        autumn_emerged_at = COALESCE(autumn_emerged_at, now()),
        updated_at = now()
    WHERE land_id = :land_id
      AND deleted_at IS NULL
      AND NOT (
          COALESCE(autumn_emerged, false)
          AND autumn_emerged_season IS NOT DISTINCT FROM :season_key
      )
    """


def _schedule_payload(
    *,
    land_id: str,
    season: str,
    sowing_date: date | None,
    status: str,
    date_from: date,
    date_to: date,
    sensors: list[str],
    job_ids: list[str],
    message: str,
    pull: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = _idle_pull()
    if pull:
        body.update(pull)
    body.update(
        {
            "land_id": land_id,
            "field_id": land_id,
            "season_key": season,
            "sowing_date": sowing_date,
            "eligible": True,
            "status": status,
            "date_from": date_from,
            "date_to": date_to,
            "sensors": sensors,
            "job_ids": list(job_ids),
            "message": message,
        }
    )
    if status == "frozen":
        body["display_name"] = None
        body["purpose"] = None
        body["claim_type"] = None
        body["dispatch"] = None
        body["job_status"] = None
        body["phase"] = "idle"
    return body


def _dispatch_message(sowing_date: date, date_to: date) -> str:
    return (
        "已按更新影像的同一领取队列派发出苗拉取。"
        f"窗口从播种日 {sowing_date.isoformat()} 到 {date_to.isoformat()}，只包含 S2。"
        "下载机领取 satellite_batch；已入库的晴空景由该任务跳过并直接写出苗字段，"
        "API 不下载影像，也不在本机计算像元。"
        "进度用返回的 job_ids 调用 GET /v1/jobs/{job_id}："
        "status 为 pending、running、completed 或 failed，"
        "progress_json 有 scenes_total 与 scenes_done，error 是失败原因。"
        "领取人是对应 work item 的 lease_owner，结束后看 last_claimed_by。"
        "官方 S2 回执入库后即可 GET autumn-emergence 查看逐日结果。"
    )


def _empty_view(land_id: str, season: str, *, eligible: bool) -> dict[str, Any]:
    body = _idle_pull()
    body.update(
        {
            "land_id": land_id,
            "field_id": land_id,
            "eligible": eligible,
            "pending": False,
            "season_key": season,
            "sowing_date": None,
            "status": None,
            "status_zh": None,
            "emerged_flag": False,
            "frozen": False,
            "baseline": {"dates": [], "start": None, "end": None, "statistic": None},
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
    )
    return body


def _apply_daily_summary(
    view: dict[str, Any],
    days: dict[str, dict[str, Any]],
    *,
    frozen: bool,
    land: dict[str, Any],
) -> None:
    timeseries = []
    last_field = None
    first_emerged = _as_date(land.get("autumn_emerged_date")) if frozen else None
    for day, field in days.items():
        last_field = field
        timeseries.append(
            {
                "date": day,
                "in_baseline": bool(field.get("in_baseline")),
                "n_pixels": int(field.get("n_pixels") or 0),
                "median_ndvi": field.get("median_ndvi"),
                "median_increment": field.get("median_increment"),
                "share_above_noise": field.get("share_above_noise"),
                "share_sustained": field.get("share_sustained"),
                "status": field.get("status"),
            }
        )
        if not first_emerged and field.get("emerged_flag"):
            first_emerged = _as_date(field.get("first_emerged_date") or field.get("as_of_date") or day)
    view["timeseries"] = timeseries
    if last_field is None:
        return
    baseline_dates = [
        _as_date(d) for d in (last_field.get("baseline_dates") or []) if _as_date(d)
    ]
    view["baseline"] = {
        "dates": baseline_dates,
        "start": baseline_dates[0] if baseline_dates else None,
        "end": baseline_dates[-1] if baseline_dates else None,
        "statistic": last_field.get("baseline_statistic"),
    }
    view["noise_floor"] = last_field.get("noise_floor")
    view["noise_statistic"] = last_field.get("noise_statistic")
    view["as_of_date"] = _as_date(last_field.get("as_of_date")) or _as_date(list(days)[-1])
    view["method_version"] = last_field.get("method_version") or METHOD_VERSION
    view["summary"] = {
        "n_clear": last_field.get("n_clear"),
        "n_pixels": last_field.get("n_pixels"),
        "median_increment": last_field.get("median_increment"),
        "share_above_noise": last_field.get("share_above_noise"),
        "share_sustained": last_field.get("share_sustained"),
        "share_ndvi_ge_0_30": last_field.get("share_ndvi_ge_0_30"),
        "ndre_share_above_noise": last_field.get("ndre_share_above_noise"),
        "class_counts": last_field.get("class_counts"),
        "reason": last_field.get("reason"),
    }
    if not frozen:
        view["status"] = last_field.get("status")
        view["status_zh"] = last_field.get("status_zh") or STATUS_ZH.get(
            str(last_field.get("status") or ""), None
        )
        view["emerged_flag"] = bool(last_field.get("emerged_flag"))
        view["frozen"] = bool(last_field.get("emerged_flag"))
        view["first_emerged_date"] = first_emerged
    else:
        view["first_emerged_date"] = first_emerged or view.get("first_emerged_date")


async def _load_daily_fields(
    db: AsyncSession,
    land_id: str,
    season: str,
    *,
    sowing: date | None,
) -> dict[str, dict[str, Any]]:
    start, end = season_bounds(season)
    if sowing is not None:
        start = max(start, sowing)
    rows = (
        await db.execute(
            _LOAD_DAILY, {"land_id": land_id, "date_from": start, "date_to": end}
        )
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _as_dict(row)
        day = _as_date(item.get("date"))
        if day is None:
            continue
        field = daily_field_of({"pixel_data": item.get("pixel_data")})
        if not field:
            continue
        # Same day, keep the last official row that actually has a field.
        out[day.isoformat()] = field
    return out


async def _load_season_jobs(
    db: AsyncSession, land_id: str, season: str
) -> list[dict[str, Any]]:
    rows = (await db.execute(_LAND_JOBS, {"land_id": land_id})).fetchall()
    matched: list[dict[str, Any]] = []
    for row in rows:
        item = _as_dict(row)
        params = _jsonish(item.get("params_json")) or {}
        job_season = str(params.get("season_key") or "")
        sowing = _as_date(params.get("sowing_date"))
        if job_season and job_season != season:
            continue
        if not job_season and sowing is not None and autumn_season_key(sowing) not in {None, season}:
            continue
        item["params_json"] = params
        matched.append(item)
    return matched


async def _pull_progress_from_jobs(
    db: AsyncSession,
    jobs: list[dict[str, Any]],
    *,
    sowing: date | None,
) -> dict[str, Any]:
    if not jobs:
        return _idle_pull()
    # Prefer an in-flight wave; otherwise the most recent completed/failed one.
    open_jobs = [
        row for row in jobs if str(row.get("status") or "") in {"pending", "running"}
    ]
    wave = open_jobs or jobs[:1]
    # Keep jobs from the same created wave when several shards exist.
    if open_jobs:
        wave = open_jobs
    job_ids = [str(row["id"]) for row in wave]
    summary = summarize_pull_jobs(wave)
    summary["job_ids"] = job_ids
    summary["display_name"] = PULL_DISPLAY_NAME
    summary["purpose"] = PULL_PURPOSE
    summary["claim_type"] = PULL_CLAIM_TYPE
    summary["dispatch"] = PULL_CLAIM_TYPE
    params = wave[0].get("params_json") or {}
    summary["date_from"] = _as_date(params.get("date_from") or params.get("sowing_date") or sowing)
    summary["date_to"] = _as_date(params.get("date_to"))
    summary["progress_message"] = _pull_message(summary)
    return summary


def _sowing_for_season(land: dict[str, Any] | None, season: str) -> date | None:
    if not land:
        return None
    stored_season = land.get("autumn_sowing_season")
    sowing = _as_date(land.get("autumn_sowing_date"))
    if sowing is None:
        return None
    if stored_season and stored_season != season:
        return None
    if not stored_season and autumn_season_key(sowing) not in {None, season}:
        return None
    return sowing


def _idle_pull() -> dict[str, Any]:
    return {
        "display_name": None,
        "purpose": None,
        "claim_type": None,
        "dispatch": None,
        "job_ids": [],
        "job_status": None,
        "claimed_by": None,
        "pending_jobs": 0,
        "running_jobs": 0,
        "completed_jobs": 0,
        "failed_jobs": 0,
        "total_jobs": 0,
        "percent": 0.0,
        "phase": "idle",
        "failure_reason": None,
        "scenes_total": 0,
        "scenes_done": 0,
        "scenes_remaining": 0,
        "progress_message": None,
        "date_from": None,
        "date_to": None,
    }


def _fresh_pull(job_ids: list[str]) -> dict[str, Any]:
    summary = summarize_pull_jobs(
        [{"id": job_id, "status": "pending"} for job_id in job_ids]
    )
    summary["job_ids"] = list(job_ids)
    summary["display_name"] = PULL_DISPLAY_NAME
    summary["purpose"] = PULL_PURPOSE
    summary["claim_type"] = PULL_CLAIM_TYPE
    summary["dispatch"] = PULL_CLAIM_TYPE
    summary["date_from"] = None
    summary["date_to"] = None
    summary["progress_message"] = _pull_message(summary)
    return summary


def summarize_pull_jobs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把 satellite_batch 分片收成与回填状态相同的计数。"""
    pending = running = completed = failed = 0
    scenes_total = scenes_done = 0
    failure: str | None = None
    lease_owner: str | None = None
    last_claimed: str | None = None
    ids: list[str] = []
    for row in rows:
        job_id = str(row.get("id") or "")
        if job_id:
            ids.append(job_id)
        status = str(row.get("status") or "")
        work_status = str(row.get("work_status") or "")
        if status in {"failed", "cancelled"}:
            failed += 1
            if failure is None:
                failure = _first_text(row.get("error"), row.get("work_error"))
        elif status in {"completed", "succeeded"}:
            completed += 1
        elif status == "running" or work_status == "leased":
            running += 1
        elif work_status == "failed":
            failed += 1
            if failure is None:
                failure = _first_text(row.get("work_error"), row.get("error"))
        else:
            pending += 1
        progress = _jsonish(row.get("progress_json"))
        if isinstance(progress, dict):
            scenes_total += _int(progress.get("scenes_total"))
            scenes_done += _int(progress.get("scenes_done"))
        if lease_owner is None and row.get("lease_owner"):
            lease_owner = str(row["lease_owner"])
        if last_claimed is None and row.get("last_claimed_by"):
            last_claimed = str(row["last_claimed_by"])
    if running:
        job_status, phase = "running", "stac"
    elif pending:
        job_status, phase = "pending", "stac"
    elif failed:
        job_status, phase = "failed", "failed"
    elif completed:
        job_status, phase = "completed", "done"
    else:
        job_status, phase = None, "idle"
    if job_status == "failed" and not failure:
        failure = "出苗拉取失败"
    denom = pending + running + completed
    if scenes_total > 0:
        percent = round(100.0 * min(scenes_done, scenes_total) / scenes_total, 1)
    elif denom:
        percent = round(100.0 * completed / denom, 1)
    elif rows:
        percent = 100.0
    else:
        percent = 0.0
    return {
        "job_ids": ids,
        "job_status": job_status,
        "claimed_by": lease_owner or last_claimed,
        "pending_jobs": pending,
        "running_jobs": running,
        "completed_jobs": completed,
        "failed_jobs": failed,
        "total_jobs": pending + running + completed + failed,
        "percent": percent,
        "phase": phase,
        "failure_reason": failure if job_status == "failed" else None,
        "scenes_total": scenes_total,
        "scenes_done": scenes_done,
        "scenes_remaining": max(scenes_total - scenes_done, 0),
    }


def _pull_message(summary: dict[str, Any]) -> str | None:
    status = summary.get("job_status")
    if status is None:
        return None
    if status == "failed":
        return summary.get("failure_reason") or "出苗拉取失败"
    if status == "completed":
        return "出苗影像任务已完成。逐日结果写在官方日指标里，请 GET 查看摘要和像元。"
    failed = int(summary.get("failed_jobs") or 0)
    return (
        "正在拉取出苗影像… "
        f"分片任务 {int(summary.get('completed_jobs') or 0)}/"
        f"{int(summary.get('total_jobs') or 0)}"
        f"（进行中 {int(summary.get('running_jobs') or 0)}，"
        f"排队 {int(summary.get('pending_jobs') or 0)}"
        + (f"，失败 {failed}" if failed else "")
        + f"，约 {float(summary.get('percent') or 0):.0f}%）"
    )


async def _load_land(db: AsyncSession, land_id: str) -> dict[str, Any] | None:
    row = (await db.execute(_LOAD_LAND, {"land_id": land_id})).first()
    return _as_dict(row) if row is not None else None


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
    if isinstance(row, dict):
        return row
    return dict(row) if row is not None else {}


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


def _iso(value: Any) -> str | None:
    day = _as_date(value)
    return day.isoformat() if day else None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _first_text(*values: Any) -> str | None:
    for value in values:
        text_value = str(value).strip() if value is not None else ""
        if text_value:
            return text_value
    return None
