"""Celery 任务：从 OSS 批量读取总览数据，在下载机本地预聚合。"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import structlog

from agric_satellite_analysis_common.internal_api import internal_client
from agric_satellite_analysis_common.storage import get_storage
from app.core.overview_preagg import OverviewAccumulator
from app.worker import celery_app

logger = structlog.get_logger()

DEFAULT_OSS_WORKERS = 8
MAX_OSS_WORKERS = 32
DEFAULT_OSS_RETRIES = 3
MAX_OSS_RETRIES = 5
OSS_RETRY_BASE_SECONDS = 1.0
OSS_RETRY_MAX_SECONDS = 10.0


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
    oss_workers: int = DEFAULT_OSS_WORKERS,
    oss_retries: int = DEFAULT_OSS_RETRIES,
) -> dict[str, Any]:
    """按游标领取 OSS 输入批次，并发下载/计算后回传 OSS 结果包。"""
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
    worker_count = max(1, min(int(oss_workers), MAX_OSS_WORKERS))
    retry_count = max(0, min(int(oss_retries), MAX_OSS_RETRIES))
    max_attempts = retry_count + 1
    logger.info(
        "overview_preagg_start",
        window_days=window_days,
        crop=crop,
        land_batch_size=batch_size,
        oss_workers=worker_count,
        oss_retries=retry_count,
        transport="oss",
    )
    cursor: str | None = None
    batch_count = 0
    land_count = 0
    last_batch: dict[str, Any] | None = None

    def _download_json(
        client: httpx.Client,
        url: str,
        expected_bytes: int | None,
        expected_sha256: str | None,
        max_attempts: int,
    ) -> dict[str, Any]:
        for attempt in range(1, max_attempts + 1):
            try:
                response = client.get(url)
                response.raise_for_status()
                raw = response.content
                if expected_bytes is not None and len(raw) != expected_bytes:
                    raise RuntimeError(
                        "总览 OSS 输入包大小不一致: "
                        f"expected={expected_bytes}, actual={len(raw)}"
                    )
                if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
                    raise RuntimeError("总览 OSS 输入包 sha256 校验失败")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("lands"), list
                ):
                    raise RuntimeError("总览 OSS 输入包格式无效")
                return payload
            except Exception as exc:
                retryable = isinstance(exc, httpx.RequestError) or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code in {408, 429, 500, 502, 503, 504}
                )
                if not retryable or attempt >= max_attempts:
                    raise
                delay = min(
                    OSS_RETRY_MAX_SECONDS,
                    OSS_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                ) * (0.5 + random.random())
                logger.warning(
                    "overview_oss_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retry_in_seconds=round(delay, 3),
                    error_type=type(exc).__name__,
                    status_code=getattr(getattr(exc, "response", None), "status_code", None),
                )
                time.sleep(delay)

        raise RuntimeError("总览 OSS 输入包下载重试状态异常")

    def _download_and_aggregate(
        client: httpx.Client, batch: dict[str, Any]
    ) -> tuple[OverviewAccumulator, int]:
        try:
            payload = _download_json(
                client,
                str(batch["oss_url"]),
                int(batch["bytes"]) if batch.get("bytes") is not None else None,
                str(batch["sha256"]) if batch.get("sha256") else None,
                max_attempts,
            )
            local_accumulator = OverviewAccumulator(
                window_from=str(payload["window_from"]),
                window_to=str(payload["window_to"]),
                crop=payload.get("crop"),
            )
            # 下载和分类都在线程池中执行；每个线程使用独立聚合器，避免共享状态竞争。
            local_accumulator.add_batch(list(payload["lands"]))
            return local_accumulator, int(batch.get("land_count") or 0)
        except Exception as exc:
            # 只记录 OSS key，不记录带签名参数的 URL，避免把临时凭据写入日志。
            logger.error(
                "overview_oss_batch_failed",
                oss_key=batch.get("oss_key"),
                land_count=batch.get("land_count"),
                error_type=type(exc).__name__,
            )
            raise

    accumulator: OverviewAccumulator | None = None

    def _merge_future(future: Any) -> None:
        nonlocal accumulator, batch_count, land_count
        local_accumulator, current_land_count = future.result()
        if accumulator is None:
            accumulator = OverviewAccumulator(
                window_from=local_accumulator.window_from,
                window_to=local_accumulator.window_to,
                crop=local_accumulator.crop,
            )
        accumulator.merge(local_accumulator)
        batch_count += 1
        land_count += current_land_count

    # 游标决定下一批 land_id，API 请求仍然串行；但每拿到一个 OSS 引用就立即提交下载，
    # 让 API 生成下一批数据和下载机处理上一批数据重叠执行。队列设置上限，避免批次过多时
    # 无限积压 Future 和聚合结果；按提交顺序合并以保持结果稳定可复现。
    pending_futures: list[Any] = []
    max_pending = worker_count * 2
    # httpx.Client 可在线程间复用连接池；限制连接数，避免并发下载压垮 OSS 或下载机。
    limits = httpx.Limits(
        max_connections=worker_count,
        max_keepalive_connections=worker_count,
    )
    with httpx.Client(timeout=300.0, limits=limits) as oss_client:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="overview-oss",
        ) as executor:
            with internal_client(timeout=300.0) as client:
                while True:
                    batch = refresh_overview_http(
                        window_days=window_days,
                        crop=crop,
                        land_batch_size=batch_size,
                        after_land_id=cursor,
                        client=client,
                    )
                    last_batch = batch
                    if batch.get("oss_url"):
                        pending_futures.append(
                            executor.submit(_download_and_aggregate, oss_client, batch)
                        )
                        if len(pending_futures) >= max_pending:
                            _merge_future(pending_futures.pop(0))

                    if bool(batch.get("done")):
                        break
                    next_cursor = str(batch.get("next_cursor") or "")
                    if not next_cursor or next_cursor == cursor:
                        raise RuntimeError("总览 OSS 批次游标没有前进")
                    cursor = next_cursor

            while pending_futures:
                _merge_future(pending_futures.pop(0))

    if accumulator is None:
        # 没有地块时仍生成一个合法的全国空结果，保证缓存状态可追踪。
        if last_batch is None:
            raise RuntimeError("总览 OSS 批次响应为空")
        accumulator = OverviewAccumulator(
            window_from=str(last_batch["window_from"]),
            window_to=str(last_batch["window_to"]),
            crop=last_batch.get("crop"),
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
    with internal_client(timeout=300.0) as client:
        out = finalize_overview_stats(result_key, client=client)

    logger.info(
        "overview_preagg_done",
        regions=out.get("regions"),
        land_count=land_count,
        batch_count=batch_count,
        result_oss_key=result_key,
        transport="oss",
    )
    return {
        **out,
        "result_oss_key": result_key,
        "land_count": land_count,
        "batch_count": batch_count,
    }
