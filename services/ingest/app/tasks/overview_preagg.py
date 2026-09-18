"""Celery 任务：从 OSS 批量读取总览数据，在下载机本地预聚合。"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import httpx
import structlog

from agric_satellite_analysis_common.internal_api import internal_client
from agric_satellite_analysis_common.storage import get_storage
from app.core.overview_preagg import OverviewAccumulator
from app.worker import celery_app

logger = structlog.get_logger()


@celery_app.task(
    name="app.tasks.overview_preagg.refresh_daily_satellite",
    bind=True,
    max_retries=300,
    time_limit=600,
    soft_time_limit=540,
)
def refresh_daily_satellite(
    self, run_id: str | None = None, as_of: str | None = None
) -> dict[str, Any]:
    """每天拉取全量地块的新影像，延时检查入库，完成后由API计算快照。"""
    from agric_satellite_analysis_common.internal_api import (
        daily_satellite_finalize,
        daily_satellite_prepare,
        internal_api_enabled,
    )

    if not internal_api_enabled():
        raise RuntimeError("每日全国刷新需要API_BASE_URL和INTERNAL_API_TOKEN")
    # 首次触发固定北京时间统计日；跨日重试继续同一批次，不能创建下一天的下载。
    if not as_of:
        from datetime import datetime, timedelta, timezone

        as_of = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    try:
        if run_id is None:
            prepared = daily_satellite_prepare(as_of=as_of)
            run_id = prepared["run_id"]
        out = daily_satellite_finalize(run_id)
    except Exception as exc:
        logger.exception("overview_daily_http_failed", run_id=run_id, as_of=as_of)
        raise self.retry(
            exc=exc, kwargs={"run_id": run_id, "as_of": as_of}, countdown=300
        )
    if out["status"] not in ("completed", "partial"):
        # 用消息延时而非占用worker等待；MQ结果真正入库之后才发布当天态势。
        raise self.retry(kwargs={"run_id": run_id, "as_of": as_of}, countdown=300)
    logger.info(
        "overview_daily_finished",
        run_id=run_id,
        status=out["status"],
        regions=out.get("regions"),
    )
    return out


@celery_app.task(name="app.tasks.overview_preagg.refresh_overview_stats")
def refresh_overview_stats(
    window_days: int = 60,
    crop: str | None = None,
    land_batch_size: int = 10,
) -> dict[str, Any]:
    """按游标领取 OSS 输入批次，下载机本地计算后回传 OSS 结果包。"""
    try:
        from agric_satellite_analysis_common.internal_api import (
            internal_api_enabled,
            finalize_overview_stats,
            refresh_overview_stats as refresh_overview_http,
        )
    except ImportError as exc:
        raise RuntimeError(
            "refresh_overview_stats 需要 agric_satellite_analysis_common.internal_api"
        ) from exc

    if not internal_api_enabled():
        raise RuntimeError(
            "refresh_overview_stats 需要 API_BASE_URL 和 INTERNAL_API_TOKEN；"
            "下载机通过 OSS 输入包计算，不直连 Postgres"
        )

    batch_size = max(1, min(int(land_batch_size), 1000))
    logger.info(
        "overview_preagg_start",
        window_days=window_days,
        crop=crop,
        land_batch_size=batch_size,
        transport="oss",
    )
    cursor: str | None = None
    batch_count = 0
    land_count = 0
    accumulator: OverviewAccumulator | None = None

    def _download_json(url: str, expected_bytes: int | None, expected_sha256: str | None) -> dict[str, Any]:
        with httpx.Client(timeout=300.0) as client:
            response = client.get(url)
            response.raise_for_status()
            raw = response.content
        if expected_bytes is not None and len(raw) != expected_bytes:
            raise RuntimeError(
                f"总览 OSS 输入包大小不一致: expected={expected_bytes}, actual={len(raw)}"
            )
        if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RuntimeError("总览 OSS 输入包 sha256 校验失败")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("lands"), list):
            raise RuntimeError("总览 OSS 输入包格式无效")
        return payload

    with internal_client(timeout=300.0) as client:
        while True:
            batch = refresh_overview_http(
                window_days=window_days,
                crop=crop,
                land_batch_size=batch_size,
                after_land_id=cursor,
                client=client,
            )
            if batch.get("oss_url"):
                payload = _download_json(
                    str(batch["oss_url"]),
                    int(batch["bytes"]) if batch.get("bytes") is not None else None,
                    str(batch["sha256"]) if batch.get("sha256") else None,
                )
                if accumulator is None:
                    accumulator = OverviewAccumulator(
                        window_from=str(payload["window_from"]),
                        window_to=str(payload["window_to"]),
                        crop=payload.get("crop"),
                    )
                accumulator.add_batch(list(payload["lands"]))
                batch_count += 1
                land_count += int(batch.get("land_count") or 0)

            if bool(batch.get("done")):
                break
            next_cursor = str(batch.get("next_cursor") or "")
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError("总览 OSS 批次游标没有前进")
            cursor = next_cursor

        if accumulator is None:
            # 没有地块时仍生成一个合法的全国空结果，保证缓存状态可追踪。
            accumulator = OverviewAccumulator(
                window_from=str(batch["window_from"]),
                window_to=str(batch["window_to"]),
                crop=batch.get("crop"),
            )

        result_payload = {
            "schema_version": 1,
            "window_from": accumulator.window_from,
            "window_to": accumulator.window_to,
            "crop": accumulator.crop,
            "land_count": land_count,
            "batch_count": batch_count,
            "results": accumulator.results(),
        }
        result_raw = json.dumps(
            result_payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        result_key = (
            "overview/preagg/output/"
            f"{accumulator.window_from}_{accumulator.window_to}/{uuid.uuid4().hex}.json"
        )
        get_storage().put_bytes(
            result_key,
            result_raw,
            content_type="application/json",
        )
        out = finalize_overview_stats(result_key, client=client)

    logger.info(
        "overview_preagg_done",
        regions=out.get("regions"),
        land_count=land_count,
        batch_count=batch_count,
        result_oss_key=result_key,
        transport="oss",
    )
    return {**out, "result_oss_key": result_key, "land_count": land_count, "batch_count": batch_count}
